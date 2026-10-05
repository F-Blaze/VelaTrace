"""KiCad IPC entrypoint for a verified local source checkout.

KiCad supplies KICAD_API_SOCKET and KICAD_API_TOKEN to this external process.
No shell, package installation, source update or board mutation happens here.
"""
import os
from pathlib import Path
import sys


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("VelaTrace requires Python 3.11 or newer. Configure KiCad's plugin interpreter.")
    # Whatever folder KiCad starts this process in (possibly a downloaded project),
    # nothing is looked up there: programs and modules come from known places only.
    os.chdir(Path(__file__).resolve().parent)
    source = Path(__file__).resolve().parent / "src"
    if not (source / "velatrace" / "__main__.py").is_file():
        raise SystemExit("VelaTrace source is incomplete. Restore the verified release checkout.")
    sys.path.insert(0, str(source))
    from velatrace.__main__ import main as run
    return run()


if __name__ == "__main__":
    main()
