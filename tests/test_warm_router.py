"""Warm (reused-JVM) Freerouting: lifecycle, fallback and pin checks."""
from pathlib import Path
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from velatrace import freerouting
from velatrace.dsn import DsnInput, ExportTicket, file_digest
from velatrace.errors import CapabilityError
from velatrace.freerouting import Freerouting, ProcessResult, WARM_SHA256
from velatrace.sexpr import parse

FIXTURES = Path(__file__).parent / "fixtures" / "routing"


class FakeWarm:
    """Stands in for _WarmRouter; `mode` scripts what the next job does."""
    started: list = []
    mode = "ok"
    after_stall = "stall"
    stopped = False

    def __init__(self, owner, warmup=True):
        if FakeWarm.mode == "start-fails":
            raise freerouting._WarmFailure("start")
        owner.work_directory.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="warm-", dir=owner.work_directory))
        self.jar_lock, self.closed, self.jobs, self.sent = object(), None, 0, []
        FakeWarm.started.append(self)

    def alive(self):
        return self.closed is None

    def run(self, args, timeout, cancel=None, on_log=None, should_stop=None):
        self.jobs += 1
        self.stopped = False
        self.sent.append(args)
        if FakeWarm.mode == "crash":
            raise freerouting._WarmFailure("crash")
        if FakeWarm.mode == "hang":
            raise TimeoutError
        if FakeWarm.mode == "cancel":
            cancel.set()  # The user clicks Cancel mid-job.
            raise freerouting.RoutingCancelled("Cancelled")
        if on_log is not None and FakeWarm.mode != "stall":
            on_log(bytearray(b"Auto-router pass #2 on board 'x' was completed in 1 seconds (4 unrouted).\n"))
            on_log(bytearray(b"Auto-router pass #3 on board 'x' was completed in 1 seconds.\n"))
        if FakeWarm.mode == "stall":  # The launcher writes the partial route when asked to stop.
            for number in range(1, 40):
                on_log(bytearray(b"Auto-router pass #%d on board 'x' was completed in 1 seconds "
                                 b"(3 unrouted and 2 violations).\n" % number))
                if should_stop():
                    self.stopped = number
                    FakeWarm.mode = FakeWarm.after_stall
                    break
        Path(args[args.index("-do") + 1]).write_text("warm result")
        return "warm log"

    def close(self, kill=False):
        self.closed = kill
        shutil.rmtree(self.directory, ignore_errors=True)


def one_shot(args, directory, timeout, cancel=None, on_data=None):
    if "-version" in args:
        return ProcessResult(0, 'version "21.0.1"')
    if "OfflineProbe" in args:
        return ProcessResult(0, "VELATRACE_OFFLINE_POLICY_OK")
    (directory / "result.ses").write_text("one-shot result")
    return ProcessResult(0, "")


class WarmLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.jar = self.root / "router.jar"
        self.jar.write_bytes(b"fixture jar")
        board = self.root / "simple.kicad_pcb"
        board.write_text("fixture board")
        path = self.root / "simple.dsn"
        path.write_text((FIXTURES / "simple.dsn").read_text())
        self.dsn = DsnInput(path, file_digest(path), ExportTicket.begin(board), frozenset({"N"}), frozenset({"F.Cu", "B.Cu"}))
        FakeWarm.started, FakeWarm.mode = [], "ok"
        for patcher in (patch.object(freerouting, "_WarmRouter", FakeWarm),
                        patch.object(freerouting, "run_bounded", side_effect=one_shot),
                        patch.object(freerouting, "JAR_SHA256", hashlib.sha256(b"fixture jar").hexdigest()),
                        patch.object(Freerouting, "_prepare")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.router = Freerouting(self.jar, sys.executable, work_directory=self.root / "work", warm=True)
        self.addCleanup(self.router.close)

    def test_prewarm_on_startup_reuse_restart_and_close(self):
        self.router.check_startup()
        with self.router._warm_lock:  # Wait for the background pre-warm.
            pass
        self.assertEqual(len(FakeWarm.started), 1)
        self.assertEqual(self.router.route(self.dsn, ()), "warm result")
        self.assertEqual(self.router.route(self.dsn, ()), "warm result")
        self.assertEqual((len(FakeWarm.started), FakeWarm.started[0].jobs), (1, 2))
        FakeWarm.started[0].closed = True  # JVM died while idle: restarted transparently.
        self.assertEqual(self.router.route(self.dsn, ()), "warm result")
        self.assertEqual(len(FakeWarm.started), 2)
        self.router.close()
        self.assertIsNotNone(FakeWarm.started[1].closed)
        self.assertEqual(self.router.route(self.dsn, ()), "one-shot result")  # Closed: never restarts.
        self.assertEqual(len(FakeWarm.started), 2)
        self.assertFalse(list((self.root / "work").iterdir()))

    def test_warm_failure_falls_back_to_one_shot_then_stops_retrying(self):
        FakeWarm.mode = "crash"
        for attempt in range(3):
            self.assertEqual(self.router.route(self.dsn, ()), "one-shot result")
        self.assertEqual(len(FakeWarm.started), freerouting.WARM_MAX_FAILURES)
        self.assertTrue(all(warm.closed is True for warm in FakeWarm.started))  # Killed, not left running.
        self.assertFalse(list((self.root / "work").iterdir()))

    def test_start_failure_falls_back(self):
        FakeWarm.mode = "start-fails"
        self.assertEqual(self.router.route(self.dsn, ()), "one-shot result")

    def test_timeout_kills_warm_jvm_and_does_not_rerun(self):
        FakeWarm.mode = "hang"
        with patch.object(freerouting, "run_bounded") as run, \
                self.assertRaisesRegex(CapabilityError, "exceeded"):
            self.router.route(self.dsn, ())
        run.assert_not_called()
        self.assertIs(FakeWarm.started[0].closed, True)

    def test_cancel_kills_warm_jvm_never_falls_back_and_rewarms(self):
        FakeWarm.mode = "cancel"
        with patch.object(freerouting, "run_bounded") as run, \
                self.assertRaises(freerouting.RoutingCancelled):
            self.router.route(self.dsn, ())
        run.assert_not_called()  # No one-shot rerun of a cancelled job.
        self.assertIs(FakeWarm.started[0].closed, True)
        self.assertEqual(self.router._warm_failures, 0)
        # The next route is refused until the caller supplies a fresh cancel event.
        with self.assertRaises(freerouting.RoutingCancelled):
            self.router.route(self.dsn, ())
        FakeWarm.mode = "ok"
        self.router.cancel = threading.Event()
        seen = []
        self.router.progress = seen.append
        self.assertEqual(self.router.route(self.dsn, ()), "warm result")
        self.assertEqual(seen, ["Routing · pass 2 · 4 unrouted", "Routing · pass 3"])

    def test_unlocked_jar_is_rehashed_before_every_warm_job(self):
        self.assertEqual(self.router.route(self.dsn, ()), "warm result")
        FakeWarm.started[0].jar_lock = None  # e.g. no OS lock off Windows
        self.jar.write_bytes(b"swapped jar")
        with self.assertRaisesRegex(CapabilityError, "hash mismatch"):
            self.router.route(self.dsn, ())
        self.assertEqual(FakeWarm.started[0].jobs, 1)
        self.assertIsNotNone(FakeWarm.started[0].closed)

    def test_one_shot_and_warm_send_identical_router_arguments(self):
        self.router.route(self.dsn, ())
        sent = FakeWarm.started[0].sent[0]
        directory = Path(sent[sent.index("-de") + 1]).parent
        self.assertEqual(sent, Freerouting._router_args(directory, directory / "simple.dsn", directory / "result.ses"))


    def test_stalled_router_is_stopped_and_its_partial_route_kept(self):
        FakeWarm.mode = "stall"
        with patch.object(freerouting, "RETRY_FRACTION", 0):  # No budget left for a retry.
            self.assertEqual(self.router.route(self.dsn, ()), "warm result")
        # Best count (3 unrouted) first seen at pass 1; stopped STALL_PASSES later.
        self.assertEqual(FakeWarm.started[0].stopped, 1 + freerouting.STALL_PASSES)
        self.assertEqual((self.router.stopped_early, FakeWarm.started[0].jobs), (True, 1))

    def test_stalled_route_is_retried_within_the_budget_and_the_complete_attempt_wins(self):
        FakeWarm.mode, FakeWarm.after_stall = "stall", "ok"
        self.addCleanup(setattr, FakeWarm, "after_stall", "stall")
        seen = []
        self.router.progress = seen.append
        self.assertEqual(self.router.route(self.dsn, ()), "warm result")
        self.assertEqual((self.router.stopped_early, FakeWarm.started[0].jobs), (False, 2))
        self.assertIn("Routing again · best so far 3 unrouted", seen)

    def test_without_warm_setting_each_route_uses_and_closes_one_launcher(self):
        router = Freerouting(self.jar, sys.executable, work_directory=self.root / "work2")
        router.check_startup()
        self.assertEqual(FakeWarm.started, [])  # No pre-warm.
        for count in (1, 2):
            self.assertEqual(router.route(self.dsn, ()), "warm result")
            self.assertEqual(len(FakeWarm.started), count)
            self.assertIsNotNone(FakeWarm.started[-1].closed)

    def test_route_budget_scales_with_board_size_and_leaves_stop_grace(self):
        self.assertEqual(freerouting.route_budget(4, 300), 60)
        self.assertEqual(freerouting.route_budget(40, 300), 150)
        self.assertEqual(freerouting.route_budget(500, 300), 300 - freerouting.STOP_GRACE_SECONDS)
        self.assertEqual(freerouting.route_budget(4, 10), 1)


class WarmStartupPinTests(unittest.TestCase):
    def test_packaged_warm_launcher_hash(self):
        launcher = Path(freerouting.__file__).parent / "router_resources" / "WarmRouter.class"
        self.assertEqual(hashlib.sha256(launcher.read_bytes()).hexdigest(), WARM_SHA256)

    def test_jar_hash_is_verified_before_any_java_process(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            jar = root / "router.jar"
            jar.write_bytes(b"not the pinned jar")
            router = Freerouting(jar, sys.executable, work_directory=root / "work", warm=True)
            with patch.object(freerouting.subprocess, "Popen") as popen, \
                    patch.object(freerouting, "run_bounded") as run:
                router._prewarm()
            popen.assert_not_called()
            run.assert_not_called()
            self.assertIsNone(router._warm)
            self.assertEqual(router._warm_failures, 1)
            self.assertFalse(list((root / "work").iterdir()))


@unittest.skipUnless(os.environ.get("VELATRACE_TEST_JAR") and os.environ.get("VELATRACE_TEST_JAVA"),
                     "Set VELATRACE_TEST_JAR and VELATRACE_TEST_JAVA for the real warm-router test")
class NativeWarmRouterTests(unittest.TestCase):
    @staticmethod
    def copper(ses):
        """Undirected wire segments and vias: Freerouting 2.1.0 varies point order run to run."""
        found = []
        def walk(node):
            if isinstance(node, list) and node:
                if node[0] == "path":
                    points = [tuple(node[i:i + 2]) for i in range(3, len(node) - 1, 2)]
                    found.extend((node[1], node[2]) + tuple(sorted(pair)) for pair in zip(points, points[1:]))
                elif node[0] == "via":
                    found.append(tuple(node[1:4]))
                for child in node[1:]:
                    walk(child)
        walk(parse(ses))
        return sorted(found)

    def test_real_warm_router_matches_one_shot_restarts_and_exits(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            board = root / "simple-test.kicad_pcb"
            board.write_text("Adapter-only fixture: not a candidate PCB or DRC claim.")
            path = root / "simple-test.dsn"
            path.write_text((FIXTURES / "simple.dsn").read_text())
            dsn = DsnInput(path, file_digest(path), ExportTicket.begin(board), frozenset({"N"}), frozenset({"F.Cu", "B.Cu"}))
            jar, java = Path(os.environ["VELATRACE_TEST_JAR"]), os.environ["VELATRACE_TEST_JAVA"]
            reference = Freerouting(jar, java, work_directory=root / "one", timeout_seconds=60).route(dsn, ())
            router = Freerouting(jar, java, work_directory=root / "warm", timeout_seconds=60, warm=True)
            try:
                router.check_startup()
                for _ in range(2):
                    self.assertEqual(self.copper(router.route(dsn, ())), self.copper(reference))
                warm = router._warm
                self.assertTrue(warm.alive())
                self.assertIn("-Djava.security.manager", warm.process.args)
                if os.name == "nt":  # Verified JAR bytes cannot change under the live JVM.
                    with self.assertRaises(OSError):
                        open(jar, "r+b").close()
                warm.process.kill()
                warm.process.wait()
                self.assertEqual(self.copper(router.route(dsn, ())), self.copper(reference))
                self.assertIsNot(router._warm, warm)
                process = router._warm.process
            finally:
                router.close()
            self.assertIsNotNone(process.poll())
            self.assertFalse(any((root / "warm").iterdir()))


if __name__ == "__main__":
    unittest.main()
