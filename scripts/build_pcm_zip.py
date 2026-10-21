"""Build the KiCad Plugin and Content Manager (PCM) zip and the demo-board zip.

    python scripts/build_pcm_zip.py --tag v0.1.0-alpha.2 --pcm-version 0.1.0 [--out dist]

Files come from the git index (`git ls-files`, LF line endings whatever core.autocrlf
says), so untracked files are never shipped and two builds give the same bytes.
Standard library only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/F-Blaze/VelaTrace"
PLUGIN_FILES = ("plugin.json", "launch.py", "requirements.txt", "LICENSE")
PLUGIN_DIRS = ("icons/", "src/velatrace/")
DEMO_DIR = "examples/velatrace-demo/"
ICON = "icons/velatrace-64.png"
DOWNLOAD_KEYS = ("download_url", "download_sha256", "download_size", "install_size")


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def _is_junk(path: str) -> bool:
    return "__pycache__" in path.split("/") or path.endswith((".pyc", ".pyo"))


def _tracked() -> list[str]:
    return sorted(p for p in _git("ls-files", "-z").decode().split("\0") if p and not _is_junk(p))


def _plugin_files(tracked: list[str]) -> list[str]:
    return [p for p in tracked if p in PLUGIN_FILES or p.startswith(PLUGIN_DIRS)]


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    """Sorted entries, fixed time stamp and permissions: the same input gives the same bytes."""
    epoch = max(int(os.environ.get("SOURCE_DATE_EPOCH", "315532800")), 315532800)  # zip floor: 1980
    stamp = time.gmtime(epoch)[:6]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type, info.create_system, info.external_attr = zipfile.ZIP_DEFLATED, 3, 0o100644 << 16
            archive.writestr(info, entries[name], compresslevel=9)


def _digest(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def build(tag: str, pcm_version: str, out: Path, allow_dirty: bool = False) -> dict:
    tracked = _tracked()
    plugin = _plugin_files(tracked)
    demo = [p for p in tracked if p.startswith(DEMO_DIR)]
    shipped = [*plugin, *demo, "pcm/metadata.json"]
    if not allow_dirty:
        dirty = _git("status", "--porcelain", "--", *shipped).decode().strip()
        if dirty:
            sys.exit(f"Uncommitted changes to shipped files (commit them or use --allow-dirty):\n{dirty}")
    if ICON not in tracked or "pcm/metadata.json" not in tracked:
        sys.exit(f"{ICON} and pcm/metadata.json must be tracked by git (git add them)")

    def blob(path: str) -> bytes:
        return _git("show", ":" + path)

    repo_meta = json.loads(blob("pcm/metadata.json"))
    entry = next((v for v in repo_meta["versions"] if v["version"] == pcm_version), None)
    if entry is None:
        sys.exit(f"pcm/metadata.json has no version {pcm_version}")
    zip_name = f"VelaTrace-{tag}-pcm.zip"
    out.mkdir(parents=True, exist_ok=True)

    entries = {"plugins/" + p: blob(p) for p in plugin}
    entries["resources/icon.png"] = blob(ICON)
    package_meta = dict(repo_meta, versions=[{k: v for k, v in entry.items() if k not in DOWNLOAD_KEYS}])
    entries["metadata.json"] = (json.dumps(package_meta, indent=2) + "\n").encode()
    pcm_zip = out / zip_name
    _write_zip(pcm_zip, entries)
    sha, size = _digest(pcm_zip)
    install_size = sum(len(data) for data in entries.values())

    entry.update(download_url=f"{REPO_URL}/releases/download/{tag}/{zip_name}", download_sha256=sha,
                 download_size=size, install_size=install_size)
    (out / "metadata-repo.json").write_text(json.dumps(repo_meta, indent=2) + "\n", encoding="utf-8")

    demo_zip = out / f"velatrace-demo-{tag}.zip"
    _write_zip(demo_zip, {"velatrace-demo/" + p[len(DEMO_DIR):]: blob(p) for p in demo})
    demo_sha, demo_size = _digest(demo_zip)
    return {"pcm_zip": pcm_zip, "sha256": sha, "download_size": size, "install_size": install_size,
            "demo_zip": demo_zip, "demo_sha256": demo_sha, "demo_size": demo_size}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True, help="release tag, e.g. v0.1.0-alpha.2")
    parser.add_argument("--pcm-version", required=True, help="version entry in pcm/metadata.json, e.g. 0.1.0")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--allow-dirty", action="store_true", help="skip the uncommitted-changes check")
    args = parser.parse_args(argv)
    result = build(args.tag, args.pcm_version, args.out, args.allow_dirty)
    print(f"{result['pcm_zip']}\n  sha256        {result['sha256']}\n  download_size {result['download_size']}\n"
          f"  install_size  {result['install_size']}\n{result['demo_zip']}\n"
          f"  sha256        {result['demo_sha256']}\n  size          {result['demo_size']}\n"
          f"{args.out / 'metadata-repo.json'}")


if __name__ == "__main__":
    main()
