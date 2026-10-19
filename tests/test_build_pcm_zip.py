import hashlib
import importlib.util
import json
from pathlib import Path
import re
import struct
import zipfile

_SPEC = importlib.util.spec_from_file_location(
    "build_pcm_zip", Path(__file__).resolve().parent.parent / "scripts" / "build_pcm_zip.py")
build_pcm_zip = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_pcm_zip)


def test_pcm_zip_layout_is_valid_and_reproducible(tmp_path):
    first = build_pcm_zip.build("v0.0.0-test", "0.1.0", tmp_path / "a", allow_dirty=True)
    second = build_pcm_zip.build("v0.0.0-test", "0.1.0", tmp_path / "b", allow_dirty=True)
    assert first["sha256"] == second["sha256"]
    assert hashlib.sha256(first["pcm_zip"].read_bytes()).hexdigest() == first["sha256"]
    assert first["demo_sha256"] == second["demo_sha256"]

    with zipfile.ZipFile(first["pcm_zip"]) as archive:
        names = archive.namelist()
        meta = json.loads(archive.read("metadata.json"))
        icon = archive.read("resources/icon.png")
    assert "plugins/plugin.json" in names and "plugins/src/velatrace/__init__.py" in names
    assert not [n for n in names if "tests/" in n or "__pycache__" in n or n.endswith(".pyc")]
    assert struct.unpack(">II", icon[16:24]) == (64, 64)  # PNG IHDR width, height

    (version,) = meta["versions"]
    assert not set(version) & set(build_pcm_zip.DOWNLOAD_KEYS)
    assert re.fullmatch(r"\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?", version["version"])
    assert len(meta["description"]) <= 150 and len(meta["description_full"]) <= 5000
    assert re.fullmatch(r"[a-zA-Z][-a-zA-Z0-9.]{0,98}[a-zA-Z0-9]", meta["identifier"])
    assert all(re.fullmatch(r"[a-z][-a-z0-9]{0,48}[a-z0-9]", tag) for tag in meta["tags"])

    repo_version = json.loads((tmp_path / "a" / "metadata-repo.json").read_text())["versions"][0]
    assert repo_version["download_sha256"] == first["sha256"]
    assert repo_version["download_url"].endswith("/v0.0.0-test/VelaTrace-v0.0.0-test-pcm.zip")
