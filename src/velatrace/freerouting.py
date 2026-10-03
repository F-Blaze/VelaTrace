"""Pinned, offline external Freerouting process. Never writes a user's board.

Java 21's process-local security policy denies Java networking. This is not an
OS sandbox for hostile native code: the immutable official JAR is a trust anchor.
"""
import atexit
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import tempfile
import threading
import time

from .constraints import Constraint
from .dsn import DsnInput, dsn_scale
from .errors import CapabilityError, RoutingCancelled, ValidationError
from .ses import number
from .sexpr import JoinedAtom, QuotedAtom, one, parse

VERSION = "2.1.0"
JAR_SHA256 = "2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def"
RELEASE_URL = "https://github.com/freerouting/freerouting/releases/tag/v2.1.0"
PROBE_SHA256 = "c27481d8f2e0505ec21b8ba375888343dfcc06406d9e62e4ab6c7c63929ef6de"
WARM_SHA256 = "4283bd5219bf2bf1f85ea7ae07a28d0fa41121f8d8bb9020ae381db4a284adae"
MAX_SES = 32_000_000
WARM_START_SECONDS = 60
WARM_MAX_FAILURES = 2
CANCELLED = "Cancelled; nothing was written to the board."
# Freerouting 2.1.0 logs e.g. "Auto-router pass #1 on board '…' was completed in 1.84
# seconds with the score of 933.76 (1 unrouted)." The count is omitted at zero.
PASS_LINE = re.compile(rb"Auto-router pass #(\d+) [^\n]*?(?:\((\d+) unrouted\))?\.?\r?\n")
# Two crossing nets on SMD pads (needs vias): routed once at warm start-up to load
# and JIT-compile the router before the user's first route, and to self-test it.
WARMUP_DSN = """(pcb warmup
 (parser (string_quote ") (space_in_quoted_tokens on) (host_cad KiCad) (host_version 9.0))
 (resolution um 10) (unit um)
 (structure
  (layer F.Cu (type signal) (property (index 0)))
  (layer B.Cu (type signal) (property (index 1)))
  (boundary (path pcb 0 0 0 30000 0 30000 20000 0 20000 0 0))
  (via Via[0-1]_600:300_um)
  (rule (width 250) (clearance 200)))
 (placement (component Pad1
  (place J1 5000 10000 front 0) (place J2 25000 10000 front 0)
  (place J3 15000 3000 front 0) (place J4 15000 17000 front 0)))
 (library
  (image Pad1 (pin Pad 1 0 0))
  (padstack Pad (shape (circle F.Cu 1500)) (attach off))
  (padstack Via[0-1]_600:300_um (shape (circle F.Cu 600)) (shape (circle B.Cu 600)) (attach off)))
 (network (net A (pins J1-1 J2-1)) (net B (pins J3-1 J4-1))
  (class Default A B (circuit (use_via Via[0-1]_600:300_um)) (rule (width 250) (clearance 200))))
 (wiring))
"""


def local_path(path: Path) -> Path:
    value = Path(path).resolve()
    if str(value).startswith(("\\\\", "//")) or '${' in str(value) or any(ch in str(value) for ch in ('"', '\n', '\r')):
        raise ValidationError("Router paths must be local filesystem paths, not UNC/network paths.")
    return value


def clean_environment(directory: Path) -> dict[str, str]:
    # No provider keys, JAVA_TOOL_OPTIONS, CLASSPATH, proxies or router env settings.
    environment = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR"}}
    environment.update({"HOME": str(directory), "USERPROFILE": str(directory),
                        "TMP": str(directory), "TEMP": str(directory),
                        "APPDATA": str(directory), "LOCALAPPDATA": str(directory)})
    return environment


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    output: str


def _drain(stream, chunks: bytearray, on_data=None) -> None:
    """Read until EOF, retaining only the final 64 KiB; on_data(chunks) after each read."""
    while data := stream.read1(4096):
        chunks.extend(data)
        if len(chunks) > 65_536:
            del chunks[:-65_536]
        if on_data is not None:
            on_data(chunks)


