"""Full JLCPCB parts catalogue: downloaded on an explicit click, verified, searched offline.

The data is the "current parts" SQLite/FTS5 database that the kicad-jlcpcb-tools project
publishes on GitHub Pages (see docs/jlcpcb.md for size, cadence and licence). download() is
the only network code here and is only ever called from a button; it sends no board data.
load() opens the verified local file read-only and never touches the network.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import astuple, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
from typing import Callable
from urllib.parse import quote
import zipfile

from .jlc_http import JlcNetError, request
from .parts_db import Part, PartsDB, PartsDBError, Source, _atomic_write, _describe, _kind
from .parts_db import load_catalogue as load_small_list

BASE_URL = "https://bouni.github.io/kicad-jlcpcb-tools/"
COUNT_FILE = "chunk_num_current_parts_fts5.txt"
CHUNK_FILE = "current-parts-fts5.db.zip.{:03d}"
SOURCE = Source(
    "bouni-current", "kicad-jlcpcb-tools parts database", BASE_URL,
    "https://github.com/Bouni/kicad-jlcpcb-tools",
    "no licence statement on the data files (plugin code is MIT); content is JLCPCB's catalogue",
    "daily when the upstream build succeeds", "bouni")
MAX_CHUNKS = 16
MAX_CHUNK_BYTES = 100_000_000
MAX_DB_BYTES = 2_000_000_000
# Fewer than this means a broken upstream build; Basic parts would be reported as Extended.
MIN_BASIC_RESISTORS = 100
MIN_BASIC_CAPACITORS = 40
CACHE_DIR = "jlc-catalog"
DB_FILE = "parts.db"
META_FILE = "parts.json"
CACHE_FORMAT = 1
COLUMNS = ("LCSC Part", "First Category", "Second Category", "MFR.Part", "Package",
           "Manufacturer", "Library Type", "Description", "Datasheet", "Price", "Stock")
_SELECT = "SELECT " + ", ".join(f'"{name}"' for name in COLUMNS) + " FROM parts WHERE parts MATCH ?"
_TIERS = {"basic": "basic", "preferred": "preferred"}
_TIER_ORDER = {"basic": 0, "preferred": 1, "extended": 2}
_CODE = re.compile(r"C\d{1,9}")
_PRICE = re.compile(r"(\d+)-\d*:(\d+(?:\.\d+)?)")
_RESET = f" Delete the {CACHE_DIR} folder in the VelaTrace config folder or download again."


@dataclass(frozen=True)
class CatalogPart(Part):
    """A catalogue row. tier is basic | preferred | extended."""
    stock: int | None = None
    prices: tuple[tuple[int, Decimal], ...] = ()  # (from quantity, USD each), ascending
    description: str = ""
    category: str = ""
    manufacturer: str = ""
    datasheet: str = ""

    def unit_price(self, quantity: int = 1) -> Decimal | None:
        """USD each at this order quantity, from the part's own price breaks."""
        price = None
        for start, amount in self.prices:
            if price is None or quantity >= start:
                price = amount
        return price


@dataclass(frozen=True)
class RemoteInfo:
    chunks: tuple[tuple[str, int], ...]  # (file name, bytes)
    published: str                       # Last-Modified of the first chunk

    @property
    def total(self) -> int:
        return sum(size for _, size in self.chunks)


def parse_prices(text: str) -> tuple[tuple[int, Decimal], ...]:
    return tuple(sorted((int(start), Decimal(amount)) for start, amount in _PRICE.findall(text or "")))


def _row_part(row) -> CatalogPart | None:
    code, first, second, mpn, package, maker, library, text, sheet, price, stock = (
        "" if value is None else str(value) for value in row)
    code = code.strip().upper()
    if not _CODE.fullmatch(code):
        return None
    tier = _TIERS.get(library.strip().casefold(), "extended")
    base = _describe(_kind(second), code, tier, package[:64], text, mpn)
    stock = stock.strip()
    return CatalogPart(*astuple(base), stock=int(stock) if stock.isdigit() else None,
                       prices=parse_prices(price), description=text.strip(),
                       category=f"{first.strip()} / {second.strip()}", manufacturer=maker.strip(),
                       datasheet=sheet.strip())


def _uri(path: Path) -> str:
    return f"file:{quote(Path(path).as_posix(), safe='/:')}?mode=ro&immutable=1"


