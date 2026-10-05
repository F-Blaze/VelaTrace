"""Optional JLCPCB Basic/Preferred parts list: free, keyless, user-initiated, cached, offline.

Nothing here touches the network on import, at audit time or on load. download_catalogue()
is the only network call and must be triggered by an explicit user action. It sends no board
data: one HTTPS GET to a fixed allow-listed URL, no redirects, bounded size and time. The
payload is sanity-checked (it must look like a complete Basic list), then cached with its
SHA-256 under the plugin config folder. load_catalogue() verifies that hash and works offline;
when no cache exists it returns None and the JLCPCB checks are simply skipped.
"""
from __future__ import annotations

from collections import defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import http.client
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import threading
from time import monotonic
from typing import Callable, Iterable
from urllib.parse import urlsplit

from .bom import SI_PREFIX
from .errors import ValidationError
from .network import connect_before


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    url: str
    homepage: str
    license: str
    cadence: str
    adapter: str


# Both are static CSV files published by open-source GitHub Actions jobs on GitHub Pages.
# See docs/bom.md for size, cadence and licensing notes (checked 2026-09-30).
SOURCES = {
    "lrks-economic": Source(
        "lrks-economic", "lrks/jlcpcb-economic-parts",
        "https://lrks.github.io/jlcpcb-economic-parts/economic-parts.csv",
        "https://github.com/lrks/jlcpcb-economic-parts",
        "no license file in the repository; data is JLCPCB's public catalogue", "weekly", "lrks"),
    "cdfer-basic-preferred": Source(
        "cdfer-basic-preferred", "CDFER/jlcpcb-parts-database",
        "https://cdfer.github.io/jlcpcb-parts-database/jlcpcb-components-basic-preferred.csv",
        "https://github.com/CDFER/jlcpcb-parts-database",
        "MIT (repository); data is JLCPCB's public catalogue", "daily", "cdfer"),
}
DEFAULT_SOURCE = "lrks-economic"
MIN_DOWNLOAD_BYTES = 20_000
MAX_DOWNLOAD_BYTES = 8_000_000
MAX_ROWS = 200_000
# A real Basic list has hundreds of chip resistors and MLCCs. Fewer means a broken upstream
# export; using it would wrongly call Basic parts "Extended", so it is refused.
MIN_BASIC_RESISTORS = 100
MIN_BASIC_CAPACITORS = 40
CACHE_DIR = "parts-db"
DATA_FILE = "jlc-parts.csv"
META_FILE = "jlc-parts.json"
CACHE_FORMAT = 1
USER_AGENT = "VelaTrace-parts-list (+https://github.com/F-Blaze/VelaTrace)"

_REQUIRED = {"lrks": {"code", "library", "deleted", "package", "type", "describe", "model"},
             "cdfer": {"lcsc", "package", "library_type", "preferred", "basic", "description",
                       "subcategory", "mfr"}}
_RESISTANCE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([mkMG]?)\s*[ΩΩ]")
_CAPACITANCE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([pnuµμm]?)F(?![A-Za-z])")
_VOLTAGE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*V(?![A-Za-z])")
_TOLERANCE = re.compile(r"±\s*(\d+(?:\.\d+)?)\s*%")
_DIELECTRIC = re.compile(r"\b(X5R|X7R|X7S|X6S|X8R|X5S|Y5V|Z5U|C0G|NP0|NPO|COG)\b")


class PartsDBError(ValidationError):
    """The parts list could not be downloaded, verified or read; nothing was cached."""


@dataclass(frozen=True)
class Part:
    lcsc: str
    tier: str  # "basic" | "preferred" (Preferred Extended: no Economic loading fee)
    kind: str  # resistor | capacitor | inductor | other
    package: str
    value: Decimal | None = None
    voltage: Decimal | None = None
    dielectric: str | None = None
    tolerance: Decimal | None = None
    mpn: str = ""


def _kind(category: str) -> str:
    text = category.casefold()
    if "chip resistor" in text:
        return "resistor"
    if text.startswith("multilayer ceramic capacitors mlcc - smd"):
        return "capacitor"
    if "inductor" in text and "smd" in text:
        return "inductor"
    return "other"


