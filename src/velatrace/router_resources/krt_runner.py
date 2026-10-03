"""Research-only launcher for the separately installed MIT KRT backend.

No upstream code is imported into VelaTrace's process. Audit hooks stop Python
networking and child processes, but are not an OS sandbox for native extensions.
Only the synthetic benchmark uses this launcher.
"""
import importlib.metadata
import os
from pathlib import Path
import runpy
import sys


def deny_external(event, args):
    if (event.startswith("socket.") or event.startswith("subprocess.")
            or event in {"os.system", "os.posix_spawn", "os.posix_spawnp", "os.spawn"}):
        raise PermissionError("KRT benchmark disables networking and child processes.")


def main():
    # Bound numerical-library parallelism before importing NumPy/SciPy.
    for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[name] = '1'
    for name, expected in (("numpy", "2.2.6"), ("scipy", "1.15.3"), ("shapely", "2.1.1")):
        if importlib.metadata.version(name) != expected:
            raise RuntimeError(f"KRT benchmark requires {name}=={expected}.")
    entry = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(entry.parent))
    sys.argv = sys.argv[1:]
    sys.addaudithook(deny_external)
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
