"""Pinned, offline external Freerouting process. Never writes a user's board.

Java 21's process-local security policy denies Java networking. This is not an
OS sandbox for hostile native code: the immutable official JAR is a trust anchor.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

from .constraints import Constraint
from .dsn import DsnInput, dsn_scale
from .errors import CapabilityError, ValidationError
from .ses import number
from .sexpr import QuotedAtom, one, parse

VERSION = "2.1.0"
JAR_SHA256 = "2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def"
RELEASE_URL = "https://github.com/freerouting/freerouting/releases/tag/v2.1.0"
PROBE_SHA256 = "c27481d8f2e0505ec21b8ba375888343dfcc06406d9e62e4ab6c7c63929ef6de"
MAX_SES = 32_000_000


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


def run_bounded(args: list[str], directory: Path, timeout: float) -> ProcessResult:
    """Drain output continuously, retain only final 64 KiB, kill on timeout."""
    chunks = bytearray()
    try:
        process = subprocess.Popen(args, cwd=directory, env=clean_environment(directory),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise CapabilityError("Cannot start Java. Install Java 21 and configure its executable path.") from exc
    def drain():
        while data := process.stdout.read(4096):
            chunks.extend(data)
            if len(chunks) > 65_536:
                del chunks[:-65_536]
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise CapabilityError(f"Freerouting/Java exceeded {timeout:g} seconds and was stopped; nothing applied.") from None
    finally:
        reader.join(timeout=5)
        process.stdout.close()
    return ProcessResult(process.returncode, chunks.decode("utf-8", errors="replace"))


def supports_constraints(constraints: tuple[Constraint, ...]) -> bool:
    return all(c.kind in {"clearance", "trace-width"} and c.target == "all nets" for c in constraints)


def _serialize(node: list) -> str:
    def atom(value):
        if isinstance(value, list):
            return _serialize(value)
        if value and not isinstance(value, QuotedAtom) and not re.search(r'[\s()"\\]', value):
            return value
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
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


class Freerouting:
    def __init__(self, jar: Path, java: str | Path = "java", *, work_directory: Path,
                 timeout_seconds: float = 300):
        self.jar = local_path(jar)
        self.work_directory = local_path(work_directory)
        located = shutil.which(str(java))
        self.java = local_path(Path(located or java))
        if not 1 <= timeout_seconds <= 3600:
            raise ValidationError("Router timeout must be between 1 and 3600 seconds.")
        self.timeout = timeout_seconds
        self.last_log = ""

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

    def check_startup(self) -> None:
        if not self.jar.is_file():
            raise CapabilityError(f"Freerouting is missing. Download unmodified freerouting-{VERSION}.jar from {RELEASE_URL} and configure its path.")
        if self.jar.stat().st_size > 100_000_000 or hashlib.sha256(self.jar.read_bytes()).hexdigest() != JAR_SHA256:
            raise CapabilityError(f"Freerouting version/hash mismatch. Install exactly {VERSION} from {RELEASE_URL}; other JARs are refused.")
        if not self.java.is_file():
            raise CapabilityError("Java is missing. Install Eclipse Temurin Java 21 (JRE or JDK) and configure its bin/java executable.")
        self.work_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="startup-", dir=self.work_directory) as name:
            directory = Path(name)
            result = run_bounded([str(self.java), "-version"], directory, 15)
            if result.returncode or not re.search(r'version "21(?:\.|\")', result.output):
                raise CapabilityError("Java 21 is required for the verified offline policy. Java 24+ removed this mechanism; configure Java 21 explicitly.")
            self._prepare(directory)

    def route(self, dsn: DsnInput, constraints: tuple[Constraint, ...]) -> str:
        self.check_startup()
        local_path(dsn.path)
        local_path(dsn.ticket.board_path)
        dsn.assert_unchanged()
        if not supports_constraints(constraints):
            raise CapabilityError("Some confirmed routing constraints cannot be enforced by this router.")
        if any(ch in dsn.path.name for ch in ('+', '\n', '\r')):
            raise ValidationError("DSN filename contains unsupported characters; export using a simple filename.")
        text = constrained_dsn(dsn.path.read_text(encoding="utf-8"), constraints)
        with tempfile.TemporaryDirectory(prefix="route-", dir=self.work_directory) as name:
            directory = Path(name)
            self._prepare(directory)
            copied = directory / dsn.path.name
            copied.write_text(text, encoding="utf-8")
            output = directory / "result.ses"
            args = self._args(directory) + ["-jar", str(self.jar), "-de", str(copied), "-do", str(output),
                    "-da", "-dl", "--gui.enabled=false", "--api_server.enabled=false",
                    "--profile.allow_telemetry=false", "--feature_flags.save_jobs=false",
                    "--user_data_path=" + str(directory), "-mp", "100", "-mt", "1"]
            result = run_bounded(args, directory, self.timeout)
            self.last_log = result.output  # Local only; never transmitted or included in provider prompts.
            if result.returncode or not output.is_file():
                raise CapabilityError("Freerouting failed or produced no SES. Inspect the local router log; nothing was applied.")
            if output.stat().st_size > MAX_SES:
                raise ValidationError("Freerouting SES exceeds the 32 MB limit; nothing was applied.")
            dsn.assert_unchanged()
            try:
                return output.read_text(encoding="utf-8")
            except UnicodeError:
                raise ValidationError("Freerouting output is not valid UTF-8; nothing was applied.") from None