def _describe(kind: str, lcsc: str, tier: str, package: str, text: str, mpn: str) -> Part:
    value = voltage = tolerance = dielectric = None
    pattern = {"resistor": _RESISTANCE, "capacitor": _CAPACITANCE}.get(kind)
    if pattern and (match := pattern.search(text)):
        prefix = match.group(2).replace("µ", "u").replace("μ", "u")
        value = Decimal(match.group(1)) * SI_PREFIX[prefix]
    if kind == "capacitor":
        if match := _VOLTAGE.search(text):
            voltage = Decimal(match.group(1))
        if match := _DIELECTRIC.search(text.upper()):
            dielectric = {"NP0": "C0G", "NPO": "C0G", "COG": "C0G"}.get(match.group(1),
                                                                      match.group(1))
    if match := _TOLERANCE.search(text):
        tolerance = Decimal(match.group(1))
    return Part(lcsc, tier, kind, package.strip(), value, voltage, dielectric, tolerance,
                mpn.strip())


def _rows_lrks(row: dict) -> Part | None:
    if row["deleted"].strip() != "0":
        return None  # dropped from the Economic list
    tier = {"base": "basic", "expand": "preferred"}.get(row["library"].strip())
    if tier is None:
        return None
    return _describe(_kind(row["type"]), row["code"].strip().upper(), tier, row["package"],
                     row["describe"], row["model"])


def _rows_cdfer(row: dict) -> Part | None:
    if row.get("present", "1").strip() == "0":
        return None
    if row["basic"].strip() == "1" or row["library_type"].strip() == "base":
        tier = "basic"
    elif row["preferred"].strip() == "1":
        tier = "preferred"
    else:
        return None
    code = row["lcsc"].strip().upper()
    text = row["description"]
    try:
        attributes = json.loads(row.get("attributes") or "{}")
        if isinstance(attributes, dict):
            text += " " + " ".join(str(v) for v in attributes.values() if isinstance(v, str))
    except ValueError:
        pass
    return _describe(_kind(row["subcategory"]), code if code.startswith("C") else "C" + code,
                     tier, row["package"], text, row["mfr"])


class PartsDB:
    """Read-only lookup over Basic/Preferred parts. Absent from the list means Extended."""

    def __init__(self, parts: Iterable[Part], *, source: str, fetched_at: str = "",
                 sha256_hex: str = "", size: int = 0):
        self.source = SOURCES[source]
        self.fetched_at = fetched_at
        self.sha256 = sha256_hex
        self.size = size
        self._parts: dict[str, Part] = {}
        self._mpn: dict[str, Part] = {}
        self._lines: dict[tuple, list[Part]] = defaultdict(list)
        for part in parts:
            self._parts[part.lcsc] = part
            if part.mpn:
                self._mpn.setdefault(part.mpn.casefold(), part)
            if part.value is not None:
                self._lines[(part.kind, part.package)].append(part)

    def __len__(self) -> int:
        return len(self._parts)

    @property
    def label(self) -> str:
        return f"{self.source.label}, {self.source.cadence}"

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = defaultdict(int)
        for part in self._parts.values():
            result[f"{part.tier}_{part.kind}"] += 1
        return dict(result)

    def part(self, lcsc: str) -> Part | None:
        return self._parts.get(lcsc.strip().upper()) if isinstance(lcsc, str) else None

    def tier(self, lcsc: str) -> str | None:
        part = self.part(lcsc)
        return part.tier if part else None

    def find_mpn(self, mpn: str) -> Part | None:
        return self._mpn.get(mpn.strip().casefold()) if isinstance(mpn, str) else None

    def values(self, kind: str, package: str) -> list[Decimal]:
        return sorted({part.value for part in self._lines.get((kind, package), ())})

    def equivalents(self, kind: str, value: Decimal, package: str, *,
                    dielectric: str | None = None, allowed_dielectrics: set[str] | None = None,
                    min_voltage: Decimal | None = None,
                    max_tolerance: Decimal | None = None) -> list[Part]:
        """Same value and package with equal-or-better ratings; Basic first, then Preferred."""
        matches = []
        for part in self._lines.get((kind, package), ()):
            if part.value != value:
                continue
            if kind == "capacitor" and dielectric is not None and (
                    part.dielectric is None or part.dielectric not in (allowed_dielectrics or {dielectric})):
                continue
            if min_voltage is not None and kind == "capacitor" and (
                    part.voltage is None or part.voltage < min_voltage):
                continue
            if max_tolerance is not None and (part.tolerance is None or part.tolerance > max_tolerance):
                continue
            matches.append(part)
        return sorted(matches, key=lambda p: (p.tier != "basic", -(p.voltage or 0),
                                              p.tolerance if p.tolerance is not None else 99,
                                              int(p.lcsc[1:]) if p.lcsc[1:].isdigit() else 0))


