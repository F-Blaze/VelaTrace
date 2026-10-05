"""Optional live JLCPCB stock and prices for the part numbers on the current BOM.

refresh() is the only network code and is only called from the "Refresh JLCPCB stock/prices"
button. Each request carries one LCSC part number and nothing else: no board, no file names,
no cookies. It is the unauthenticated JSON the JLCPCB parts page itself loads; it is
undocumented, so every failure is survivable and results are cached with their time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import time
from typing import Callable, Iterable

from .jlc_http import Cancelled, JlcNetError, request
from .parts_db import _atomic_write

URL = "https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode={}"
MIN_INTERVAL = 1.0      # seconds between two requests: a person clicking, not a crawler
MAX_PARTS = 200         # per click; a larger BOM is refreshed in further clicks (cache fills up)
MAX_AGE = timedelta(hours=6)   # a newer cached answer is reused instead of asking again
MAX_FAILURES = 3        # consecutive failures before giving up on this click
MAX_RESPONSE_BYTES = 300_000
MAX_CACHE = 5000
CACHE_DIR = "jlc-live"
CACHE_FILE = "parts.json"
CACHE_FORMAT = 1
_CODE = re.compile(r"C\d{1,9}")
_TIME = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class LivePart:
    lcsc: str
    fetched_at: str                      # UTC, when JLCPCB answered
    found: bool = True                   # False: JLCPCB does not list this part number
    stock: int = 0                       # JLCPCB assembly stock
    prices: tuple[tuple[int, Decimal], ...] = ()
    library: str = ""                    # "basic" | "extended"; the endpoint does not mark Preferred
    buyable: bool = True
    mpn: str = ""

    def unit_price(self, quantity: int = 1) -> Decimal | None:
        price = None
        for start, amount in self.prices:
            if price is None or quantity >= start:
                price = amount
        return price

    @property
    def when(self) -> datetime:
        return datetime.strptime(self.fetched_at, _TIME).replace(tzinfo=timezone.utc)

    @property
    def label(self) -> str:
        return "live as of " + self.when.astimezone().strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class LiveResult:
    parts: dict[str, LivePart]           # everything known after this click, cached answers included
    asked: int                           # requests actually sent
    failed: dict[str, str]               # code -> reason
    skipped: int                         # codes beyond MAX_PARTS, left for the next click

    @property
    def summary(self) -> str:
        text = f"{self.asked} part(s) checked at JLCPCB"
        if self.failed:
            text += f", {len(self.failed)} failed ({next(iter(self.failed.values()))})"
        if self.skipped:
            text += f", {self.skipped} left for the next click (limit {MAX_PARTS} per click)"
        return text + "."


def parse_part(code: str, payload: bytes, fetched_at: str) -> LivePart:
    """Untrusted JSON in, a bounded typed record out."""
    try:
        document = json.loads(payload.decode("utf-8"))
        data = document.get("data") if isinstance(document, dict) else None
        if data is None:
            return LivePart(code, fetched_at, found=False)
        if not isinstance(data, dict) or str(data.get("componentCode", "")).upper() != code:
            raise ValueError()
        stock = data.get("stockCount")
        if type(stock) is not int or not 0 <= stock < 10**12:
            raise ValueError()
        prices = []
        for row in (data.get("prices") or [])[:40]:
            amount = Decimal(str(row["productPrice"]))
            start = row["startNumber"]
            if type(start) is not int or start < 0 or not amount.is_finite() or not 0 <= amount < 10**6:
                raise ValueError()
            prices.append((start, amount))
        library = {"base": "basic", "expand": "extended"}.get(str(data.get("componentLibraryType")), "")
        return LivePart(code, fetched_at, True, stock, tuple(sorted(prices)), library,
                        str(data.get("isBuyComponent", "1")) == "1",
                        str(data.get("componentModelEn") or "")[:80])
    except (ValueError, KeyError, TypeError, AttributeError, InvalidOperation, UnicodeDecodeError) as exc:
        raise JlcNetError("JLCPCB answered in an unexpected format.") from exc


def _cache_path(config_dir: Path) -> Path:
    return Path(config_dir) / CACHE_DIR / CACHE_FILE


def load_live(config_dir: Path) -> dict[str, LivePart]:
    """Offline read of earlier answers. A damaged cache is simply empty."""
    path = _cache_path(config_dir)
    try:
        if not path.is_file() or path.stat().st_size > 5_000_000:
            return {}
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("format") != CACHE_FORMAT:
            return {}
        parts = {}
        for code, row in list(document["parts"].items())[:MAX_CACHE]:
            part = LivePart(code, row["fetched_at"], bool(row["found"]), int(row["stock"]),
                            tuple((int(a), Decimal(b)) for a, b in row["prices"]),
                            str(row["library"]), bool(row["buyable"]), str(row["mpn"]))
            if _CODE.fullmatch(code) and part.when:
                parts[code] = part
        return parts
    except (OSError, ValueError, KeyError, TypeError, AttributeError, InvalidOperation):
        return {}


def _save(config_dir: Path, parts: dict[str, LivePart]) -> None:
    newest = sorted(parts.values(), key=lambda part: part.fetched_at)[-MAX_CACHE:]
    document = {"format": CACHE_FORMAT, "parts": {part.lcsc: {
        "fetched_at": part.fetched_at, "found": part.found, "stock": part.stock,
        "prices": [[start, str(amount)] for start, amount in part.prices],
        "library": part.library, "buyable": part.buyable, "mpn": part.mpn} for part in newest}}
    path = _cache_path(config_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(document, sort_keys=True).encode("utf-8"))
    except OSError:
        pass  # the answers are still returned; only the cache is missing next time


def clear_live(config_dir: Path) -> None:
    _cache_path(config_dir).unlink(missing_ok=True)


def refresh(config_dir: Path, codes: Iterable[str], *, fetch=request,
            sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] | None = None,
            max_age: timedelta = MAX_AGE, progress: Callable[[int, int], None] | None = None,
            cancel: Callable[[], bool] | None = None) -> LiveResult:
    """Explicit user action only. One polite request per part number not answered recently."""
    now = now or (lambda: datetime.now(timezone.utc))
    wanted = sorted({code.strip().upper() for code in codes
                     if isinstance(code, str) and _CODE.fullmatch(code.strip().upper())},
                    key=lambda code: int(code[1:]))
    known = load_live(config_dir)
    stale = [code for code in wanted if code not in known or now() - known[code].when > max_age]
    todo, skipped = stale[:MAX_PARTS], len(stale[MAX_PARTS:])
    failed: dict[str, str] = {}
    asked = streak = 0
    last = None
    try:
        for index, code in enumerate(todo):
            if cancel is not None and cancel():
                break
            if last is not None and (wait := MIN_INTERVAL - (clock() - last)) > 0:
                sleep(wait)
            last = clock()
            asked += 1
            try:
                _, _, body = fetch(URL.format(code), timeout=15, max_bytes=MAX_RESPONSE_BYTES,
                                   headers={"Accept": "application/json"}, cancel=cancel)
                known[code] = parse_part(code, body, now().strftime(_TIME))
                streak = 0
            except Cancelled:
                break
            except JlcNetError as exc:
                failed[code] = str(exc)
                streak += 1
                if streak >= MAX_FAILURES:  # blocked or down: stop asking
                    for rest in todo[index + 1:]:
                        failed[rest] = "not asked after repeated failures"
                    break
            if progress:
                progress(index + 1, len(todo))
    finally:
        if asked:
            _save(config_dir, known)
    return LiveResult({code: known[code] for code in wanted if code in known}, asked, failed, skipped)