def _phrase(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _column(name: str, text: str) -> str:
    return f'"{name}":{_phrase(text)}'


class JlcCatalog(PartsDB):
    """Read-only catalogue. Basic/Preferred parts are indexed in memory exactly like the small
    list (so equivalents()/values() behave the same); Extended parts are looked up in SQLite."""

    complete = True  # part() knows Extended parts too; None means "not in the catalogue"

    def __init__(self, path: Path, *, fetched_at: str = "", published: str = "",
                 sha256_hex: str = "", size: int = 0):
        self.source = SOURCE
        self.fetched_at = fetched_at
        self.published = published
        self.sha256 = sha256_hex
        self.size = size
        self.path = Path(path)
        self._lock = threading.Lock()
        self._parts: dict[str, Part] = {}
        self._mpn: dict[str, Part] = {}
        self._lines: dict[tuple, list[Part]] = defaultdict(list)
        self._missing: set[str] = set()
        try:
            self._db = sqlite3.connect(_uri(self.path), uri=True, check_same_thread=False)
            self._db.execute("PRAGMA query_only=1")
            for part in self._rows(_column("Library Type", "Basic") + " OR "
                                   + _column("Library Type", "Preferred"), 50_000):
                if part.tier == "extended":
                    continue
                self._parts[part.lcsc] = part
                if part.mpn:
                    self._mpn.setdefault(part.mpn.casefold(), part)
                # Only parts that can actually be placed today are offered as replacements.
                if part.value is not None and part.stock:
                    self._lines[(part.kind, part.package)].append(part)
        except sqlite3.Error as exc:
            self.close()
            raise PartsDBError("The JLCPCB catalogue cannot be read." + _RESET) from exc

    def close(self) -> None:
        db, self._db = getattr(self, "_db", None), None
        if db is not None:
            db.close()

    def _rows(self, match: str, limit: int) -> list[CatalogPart]:
        with self._lock:
            if self._db is None:
                raise PartsDBError("The JLCPCB catalogue is closed.")
            try:
                rows = self._db.execute(_SELECT + " LIMIT ?", (match, limit)).fetchall()
            except sqlite3.Error as exc:
                raise PartsDBError("The JLCPCB catalogue cannot be searched." + _RESET) from exc
        return [part for part in map(_row_part, rows) if part is not None]

    @property
    def label(self) -> str:
        return f"{self.source.label}, published {self.published[:10] or 'unknown'}"

    def part(self, lcsc: str) -> CatalogPart | None:
        if not isinstance(lcsc, str) or not _CODE.fullmatch(code := lcsc.strip().upper()):
            return None
        if code in self._parts or code in self._missing:
            return self._parts.get(code)
        # Trigram search matches substrings (C2580 inside C25804): confirm the exact code.
        found = next((part for part in self._rows(_column("LCSC Part", code), 50)
                      if part.lcsc == code), None) if len(code) >= 3 else None
        if found is None:
            self._missing.add(code)
        else:
            self._parts[code] = found
        return found

    def find_mpn(self, mpn: str) -> CatalogPart | None:
        if not isinstance(mpn, str) or not 3 <= len(key := mpn.strip()) <= 80:
            return None
        if key.casefold() in self._mpn:
            return self._mpn[key.casefold()]
        matches = [part for part in self._rows(_column("MFR.Part", key), 50)
                   if part.mpn.casefold() == key.casefold()]
        return min(matches, key=_rank) if matches else None

    def search(self, text: str = "", *, package: str = "", tiers: tuple[str, ...] = (),
               in_stock: bool = False, exact: tuple | None = None,
               limit: int = 100) -> list[CatalogPart]:
        """Parts whose code, MPN, description, package, category or manufacturer contain
        every word. `exact` = (kind, value, package) keeps only that passive value.
        Basic first, then Preferred, then Extended; most stock first."""
        words = [word for word in text.split()[:12] if len(word) <= 80]
        if len(words) == 1 and _CODE.fullmatch(words[0].upper()) and (hit := self.part(words[0])):
            return [hit]
        # The trigram index needs three characters; shorter words are checked on the rows.
        clauses = [_phrase(word) for word in words if len(word) >= 3]
        if len(package) >= 3:
            clauses.append(_column("Package", package))
        if not clauses:
            return []
        short = [word.casefold() for word in words if len(word) < 3]
        wanted = set(tiers or _TIER_ORDER)
        found: dict[str, CatalogPart] = {}
        economic = "(" + _column("Library Type", "Basic") + " OR " + _column("Library Type", "Preferred") + ")"
        # Two passes so Basic/Preferred matches are never crowded out by the row limit.
        for extra in ([economic] if wanted & {"basic", "preferred"} else []) + (
                [None] if "extended" in wanted else []):
            for part in self._rows(" AND ".join(clauses + ([extra] if extra else [])), 3000):
                haystack = " ".join((part.lcsc, part.mpn, part.description, part.package,
                                     part.category, part.manufacturer)).casefold()
                if (part.tier in wanted and (not in_stock or part.stock)
                        and (not package or part.package.casefold() == package.casefold())
                        and all(word in haystack for word in short)
                        and (exact is None or (part.kind, part.value, part.package) == exact)):
                    found.setdefault(part.lcsc, part)
        return sorted(found.values(), key=_rank)[:limit]


def _rank(part: CatalogPart):
    return (_TIER_ORDER.get(part.tier, 3), -(part.stock or 0), part.lcsc)


def cache_paths(config_dir: Path) -> tuple[Path, Path]:
    folder = Path(config_dir) / CACHE_DIR
    return folder / DB_FILE, folder / META_FILE


def remote_info(fetch=request) -> RemoteInfo:
    """What a download would fetch: one tiny GET and one HEAD per chunk. No board data."""
    _, _, body = fetch(BASE_URL + COUNT_FILE, max_bytes=16, timeout=20)
    text = body.decode("ascii", "replace").strip()
    if not text.isdigit() or not 1 <= int(text) <= MAX_CHUNKS:
        raise JlcNetError("The catalogue index is not in the expected format.")
    chunks, published = [], ""
    for index in range(1, int(text) + 1):
        name = CHUNK_FILE.format(index)
        _, headers, _ = fetch(BASE_URL + name, method="HEAD", timeout=20)
        size = headers.get("content-length", "")
        if not size.isdigit() or not 0 < int(size) <= MAX_CHUNK_BYTES:
            raise JlcNetError("The catalogue server did not report a plausible file size.")
        chunks.append((name, int(size)))
        published = published or headers.get("last-modified", "")
    return RemoteInfo(tuple(chunks), _iso(published))


def _iso(http_date: str) -> str:
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(http_date).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def _fetch_chunk(fetch, name: str, size: int, path: Path, progress, done: int, total: int, cancel):
    """Resume a partial file with a Range request; restart it when the server ignores Range."""
    have = path.stat().st_size if path.exists() else 0
    if have > size:
        path.unlink()
        have = 0
    if have == size:
        return
    state = {"have": have}
    with path.open("ab") as output:
        def sink(chunk: bytes):
            output.write(chunk)
            state["have"] += len(chunk)
            if progress:
                progress(done + state["have"], total)
        headers = {"Range": f"bytes={have}-"} if have else {}
        status, found, _ = fetch(BASE_URL + name, headers=headers, timeout=1800,
                                 max_bytes=size - have if have else size, sink=sink, cancel=cancel)
        if have and (status != 206 or not found.get("content-range", "").startswith(f"bytes {have}-")):
            output.truncate(0)  # not a continuation of our bytes: never splice it in
            raise JlcNetError("The server did not resume the download; click again to restart it.")
        output.flush()
        os.fsync(output.fileno())
    if path.stat().st_size != size:
        raise JlcNetError("Catalogue download is incomplete; click again to resume.")


def validate(path: Path, *, min_basic_resistors: int = MIN_BASIC_RESISTORS,
             min_basic_capacitors: int = MIN_BASIC_CAPACITORS) -> dict[str, int]:
    """Refuse anything that is not a complete, readable catalogue. Returns part counts."""
    try:
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise PartsDBError("The downloaded catalogue is not a SQLite database.")
        db = sqlite3.connect(_uri(path), uri=True)
        try:
            if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise PartsDBError("The downloaded catalogue failed SQLite's integrity check.")
            db.execute(_SELECT + " LIMIT 1", (_column("LCSC Part", "C25804"),)).fetchall()
            total = db.execute("SELECT count(*) FROM parts").fetchone()[0]
        finally:
            db.close()
    except (OSError, sqlite3.Error) as exc:
        raise PartsDBError("The downloaded catalogue does not have the expected format; the "
                           "source may have changed. Nothing was replaced.") from exc
    catalogue = JlcCatalog(path)
    try:
        counts = catalogue.counts()
    finally:
        catalogue.close()
    if (counts.get("basic_resistor", 0) < min_basic_resistors
            or counts.get("basic_capacitor", 0) < min_basic_capacitors):
        raise PartsDBError(
            f"The catalogue looks incomplete ({counts.get('basic_resistor', 0)} Basic resistors, "
            f"{counts.get('basic_capacitor', 0)} Basic capacitors); refusing it so Basic parts are "
            "not reported as Extended. The previous catalogue, if any, was kept.")
    return {"parts": total, **counts}


def download(config_dir: Path, *, info: RemoteInfo | None = None, fetch=request,
             progress: Callable[[int, int], None] | None = None,
             cancel: Callable[[], bool] | None = None, now: datetime | None = None,
             min_basic_resistors: int = MIN_BASIC_RESISTORS,
             min_basic_capacitors: int = MIN_BASIC_CAPACITORS) -> dict:
    """Explicit user action only. Resumable; the old catalogue stays until the new one is
    fully verified, then it is replaced atomically. Returns the saved metadata."""
    info = info or remote_info(fetch)
    data_path, meta_path = cache_paths(config_dir)
    work = data_path.parent / "download"
    try:
        work.mkdir(parents=True, exist_ok=True)
        marker = work / "source.json"
        identity = json.dumps({"chunks": info.chunks, "published": info.published}, sort_keys=True)
        if not marker.exists() or marker.read_text(encoding="utf-8") != identity:
            shutil.rmtree(work)  # a different upstream build: partial files cannot be mixed
            work.mkdir()
            marker.write_text(identity, encoding="utf-8")
        done = 0
        for name, size in info.chunks:
            _fetch_chunk(fetch, name, size, work / name, progress, done, info.total, cancel)
            done += size
        archive = work / "parts.zip"
        with archive.open("wb") as output:
            for name, _ in info.chunks:
                with (work / name).open("rb") as chunk:
                    shutil.copyfileobj(chunk, output, 1 << 20)
        extracted = work / "parts.db"
        digest = sha256()
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) != 1 or not 0 < members[0].file_size <= MAX_DB_BYTES:
                raise PartsDBError("The catalogue archive is not in the expected format.")
            if shutil.disk_usage(work).free < members[0].file_size + 50_000_000:
                raise PartsDBError(f"Not enough disk space: the catalogue unpacks to "
                                   f"{members[0].file_size / 1e6:.0f} MB. The download was kept; "
                                   "free some space and click again.")
            # zipfile checks the member's CRC-32 when the stream is read to its end.
            with bundle.open(members[0]) as source, extracted.open("wb") as output:
                while block := source.read(1 << 20):
                    if cancel is not None and cancel():
                        raise JlcNetError("Cancelled.")
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
        counts = validate(extracted, min_basic_resistors=min_basic_resistors,
                          min_basic_capacitors=min_basic_capacitors)
        meta = {"format": CACHE_FORMAT, "source": SOURCE.key, "url": BASE_URL,
                "fetched_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "published": info.published, "sha256": digest.hexdigest(),
                "bytes": extracted.stat().st_size, "download_bytes": info.total, "counts": counts}
        meta_path.unlink(missing_ok=True)  # never leave metadata describing other data
        os.replace(extracted, data_path)
        meta["mtime_ns"] = data_path.stat().st_mtime_ns
        _atomic_write(meta_path, json.dumps(meta, sort_keys=True).encode("utf-8"))
        shutil.rmtree(work, ignore_errors=True)
        return meta
    except zipfile.BadZipFile as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise PartsDBError("The catalogue archive is corrupt (checksum mismatch); the partial "
                           "download was discarded. Click again to download it afresh.") from exc
    except PermissionError as exc:
        raise PartsDBError("The catalogue file is in use; close other VelaTrace windows and "
                           "try again.") from exc
    except OSError as exc:
        raise PartsDBError("Could not save the catalogue in the local config folder "
                           "(disk full or no permission?).") from exc