def parse_catalogue(payload: bytes, source: str, *, fetched_at: str = "",
                    min_basic_resistors: int = MIN_BASIC_RESISTORS,
                    min_basic_capacitors: int = MIN_BASIC_CAPACITORS) -> PartsDB:
    if source not in SOURCES:
        raise PartsDBError("Unknown parts-list source.")
    if not isinstance(payload, bytes) or len(payload) > MAX_DOWNLOAD_BYTES:
        raise PartsDBError("Parts list is missing or exceeds the size limit.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PartsDBError("Parts list is not UTF-8 CSV.") from exc
    adapter = SOURCES[source].adapter
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames or not _REQUIRED[adapter] <= set(reader.fieldnames):
        raise PartsDBError("Parts list does not have the expected columns; the source format "
                           "may have changed. Nothing was cached.")
    convert = _rows_lrks if adapter == "lrks" else _rows_cdfer
    parts = []
    try:
        for count, row in enumerate(reader):
            if count >= MAX_ROWS:
                raise PartsDBError("Parts list has too many rows.")
            if None in row or any(value is None for value in row.values()):
                raise PartsDBError("Parts list contains malformed rows.")
            part = convert(row)
            if part is None:
                continue
            if not re.fullmatch(r"C\d{1,9}", part.lcsc) or len(part.package) > 64:
                raise PartsDBError("Parts list contains an invalid LCSC part number.")
            parts.append(part)
    except csv.Error as exc:
        raise PartsDBError("Parts list is not valid CSV.") from exc
    db = PartsDB(parts, source=source, fetched_at=fetched_at,
                 sha256_hex=sha256(payload).hexdigest(), size=len(payload))
    counts = db.counts()
    if (counts.get("basic_resistor", 0) < min_basic_resistors
            or counts.get("basic_capacitor", 0) < min_basic_capacitors):
        raise PartsDBError(
            f"The {SOURCES[source].label} list looks incomplete "
            f"({counts.get('basic_resistor', 0)} Basic resistors, "
            f"{counts.get('basic_capacitor', 0)} Basic capacitors); refusing it so Basic parts "
            "are not reported as Extended. Try again later or choose the other source.")
    return db


def https_get(url: str, max_bytes: int, timeout: float) -> bytes:
    """One bounded GET with verified TLS. Redirects and oversize bodies are refused."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or url not in {source.url for source in SOURCES.values()}):
        raise PartsDBError("Only the built-in HTTPS parts-list URLs can be downloaded.")
    deadline = monotonic() + timeout
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                                             timeout=timeout, context=ssl.create_default_context())
    connection._create_connection = lambda address, timeout, source_address=None: connect_before(
        address, deadline, source_address)
    response = None
    expired = threading.Event()

    def expire():
        # The socket timeout bounds one recv, and one read() may issue many: a server
        # that trickles bytes would hold the worker (and the window) indefinitely.
        expired.set()
        active = connection.sock
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    watchdog = threading.Timer(timeout, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        connection.request("GET", parsed.path, headers={
            "User-Agent": USER_AGENT, "Accept": "text/csv, text/plain;q=0.5",
            "Accept-Encoding": "identity"})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise PartsDBError("Parts-list server redirected; redirects are refused.")
        if response.status != 200:
            raise PartsDBError(f"Parts-list download failed (HTTP {response.status}).")
        declared = response.getheader("Content-Length")
        if declared is not None and (not declared.isdigit() or int(declared) > max_bytes):
            raise PartsDBError("Parts list exceeds the download size limit.")
        body = bytearray()
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk = response.read(65536)
            if not chunk:
                break
            body += chunk
            if len(body) > max_bytes:
                raise PartsDBError("Parts list exceeds the download size limit.")
        if declared is not None and int(declared) != len(body):
            raise PartsDBError("Parts-list download was truncated.")
        return bytes(body)
    except (TimeoutError, socket.timeout) as exc:
        raise PartsDBError("Parts-list download timed out.") from exc
    except (OSError, http.client.HTTPException) as exc:
        if expired.is_set():
            raise PartsDBError("Parts-list download timed out.") from exc
        raise PartsDBError("Cannot download the parts list. Check the network connection.") from exc
    finally:
        watchdog.cancel()
        if response is not None:
            response.close()
        connection.close()


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".parts-", suffix=".tmp",
                                         delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink(missing_ok=True)


def cache_paths(config_dir: Path) -> tuple[Path, Path]:
    folder = Path(config_dir) / CACHE_DIR
    return folder / DATA_FILE, folder / META_FILE


def download_catalogue(config_dir: Path, source: str = DEFAULT_SOURCE, *,
                       fetch: Callable[[str, int, float], bytes] | None = None,
                       timeout: float = 30.0, now: datetime | None = None,
                       min_basic_resistors: int = MIN_BASIC_RESISTORS,
                       min_basic_capacitors: int = MIN_BASIC_CAPACITORS) -> PartsDB:
    """Explicit user action only: fetch, verify and cache the list. Replaces any old cache."""
    if source not in SOURCES:
        raise PartsDBError("Unknown parts-list source.")
    payload = (fetch or https_get)(SOURCES[source].url, MAX_DOWNLOAD_BYTES, timeout)
    if not isinstance(payload, bytes) or not MIN_DOWNLOAD_BYTES <= len(payload) <= MAX_DOWNLOAD_BYTES:
        raise PartsDBError("Parts list is implausibly small or large; nothing was cached.")
    fetched_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = parse_catalogue(payload, source, fetched_at=fetched_at,
                         min_basic_resistors=min_basic_resistors,
                         min_basic_capacitors=min_basic_capacitors)
    data_path, meta_path = cache_paths(config_dir)
    meta = {"format": CACHE_FORMAT, "source": source, "url": SOURCES[source].url,
            "fetched_at": fetched_at, "sha256": db.sha256, "bytes": len(payload),
            "counts": db.counts()}
    try:
        data_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.unlink(missing_ok=True)  # never leave metadata describing other data
        _atomic_write(data_path, payload)
        _atomic_write(meta_path, json.dumps(meta, sort_keys=True).encode("utf-8"))
    except OSError as exc:
        raise PartsDBError("Could not save the parts list in the local config folder.") from exc
    return db


def load_catalogue(config_dir: Path) -> PartsDB | None:
    """Offline read of the verified cache. None when nothing was downloaded yet."""
    data_path, meta_path = cache_paths(config_dir)
    if not data_path.exists() and not meta_path.exists():
        return None
    reset = f" Delete the {CACHE_DIR} folder in the VelaTrace config folder or download again."
    try:
        if meta_path.stat().st_size > 100_000 or data_path.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise ValueError()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (not isinstance(meta, dict) or meta.get("format") != CACHE_FORMAT
                or meta.get("source") not in SOURCES
                or not isinstance(meta.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"])
                or type(meta.get("bytes")) is not int or not isinstance(meta.get("fetched_at"), str)):
            raise ValueError()
        payload = data_path.read_bytes()
    except (OSError, ValueError, TypeError) as exc:
        raise PartsDBError("Cached parts list is unreadable or incomplete." + reset) from exc
    if len(payload) != meta["bytes"] or sha256(payload).hexdigest() != meta["sha256"]:
        raise PartsDBError("Cached parts list failed its integrity check (hash mismatch)." + reset)
    # Completeness was checked at download time; the hash proves it is the same payload.
    return parse_catalogue(payload, meta["source"], fetched_at=meta["fetched_at"],
                           min_basic_resistors=0, min_basic_capacitors=0)


def clear_catalogue(config_dir: Path) -> None:
    for path in cache_paths(config_dir):
        path.unlink(missing_ok=True)