def pass_reporter(progress):
    """An on_data callback reporting each new Freerouting pass line through progress()."""
    last = [None]
    def report(chunks):
        found = PASS_LINE.findall(bytes(chunks[-8192:]))
        if found and found[-1] != last[0]:
            last[0] = found[-1]
            number, unrouted = found[-1]
            progress(f"Routing · pass {int(number)}" + (f" · {int(unrouted)} unrouted" if unrouted else ""))
    return report


def run_bounded(args: list[str], directory: Path, timeout: float, cancel=None, on_data=None) -> ProcessResult:
    """Drain output continuously, retain only final 64 KiB, kill on timeout or cancel."""
    chunks = bytearray()
    try:
        process = subprocess.Popen(args, cwd=directory, env=clean_environment(directory),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise CapabilityError("Cannot start Java. Install Java 21 and configure its executable path.") from exc
    reader = threading.Thread(target=_drain, args=(process.stdout, chunks, on_data), daemon=True)
    reader.start()
    try:
        _wait(process, timeout, cancel)
    finally:
        reader.join(timeout=5)
        process.stdout.close()
    return ProcessResult(process.returncode, chunks.decode("utf-8", errors="replace"))


def _wait(process, timeout: float, cancel=None) -> None:
    """Wait for exit; kill on timeout or when `cancel` (a threading.Event) is set."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            process.wait(timeout=.1)
            return
        except subprocess.TimeoutExpired:
            cancelled = cancel is not None and cancel.is_set()
            if cancelled or time.monotonic() > deadline:
                process.kill()
                process.wait()
                if cancelled:
                    raise RoutingCancelled(CANCELLED) from None
                raise CapabilityError(f"Freerouting/Java exceeded {timeout:g} seconds and was stopped; nothing applied.") from None


def supports_constraints(constraints: tuple[Constraint, ...]) -> bool:
    return all(c.kind in {"clearance", "trace-width"} and c.target == "all nets" for c in constraints)


def _serialize(node: list) -> str:
    def atom(value):
        if isinstance(value, list):
            return _serialize(value)
        if isinstance(value, JoinedAtom):
            return value.raw  # e.g. "Pi-1"-3: re-quoting as one string would change the pin reference.
        if value and not isinstance(value, QuotedAtom) and not re.search(r'[\s()"\\]', value):
            return value
        # Specctra has no escapes: Freerouting reads a backslash literally and ends at the next quote.
        if '"' in value:
            raise ValidationError("DSN text contains a quote Specctra cannot represent.")
        return '"' + value + '"'
    if node == ["string_quote", '"']:
        return '(string_quote ")'
    return '(' + ' '.join(atom(item) for item in node) + ')'


def constrained_dsn(text: str, constraints: tuple[Constraint, ...]) -> str:
    """Increase every width/clearance rule; never weaken a pre-existing rule.

    Only global all-nets targets are supported. Header exclusion and per-net
    constraints must be refused, never passed to the router as unenforced prose.
    """
    if not supports_constraints(constraints):
        raise CapabilityError("Router supports clearance/trace-width for 'all nets' only. Header keepouts and per-net targets require a geometry adapter; revise explicitly.")
    if not constraints:
        return text
    root = parse(text)
    scale = dsn_scale(root)
    one(one(root, "structure"), "rule")
    thresholds = {"width": 0.0, "clearance": 0.0}
    for constraint in constraints:
        kind = "width" if constraint.kind == "trace-width" else "clearance"
        thresholds[kind] = max(thresholds[kind], constraint.minimum_mm / scale)
    def visit(node):
        if node and node[0] == "rule":
            present = set()
            for item in node[1:]:
                if isinstance(item, list) and item and item[0] in thresholds:
                    if len(item) < 2 or not isinstance(item[1], str):
                        raise ValidationError("Unsupported DSN numeric rule.")
                    value = number(item[1])
                    item[1] = format(max(value, thresholds[item[0]]), '.12g')
                    present.add(item[0])
            for kind, minimum in thresholds.items():
                if minimum and kind not in present:
                    node.append([kind, format(minimum, '.12g')])
        for child in node:
            if isinstance(child, list):
                visit(child)
    visit(root)
    return _serialize(root)


def _policy(directory: Path, jar: Path) -> str:
    # Paths are literals: no untrusted Java property expansion or quote injection.
    def quote(path):
        return str(path).replace('\\', '/').replace('${', '$\\{')
    return f'''grant {{
 permission java.io.FilePermission "${{java.home}}${{/}}-", "read";
 permission java.io.FilePermission "{quote(jar)}", "read";
 permission java.io.FilePermission "{quote(directory)}", "read,write,delete";
 permission java.io.FilePermission "{quote(directory)}/-", "read,write,delete";
 permission java.util.PropertyPermission "*", "read,write";
 permission java.lang.RuntimePermission "accessDeclaredMembers";
 permission java.lang.RuntimePermission "getClassLoader";
 permission java.lang.RuntimePermission "setContextClassLoader";
 permission java.lang.RuntimePermission "createClassLoader";
 permission java.lang.RuntimePermission "accessClassInPackage.*";
 permission java.lang.RuntimePermission "getenv.*";
 permission java.lang.RuntimePermission "shutdownHooks";
 permission java.lang.RuntimePermission "modifyThread";
 permission java.lang.RuntimePermission "modifyThreadGroup";
 permission java.lang.RuntimePermission "setDefaultUncaughtExceptionHandler";
 permission java.lang.RuntimePermission "setIO";
 permission java.lang.RuntimePermission "exitVM.*";
 permission java.lang.RuntimePermission "loadLibrary.*";
 permission java.lang.reflect.ReflectPermission "suppressAccessChecks";
 permission java.util.logging.LoggingPermission "control";
 permission java.security.SecurityPermission "getProperty.*";
 permission java.security.SecurityPermission "putProviderProperty.*";
 permission java.awt.AWTPermission "*";
}};
'''


def _locked_jar(path: Path):
    """Return (handle, sha256) with the JAR hashed through a Windows handle that
    shares read access only: while it is open nobody can write, rename or delete
    the file, and opening fails if a writer already has it open. So the verified
    bytes stay the bytes a long-lived JVM reads. Elsewhere returns (None, sha256)
    and callers must re-hash before every use."""
    if os.name != "nt":
        return None, hashlib.sha256(path.read_bytes()).hexdigest()
    import ctypes
    from ctypes import wintypes
    import msvcrt
    create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create.restype = wintypes.HANDLE
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    # GENERIC_READ, FILE_SHARE_READ only, OPEN_EXISTING.
    handle = create(str(path), 0x80000000, 0x1, None, 3, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "Cannot lock the Freerouting JAR")
    locked = os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb")
    try:
        return locked, hashlib.file_digest(locked, "sha256").hexdigest()
    except BaseException:
        locked.close()
        raise


class _WarmFailure(Exception):
    """The reusable JVM failed; the caller falls back to a one-shot run."""


class _WarmRouter:
    """One long-lived JVM running router_resources/WarmRouter under the offline policy.

    Start-up does everything a one-shot route does before launching Freerouting:
    pinned JAR hash, Java 21 check and the OfflineProbe in this exact directory.
    """
    def __init__(self, owner: "Freerouting"):
        owner.work_directory.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="warm-", dir=owner.work_directory))
        self.jar_lock = self.process = None
        self.log = bytearray()
        self.on_log = None  # The current job's pass reporter.
        self.replies: Queue = Queue()
        atexit.register(self.close)
        try:
            self.jar_lock, digest = _locked_jar(owner.jar)
            if digest != JAR_SHA256:
                raise CapabilityError(f"Freerouting version/hash mismatch. Install exactly {VERSION} from {RELEASE_URL}; other JARs are refused.")
            owner._check_java(self.directory)
            owner._prepare(self.directory)
            launcher = (Path(__file__).parent / "router_resources" / "WarmRouter.class").read_bytes()
            if hashlib.sha256(launcher).hexdigest() != WARM_SHA256:
                raise _WarmFailure("WarmRouter.class is missing or modified")
            (self.directory / "WarmRouter.class").write_bytes(launcher)
            self.process = subprocess.Popen(
                owner._args(self.directory) + ["-cp", f"{self.directory}{os.pathsep}{owner.jar}",
                                               "WarmRouter", str(self.directory)],
                cwd=self.directory, env=clean_environment(self.directory), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=self._pump, daemon=True, name="velatrace-warm-replies").start()
            threading.Thread(target=self._drain_log, daemon=True, name="velatrace-warm-log").start()
            if self._reply(WARM_START_SECONDS) != "VELATRACE_WARM_READY":
                raise _WarmFailure("Warm router did not start")
            warmup = self.directory / "warmup"
            warmup.mkdir()
            (warmup / "warmup.dsn").write_text(WARMUP_DSN, encoding="utf-8")
            self.run(owner._router_args(warmup, warmup / "warmup.dsn", warmup / "warmup.ses"), WARM_START_SECONDS)
            shutil.rmtree(warmup, ignore_errors=True)
        except BaseException:
            self.close()
            raise

    # Reader threads own and close their pipes at EOF (process exit).
    def _pump(self):
        with self.process.stdout as replies:
            for line in replies:
                self.replies.put(line.decode("utf-8", errors="replace").strip())
        self.replies.put(None)

    def _drain_log(self):
        with self.process.stderr as log:
            _drain(log, self.log, lambda chunks: self.on_log and self.on_log(chunks))

    def _reply(self, timeout: float, cancel=None) -> str:
        deadline = time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                raise RoutingCancelled(CANCELLED)
            try:
                reply = self.replies.get(timeout=min(.1, max(0, deadline - time.monotonic())))
                break
            except Empty:
                if time.monotonic() >= deadline:
                    raise TimeoutError from None
        if reply is None:
            raise _WarmFailure("Warm router exited")
        return reply

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def run(self, args: list[str], timeout: float, cancel=None, on_log=None) -> str:
        """Submit one job; return the log tail. Raises TimeoutError, _WarmFailure or
        RoutingCancelled (the caller then kills this JVM)."""
        if any(ch in arg for arg in args for ch in "\t\r\n"):
            raise _WarmFailure("Argument cannot be sent to the warm router")
        del self.log[:]
        self.on_log = on_log
        try:
            self.process.stdin.write(("\t".join(args) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError as exc:
            raise _WarmFailure("Warm router pipe closed") from exc
        try:
            reply = self._reply(timeout, cancel)
        finally:
            self.on_log = None
        if reply != "VELATRACE_JOB COMPLETED":
            raise _WarmFailure(reply)
        return self.log.decode("utf-8", errors="replace")

    def close(self, kill: bool = False):
        atexit.unregister(self.close)
        if self.process is not None:
            if kill:
                self.process.kill()
            try:
                self.process.stdin.close()  # WarmRouter exits when stdin closes.
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait()
        if self.jar_lock is not None:
            self.jar_lock.close()
        shutil.rmtree(self.directory, ignore_errors=True)


class Freerouting:
    def __init__(self, jar: Path, java: str | Path = "java", *, work_directory: Path,
                 timeout_seconds: float = 300, warm: bool = False):
        """warm=True keeps one verified router JVM for the session (started by
        check_startup, restarted if it dies); any failure falls back to one-shot."""
        self.jar = local_path(jar)
        self.work_directory = local_path(work_directory)
        located = shutil.which(str(java))
        self.java = local_path(Path(located or java))
        if not 1 <= timeout_seconds <= 3600:
            raise ValidationError("Router timeout must be between 1 and 3600 seconds.")
        self.timeout = timeout_seconds
        self.last_log = ""
        self.warm = warm
        self._warm: _WarmRouter | None = None
        self._warm_lock = threading.Lock()
        self._warm_failures = 0
        self._closed = False
        # Set by the caller before each route: cancel (a threading.Event) stops the
        # router process, progress(message) receives "Routing · pass N" updates.
        self.cancel = threading.Event()
        self.progress = lambda message: None

    def _args(self, directory: Path) -> list[str]:
        return [str(self.java), "-Xmx1024m", "-Djava.security.manager",
                "-Djava.security.policy==" + str(directory / "offline.policy"),
                "-Djava.awt.headless=true", "-Djava.io.tmpdir=" + str(directory),
                "-Duser.home=" + str(directory)]

    def _prepare(self, directory: Path):
        (directory / "offline.policy").write_text(_policy(directory, self.jar), encoding="utf-8")
        probe = Path(__file__).parent / "router_resources" / "OfflineProbe.class"
        try:
            content = probe.read_bytes()
        except OSError:
            raise CapabilityError("Offline policy probe is missing; reinstall VelaTrace from a signed release.") from None
        if hashlib.sha256(content).hexdigest() != PROBE_SHA256:
            raise CapabilityError("Offline policy probe is missing or modified; reinstall VelaTrace from a signed release.")
        (directory / probe.name).write_bytes(content)
        result = run_bounded(self._args(directory) + ["-cp", str(directory), "OfflineProbe",
                             str(directory.parent / "must-not-write")], directory, 15)
        if result.returncode or "VELATRACE_OFFLINE_POLICY_OK" not in result.output:
            raise CapabilityError("Java network-denial policy verification failed. Install a supported Java 21 runtime; routing refused.")

    def _check_installation(self) -> None:
        if not self.jar.is_file():
            raise CapabilityError(f"Freerouting is missing. Download unmodified freerouting-{VERSION}.jar from {RELEASE_URL} and configure its path.")
        if self.jar.stat().st_size > 100_000_000 or hashlib.sha256(self.jar.read_bytes()).hexdigest() != JAR_SHA256:
            raise CapabilityError(f"Freerouting version/hash mismatch. Install exactly {VERSION} from {RELEASE_URL}; other JARs are refused.")
        if not self.java.is_file():
            raise CapabilityError("Java is missing. Install Eclipse Temurin Java 21 (JRE or JDK) and configure its bin/java executable.")
        self.work_directory.mkdir(parents=True, exist_ok=True)

    def _check_java(self, directory: Path) -> None:
        result = run_bounded([str(self.java), "-version"], directory, 15)
        if result.returncode or not re.search(r'version "21(?:\.|\")', result.output):
            raise CapabilityError("Java 21 is required for the verified offline policy. Java 24+ removed this mechanism; configure Java 21 explicitly.")

    def check_startup(self) -> None:
        self._check_installation()
        # Only clean up the directory this call creates. Other matching directories
        # may belong to another running router or contain files owned by the user.
        with tempfile.TemporaryDirectory(prefix="startup-", dir=self.work_directory, ignore_cleanup_errors=True) as name:
            directory = Path(name)
            self._check_java(directory)
            self._prepare(directory)
        if self.warm:
            # Pre-warm so the first route click does not pay JVM and router start-up.
            threading.Thread(target=self._prewarm, daemon=True, name="velatrace-router-prewarm").start()

    def close(self) -> None:
        """Stop the warm router JVM, if any. Idempotent; safe from any thread."""
        self._closed = True
        self._stop_warm()

    @staticmethod
    def _router_args(directory: Path, copied: Path, output: Path) -> list[str]:
        # The one-shot CLI and the warm launcher receive exactly the same router arguments.
        return ["-de", str(copied), "-do", str(output),
                "-da", "--gui.enabled=false", "--api_server.enabled=false",
                "--profile.allow_telemetry=false", "--feature_flags.save_jobs=false",
                "--user_data_path=" + str(directory), "-mp", "100", "-mt", "1"]

    @staticmethod
    def _read_ses(output: Path, dsn: DsnInput) -> str:
        if not output.is_file():
            raise CapabilityError("Freerouting failed or produced no SES. Inspect the local router log; nothing was applied.")
        if output.stat().st_size > MAX_SES:
            raise ValidationError("Freerouting SES exceeds the 32 MB limit; nothing was applied.")
        dsn.assert_unchanged()
        try:
            return output.read_text(encoding="utf-8")
        except UnicodeError:
            raise ValidationError("Freerouting output is not valid UTF-8; nothing was applied.") from None

    def _stop_warm(self, kill: bool = False) -> None:
        warm, self._warm = self._warm, None
        if warm is not None:
            warm.close(kill)

    def _ensure_warm(self) -> "_WarmRouter | None":
        """Caller holds _warm_lock. Returns a live warm router or None (use one-shot)."""
        if self._warm is not None and self._warm.alive():
            return self._warm
        self._stop_warm()
        if self._closed or self._warm_failures >= WARM_MAX_FAILURES:
            return None
        try:
            self._warm = _WarmRouter(self)
        except Exception:
            self._warm_failures += 1
            return None
        if self._closed:  # close() raced with start-up.
            self._stop_warm()
        return self._warm

    def _prewarm(self) -> None:
        with self._warm_lock:
            self._ensure_warm()

    def _route_warm(self, dsn: DsnInput, text: str, cancel) -> str | None:
        """Route in the warm JVM. None means use the one-shot path instead."""
        with self._warm_lock:
            warm = self._ensure_warm()
            if warm is None:
                return None
            if warm.jar_lock is None:
                # No OS lock pins the JAR bytes on this platform: re-verify before every job.
                try:
                    self._check_installation()
                except BaseException:
                    self._stop_warm()
                    raise
            with tempfile.TemporaryDirectory(prefix="job-", dir=warm.directory, ignore_cleanup_errors=True) as name:
                directory = Path(name)
                copied = directory / dsn.path.name
                copied.write_text(text, encoding="utf-8")
                output = directory / "result.ses"
                try:
                    self.last_log = warm.run(self._router_args(directory, copied, output), self.timeout,
                                             cancel, pass_reporter(self.progress))
                    if not output.is_file():
                        raise _WarmFailure("No SES")
                except RoutingCancelled:
                    # Killing the JVM is the only way to stop a job; warm up a fresh one.
                    self._stop_warm(kill=True)
                    threading.Thread(target=self._prewarm, daemon=True, name="velatrace-router-prewarm").start()
                    raise
                except TimeoutError:
                    self._stop_warm(kill=True)
                    raise CapabilityError(f"Freerouting/Java exceeded {self.timeout:g} seconds and was stopped; nothing applied.") from None
                except _WarmFailure:
                    self._warm_failures += 1
                    self._stop_warm(kill=True)
                    return None
                self._warm_failures = 0
                return self._read_ses(output, dsn)

    def route(self, dsn: DsnInput, constraints: tuple[Constraint, ...], *, electrical_rules=None) -> str:
        cancel = self.cancel
        if cancel.is_set():
            raise RoutingCancelled(CANCELLED)
        local_path(dsn.path)
        local_path(dsn.ticket.board_path)
        dsn.assert_unchanged()
        if not supports_constraints(constraints):
            raise CapabilityError("Some confirmed routing constraints cannot be enforced by this router.")
        if any(ch in dsn.path.name for ch in ('+', '\n', '\r')):
            raise ValidationError("DSN filename contains unsupported characters; export using a simple filename.")
        text = constrained_dsn(dsn.path.read_text(encoding="utf-8"), constraints)
        if electrical_rules is not None:
            # Dedicated classes are required: the pinned router ignores net-level
            # layer_rule. Compile only after global minima have been strengthened.
            from .electrical_rules import compile_electrical_dsn
            text = compile_electrical_dsn(text, electrical_rules)
        if self.warm and (ses := self._route_warm(dsn, text, cancel)) is not None:
            return ses
        if cancel.is_set():
            raise RoutingCancelled(CANCELLED)
        self._check_installation()
        with tempfile.TemporaryDirectory(prefix="route-", dir=self.work_directory, ignore_cleanup_errors=True) as name:
            directory = Path(name)
            self._check_java(directory)
            # Verify the policy in the exact directory used by this run. A second
            # startup-directory probe adds a JVM launch without additional proof.
            self._prepare(directory)
            copied = directory / dsn.path.name
            copied.write_text(text, encoding="utf-8")
            output = directory / "result.ses"
            args = self._args(directory) + ["-jar", str(self.jar)] + self._router_args(directory, copied, output)
            result = run_bounded(args, directory, self.timeout, cancel, pass_reporter(self.progress))
            self.last_log = result.output  # Local only; never transmitted or included in provider prompts.
            if result.returncode:
                raise CapabilityError("Freerouting failed or produced no SES. Inspect the local router log; nothing was applied.")
            return self._read_ses(output, dsn)