def read_meta(config_dir: Path) -> dict | None:
    """Metadata of the installed catalogue, or None when there is none."""
    data_path, meta_path = cache_paths(config_dir)
    if not data_path.exists() and not meta_path.exists():
        return None
    try:
        if meta_path.stat().st_size > 100_000:
            raise ValueError()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (not isinstance(meta, dict) or meta.get("format") != CACHE_FORMAT
                or not isinstance(meta.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"])
                or type(meta.get("bytes")) is not int or type(meta.get("mtime_ns")) is not int
                or not isinstance(meta.get("fetched_at"), str)
                or not isinstance(meta.get("published"), str)):
            raise ValueError()
        if data_path.stat().st_size != meta["bytes"]:
            raise PartsDBError("The JLCPCB catalogue file has the wrong size." + _RESET)
    except (OSError, ValueError, TypeError) as exc:
        raise PartsDBError("The JLCPCB catalogue is unreadable or incomplete." + _RESET) from exc
    return meta


def load(config_dir: Path) -> JlcCatalog | None:
    """Offline. None when no catalogue was downloaded. The full SHA-256 is re-checked only
    when the file's modification time differs from the one recorded at download."""
    meta = read_meta(config_dir)
    if meta is None:
        return None
    data_path = cache_paths(config_dir)[0]
    if data_path.stat().st_mtime_ns != meta["mtime_ns"]:
        digest = sha256()
        with data_path.open("rb") as stream:
            while block := stream.read(1 << 20):
                digest.update(block)
        if digest.hexdigest() != meta["sha256"]:
            raise PartsDBError("The JLCPCB catalogue failed its integrity check." + _RESET)
    return JlcCatalog(data_path, fetched_at=meta["fetched_at"], published=meta["published"],
                      sha256_hex=meta["sha256"], size=meta["bytes"])


def clear(config_dir: Path) -> None:
    shutil.rmtree(Path(config_dir) / CACHE_DIR, ignore_errors=True)


def best_parts_db(config_dir: Path) -> PartsDB | None:
    """The full catalogue when installed, else the small Basic/Preferred list, else None."""
    return load(config_dir) or load_small_list(config_dir)
