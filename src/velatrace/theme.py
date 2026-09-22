"""Read-only best-effort KiCad appearance selection; no IPC theme getter exists.

KiCad's common_settings.cpp uses appearance.app_theme on Windows; APP_THEME in
include/settings/common_settings.h is LIGHT=0, DARK=1, AUTO=2. Other platforms
follow their system theme. Unknown settings always defer to the system/manual UI.
Sources: https://gitlab.com/kicad/code/kicad/-/blob/master/common/settings/common_settings.cpp
https://gitlab.com/kicad/code/kicad/-/blob/master/include/settings/common_settings.h
"""
import json
import os
from pathlib import Path
import sys


def saved_kicad_theme(major: int, minor: int = 0, *, config_root=None,
                      platform: str | None = None) -> str | None:
    """Return 'Light', 'Dark', or None (automatic/unknown); never guess a version."""
    if (type(major) is not int or type(minor) is not int or major < 9 or minor < 0
            or (platform or sys.platform) != "win32"):
        return None
    root = config_root or os.environ.get("KICAD_CONFIG_HOME")
    if root is None:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        root = Path(appdata) / "kicad"
    path = Path(root) / f"{major}.{minor}" / "kicad_common.json"
    try:
        if path.stat().st_size > 1_000_000:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))["appearance"]["app_theme"]
        return {0: "Light", 1: "Dark"}.get(value) if type(value) is int else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
