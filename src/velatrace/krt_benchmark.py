"""Pinned external KRT comparator, restricted to VelaTrace-authored fixtures.

This is intentionally not a production board backend. It executes a separate
Python/Rust tool with no provider environment, Python networking or subprocesses.
Those restrictions are not an OS sandbox for native code. Never pass user boards.
"""
import hashlib
import math
from pathlib import Path
import sys
import tempfile
import time

from .candidate import project_context, context_matches, trusted_via_catalog
from .dsn import file_digest
from .errors import CapabilityError, ValidationError
from .freerouting import local_path, run_bounded


KRT_VERSION = "0.22.1"
KRT_COMMIT = "023d3f79027d5406e4ea8e68291f588c5c135673"
KRT_SOURCE_SHA256 = "6a62a41265e7023f04a1ed1be8e3dbefcd0a8398726a9c50988c01c9e50645f9"
KRT_WINDOWS_SHA256 = "06d127d55c4edfa5135b80ac5c6686d9e0a3d0d1d2c167e58a8972198fc97baa"


def source_digest(root: Path) -> str:
    """Hash all Python source and version declarations; refuse bytecode shadows."""
    for path in root.rglob('*'):
        if path.suffix.lower() in {'.pyc', '.pyo', '.pyd', '.so', '.dll'}:
            relative = path.relative_to(root).as_posix()
            if relative not in {'rust_router/grid_router.pyd', 'rust_router/grid_router-windows-x86_64.pyd'}:
                raise CapabilityError('KRT checkout contains unexpected bytecode/native modules; use a fresh checkout.')
            if path.is_symlink() or file_digest(path) != KRT_WINDOWS_SHA256:
                raise CapabilityError('KRT native module does not match the pinned release asset.')
    files = sorted([*root.rglob('*.py'), root/'rust_router/Cargo.toml', root/'VERSION'],
                   key=lambda path: path.relative_to(root).as_posix())
    result = hashlib.sha256()
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise CapabilityError("KRT source must not contain linked Python files.")
        content = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
        result.update(path.relative_to(root).as_posix().encode() + b'\0' + content.encode() + b'\n')
    return result.hexdigest()


def _fixture_only(dsn):
    from .benchmark import fixture_texts
    _, context = project_context(dsn.ticket.board_path)
    expected_hashes = {**{path: digest for path, digest in context.items() if digest is not None},
                       dsn.ticket.board_path: dsn.ticket.board_digest, dsn.path: dsn.digest}
    captured = {path: path.read_bytes() for path in expected_hashes}
    if any(hashlib.sha256(data).hexdigest() != expected_hashes[path] for path, data in captured.items()):
        raise ValidationError('Benchmark input changed while capturing its synthetic fixture.')
    actual = {path.suffix: data.decode('utf-8').replace('\r\n', '\n') for path, data in captured.items()}
    for layers in (4, 6, 8):
        for dense in (False, True):
            for obstacles in (False, True):
                expected = {Path(name).suffix: value for name, value in
                            fixture_texts(layers, dense=dense, obstacles=obstacles).items()}
                if actual == expected:
                    return context, captured
    raise ValidationError("KRT comparator accepts only exact authored benchmark fixtures, never user boards.")


class KrtBenchmarkBackend:
    def __init__(self, root: Path, python: Path, directory: Path):
        self.root, self.python, self.directory = map(local_path, (root, python, directory))
        self.last_log = ''
        self.last_board = ''
        self.directory.mkdir(parents=True, exist_ok=True)

    def check_startup(self):
        if sys.platform != 'win32':
            raise CapabilityError("This experimental KRT binary pin currently supports Windows x86_64 only.")
        try:
            if source_digest(self.root) != KRT_SOURCE_SHA256:
                raise CapabilityError("KRT source does not match the pinned 0.22.1 checkout.")
            binary = self.root/'rust_router/grid_router.pyd'
            if file_digest(binary) != KRT_WINDOWS_SHA256:
                raise CapabilityError("KRT Windows binary does not match the pinned release asset.")
            if not self.python.is_file():
                raise CapabilityError("Configure the isolated KRT Python executable.")
        except OSError as exc:
            raise CapabilityError("KRT files are missing; follow the experimental backend setup instructions.") from exc
        result = run_bounded(self._command('--help'), self.directory, 60)
        if result.returncode:
            raise CapabilityError("KRT startup failed: " + result.output[-3000:])
        return {'version': KRT_VERSION, 'commit': KRT_COMMIT, 'source_sha256': KRT_SOURCE_SHA256,
                'binary_sha256': KRT_WINDOWS_SHA256, 'python_sha256': file_digest(self.python),
                'launcher_sha256': file_digest(Path(__file__).parent/'router_resources/krt_runner.py'),
                'dependencies': {'numpy': '2.2.6', 'scipy': '1.15.3', 'shapely': '2.1.1'},
                'scope': 'authored synthetic fixtures only; no production IPC integration'}

    def _command(self, *args):
        launcher = Path(__file__).parent/'router_resources/krt_runner.py'
        return [str(self.python), '-I', '-B', str(launcher), str(self.root/'py_router/route.py'), *args]

    def route(self, dsn, *, timeout_seconds):
        from .external_copper import import_copper
        if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValidationError("KRT needs a finite positive process budget.")
        started = time.monotonic()
        dsn.assert_unchanged()
        context, captured = _fixture_only(dsn)
        source = captured[dsn.ticket.board_path].decode('utf-8')
        # Recheck executable source for each run, not only startup.
        if (source_digest(self.root) != KRT_SOURCE_SHA256
                or file_digest(self.root/'rust_router/grid_router.pyd') != KRT_WINDOWS_SHA256):
            raise CapabilityError("KRT changed since startup; nothing was accepted.")
        self.last_log, self.last_board = '', ''
        with tempfile.TemporaryDirectory(prefix='krt-', dir=self.directory, ignore_cleanup_errors=True) as name:
            folder = Path(name)
            board = folder/'crossed.kicad_pcb'
            for path, data in captured.items():
                (folder/path.name).write_bytes(data)
            before = {path.name: file_digest(path) for path in folder.iterdir() if path.is_file()}
            output = folder/'routed.kicad_pcb'
            # No width/clearance ceiling or pin-swapping options. The original
            # project's rules remain authoritative in the independent validator.
            args = self._command(str(board), str(output), '--no-fix-drc-settings',
                                 '--no-stub-layer-swap', '--nets', *sorted(dsn.nets))
            dsn.assert_unchanged()
            if not context_matches(context):
                raise ValidationError('Benchmark project changed before KRT launched.')
            remaining = timeout_seconds - (time.monotonic()-started)
            if remaining <= 0:
                raise TimeoutError('KRT budget expired during input/integrity checks.')
            result = run_bounded(args, folder, remaining)
            self.last_log = result.output
            dsn.assert_unchanged()
            if not context_matches(context):
                raise ValidationError("Benchmark project changed while KRT was running.")
            if any(not (folder/name).is_file() or file_digest(folder/name) != digest
                   for name, digest in before.items()):
                raise ValidationError("KRT modified a staged source or its design rules; candidate refused.")
            if result.returncode or not output.is_file():
                raise CapabilityError("KRT failed or produced no board; inspect its local benchmark log.")
            if output.stat().st_size > 32_000_000:
                raise ValidationError("KRT output exceeds the supported 32 MB limit.")
            self.last_board = output.read_text(encoding='utf-8')
            return import_copper(source, self.last_board, dsn, via_catalog=trusted_via_catalog(dsn))
