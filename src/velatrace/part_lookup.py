"""Footprint fit, step two: find the real part behind each non-passive footprint and get its pads.

identify() is local (LCSC and MPN fields, the offline catalogue, earlier answers, the user) and may ask
questions, so the UI runs it on its own thread. fetch() is the only network code: EasyEDA/LCSC pad
data (cached, parsed fields only) and, for parts not on LCSC, a package-level DigiKey record that
is kept in memory for one run and never written to disk (DigiKey's terms forbid a local database).
Each request carries one part number and nothing about the board. Both sources are undocumented
or untested in places, so every failure is survivable and only shows up as "not checked".
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import urlencode

from . import jlc_export
from .bom import _ref_key, lcsc_code, mpn_of
from .easyeda import RealPad, RealPart, _text, parse_component
from .errors import VelaTraceError
from .findings import Finding, Severity
from .footprint_fit import compare, fit_candidates, kicad_pads, pad_nets
from .jlc_http import JlcNetError, request
from .models import DesignSnapshot
from .parts_db import _atomic_write

EASYEDA_URL = "https://easyeda.com/api/products/{}/components?version=6.4.19.5"
DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"
EASYEDA_INTERVAL = 1.0      # seconds between requests: a person clicking, not a crawler
DIGIKEY_INTERVAL = 0.6      # their limit is per second and exceeding it suspends the account
MAX_PARTS = 200             # EasyEDA requests per run; the rest is looked up on the next run
MAX_DIGIKEY = 50            # DigiKey searches per run (their daily limit is 1000)
MAX_FAILURES = 3            # consecutive failures per source before it is left alone this run
EASYEDA_BYTES = 1_000_000
DIGIKEY_BYTES = 2_000_000
MISSING_AGE = timedelta(days=30)    # "EasyEDA has no pads for this" is asked again after this
CACHE_DIR = "part-fit"
CACHE_FORMAT = 1
MAX_CACHE = 2000
MAX_BOARDS = 200
MAX_CHOICES = 500
SKIP = "skip"
_CODE = re.compile(r"C\d{1,9}")
_TIME = "%Y-%m-%dT%H:%M:%SZ"
# Result groups of the summary, in the order shown.
CATEGORIES = ("not found", "skipped", "not looked up", "failed")


@dataclass(frozen=True)
class Option:
    """One answer to offer the user. key is what gets remembered: an LCSC code or "mpn:<number>"."""
    key: str
    label: str
    part: RealPart | None = None


@dataclass(frozen=True)
class DigiKeyLogin:
    client_id: str
    client_secret: str = field(repr=False)


def excluded_refs(path) -> set[str]:
    """DNP / exclude-from-BOM references of the saved board (the snapshot does not carry them)."""
    try:
        if path and Path(path).suffix == ".kicad_pcb" and Path(path).stat().st_size < 32_000_000:
            parts, _ = jlc_export.board_parts(Path(path).read_text(encoding="utf-8"))
            return {p.reference for p in parts if p.dnp or p.exclude_from_bom}
    except (OSError, VelaTraceError):
        pass
    return set()


# ---- files in <config>/part-fit/ ---------------------------------------------------------------

def _read(config_dir: Path, name: str) -> dict:
    path = Path(config_dir) / CACHE_DIR / name
    try:
        if path.is_file() and path.stat().st_size < 5_000_000:
            document = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(document, dict) and document.get("format") == CACHE_FORMAT:
                return document
    except (OSError, ValueError):
        pass
    return {}


def _write(config_dir: Path, name: str, document: dict) -> None:
    path = Path(config_dir) / CACHE_DIR / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps({"format": CACHE_FORMAT, **document}, sort_keys=True).encode("utf-8"))
    except OSError:
        pass  # the answers are still used this run; only the memory of them is missing next time


def _load_cache(config_dir: Path) -> dict[str, dict]:
    """Earlier EasyEDA answers: code -> {at, title, package, pads} or {at, none}. A bad file is empty."""
    rows = _read(config_dir, "easyeda.json").get("parts")
    return {code: row for code, row in rows.items() if _CODE.fullmatch(code) and isinstance(row, dict)
            and isinstance(row.get("at"), str)} if isinstance(rows, dict) else {}


def _from_row(code: str, row: dict) -> RealPart | None:
    try:
        # Rows saved before pad areas were kept have 4 fields; their area is unknown (0).
        pads = tuple(RealPad(str(n)[:16], float(x), float(y), bool(t), float(area[0]) if area else 0.0)
                     for n, x, y, t, *area in row["pads"])
        return RealPart(code, str(row["title"]), str(row["package"]), pads) if pads else None
    except (KeyError, TypeError, ValueError):
        return None


def _row(part: RealPart | None, at: str) -> dict:
    if part is None:
        return {"at": at, "none": True}
    return {"at": at, "title": part.title, "package": part.package,
            "pads": [[p.number, p.x_mm, p.y_mm, p.through_hole, p.area_mm2] for p in part.pads]}


# ---- DigiKey -----------------------------------------------------------------------------------

def _int(text: str) -> int:
    match = re.match(r"\s*(\d+)", text)
    return int(match[1]) if match else 0


def _digikey_option(product: dict) -> Option | None:
    """A package-level record from one Product Information v4 product (untrusted JSON)."""
    mpn = _text(product.get("ManufacturerProductNumber")).strip()
    if not mpn:
        return None
    params = {_text(row.get("ParameterText")).casefold(): _text(row.get("ValueText"))
              for row in product.get("Parameters") or [] if isinstance(row, dict)}
    count = next((n for key in ("number of positions", "number of pins", "number of terminations")
                  if (n := _int(params.get(key, "")))), 0)
    mount = params.get("mounting type", "").casefold()
    through = False if "surface" in mount else True if "through" in mount else None
    rows = _int(params.get("number of rows", ""))
    maker, text = product.get("Manufacturer"), product.get("Description")
    label = " · ".join(item for item in (
        mpn, _text(maker.get("Name")) if isinstance(maker, dict) else "",
        _text(text.get("ProductDescription")) if isinstance(text, dict) else "") if item)
    part = RealPart("", mpn, params.get("package / case") or params.get("supplier device package", ""),
                    source="digikey", pin_count=count, through_hole=through,
                    rows=rows if count and rows and count % rows == 0 else 0) if count else None
    return Option("mpn:" + mpn, label, part)


class _DigiKey:
    """Product Information v4 as documented (client-credentials token, keyword search); the token
    stays in memory. Neither the credentials nor the token ever reach an error message."""

    def __init__(self, login: DigiKeyLogin, fetch, pace, clock):
        self.login, self.fetch, self.pace, self.clock = login, fetch, pace, clock
        self.token, self.expires = "", 0.0

    def _post(self, url: str, body: bytes, content_type: str, headers: dict[str, str]) -> bytes:
        self.pace.wait()
        return self.fetch(url, method="POST", body=body, content_type=content_type, headers={
            "Accept": "application/json", **headers}, timeout=20, max_bytes=DIGIKEY_BYTES)[2]

    def _sign_in(self) -> None:
        form = urlencode({"client_id": self.login.client_id, "client_secret": self.login.client_secret,
                          "grant_type": "client_credentials"}).encode()
        try:
            data = json.loads(self._post(DIGIKEY_TOKEN_URL, form, "application/x-www-form-urlencoded", {}))
            token, life = data["access_token"], float(data["expires_in"])
            if not isinstance(token, str) or not token:
                raise ValueError()
        except (ValueError, KeyError, TypeError) as exc:
            raise JlcNetError("DigiKey answered in an unexpected format.") from exc
        self.token, self.expires = token, self.clock() + max(life - 30, 0)

    def search(self, text: str) -> list[Option]:
        for retry in (True, False):
            if not self.token or self.clock() >= self.expires:
                self._sign_in()
            try:
                body = self._post(
                    DIGIKEY_SEARCH_URL, json.dumps({"Keywords": text, "Limit": 10, "Offset": 0}).encode(),
                    "application/json", {"Authorization": "Bearer " + self.token,
                                         "X-DIGIKEY-Client-Id": self.login.client_id,
                                         "X-DIGIKEY-Locale-Site": "US", "X-DIGIKEY-Locale-Language": "en",
                                         "X-DIGIKEY-Locale-Currency": "USD"})
                break
            except JlcNetError as exc:
                if exc.status == 401 and retry:   # expired early: one fresh token, one more try
                    self.token = ""
                    continue
                raise
        try:
            products = json.loads(body)["Products"]
            return [option for product in products[:10] if isinstance(product, dict)
                    and (option := _digikey_option(product))]
        except (ValueError, KeyError, TypeError) as exc:
            raise JlcNetError("DigiKey answered in an unexpected format.") from exc


class _Pace:
    def __init__(self, interval: float, sleep, clock):
        self.interval, self.sleep, self.clock, self.last = interval, sleep, clock, None

    def wait(self) -> None:
        if self.last is not None and (gap := self.interval - (self.clock() - self.last)) > 0:
            self.sleep(gap)
        self.last = self.clock()


# ---- the lookup --------------------------------------------------------------------------------

class PartLookup:
    """identify() on the UI thread, then fetch() anywhere, then provider() for run_heavy.

    choose(reference, options) -> Option | None and ask(reference, prompt) -> str | None are the
    only questions; None means "skip this part", and that is remembered for this board."""

    def __init__(self, snapshot: DesignSnapshot, config_dir: Path, *, excluded=(), catalogue=None,
                 digikey: DigiKeyLogin | None = None, choose=None, ask=None):
        skip = set(excluded)
        self.candidates = [c for c in fit_candidates(snapshot) if c.reference not in skip]
        self.config_dir, self.catalogue, self.digikey = Path(config_dir), catalogue, digikey
        self.choose, self.ask = choose or (lambda ref, options: None), ask or (lambda ref, prompt: None)
        self.board = str(snapshot.path) if snapshot.path else ""
        self.boards = {}
        self.remembered: dict[str, str] = {}
        if self.board:
            self.boards = dict(_read(self.config_dir, "choices.json").get("boards") or {})
            known = self.boards.get(self.board)
            self.remembered = {ref: key for ref, key in (known.items() if isinstance(known, dict) else ())
                               if isinstance(ref, str) and isinstance(key, str)}
        self.cache = _load_cache(self.config_dir)
        self.targets: dict[str, str] = {}               # reference -> LCSC code or "mpn:<number>"
        self.unchecked: dict[str, tuple[str, str]] = {}  # reference -> (category, reason)
        self.parts: dict[str, RealPart] = {}            # reference -> what the real part looks like

    # -- identify (local) --
    def _remember(self, ref: str, key: str) -> None:
        self.remembered.pop(ref, None)
        self.remembered[ref] = key
        if self.board:
            self.boards.pop(self.board, None)
            self.boards[self.board] = dict(list(self.remembered.items())[-MAX_CHOICES:])
            _write(self.config_dir, "choices.json", {"boards": dict(list(self.boards.items())[-MAX_BOARDS:])})

    def _typed(self, text: str) -> str | None:
        """An LCSC code, or a part number turned into one when the catalogue knows it."""
        text = text.strip()
        if _CODE.fullmatch(text.upper()):
            return text.upper()
        hit = self.catalogue.find_mpn(text) if self.catalogue is not None and text else None
        return hit.lcsc if hit else "mpn:" + text if self.digikey and text else None

    def _identify(self, comp) -> str | None:
        ref = comp.reference
        term = (mpn_of(comp) or comp.value).strip()
        if code := lcsc_code(comp):
            return code
        if (mpn := mpn_of(comp)) and self.catalogue is not None and (hit := self.catalogue.find_mpn(mpn)):
            return hit.lcsc
        if ref in self.remembered:
            return self.remembered[ref]
        found = []
        if self.catalogue is not None and hasattr(self.catalogue, "search") and len(term) >= 3:
            found = self.catalogue.search(term, limit=20)
        if len(found) == 1:
            return found[0].lcsc
        if found:
            picked = self.choose(ref, [Option(p.lcsc, f"{p.lcsc} · {p.mpn} · {p.package} · {p.description}"[:160])
                                       for p in found])
            key = picked.key if picked else SKIP
        elif self.digikey and term:   # not on LCSC (as far as we know): DigiKey is asked later
            return "mpn:" + term
        else:
            text = self.ask(ref, f"{ref} ({comp.value}, {comp.footprint.rsplit(':', 1)[-1]}): which part is "
                                 "this? Type an LCSC code (C...) or a manufacturer part number.")
            if not text or not text.strip():
                key = SKIP
            elif not (key := self._typed(text)):
                return None
        self._remember(ref, key)
        return key

    def identify(self) -> None:
        for comp in self.candidates:
            key = self._identify(comp)
            if key == SKIP:
                self.unchecked[comp.reference] = ("skipped", "you skipped it")
            elif key:
                self.targets[comp.reference] = key
            else:
                self.unchecked[comp.reference] = ("not found", "no part number, and it is not in the catalogue")
        self.catalogue = None

    # -- fetch (network) --
    def _cached(self, code: str, now: datetime) -> tuple[bool, RealPart | None]:
        row = self.cache.get(code)
        if row is None:
            return False, None
        if row.get("none"):
            try:
                fresh = now - datetime.strptime(row["at"], _TIME).replace(tzinfo=timezone.utc) < MISSING_AGE
            except ValueError:
                fresh = False
            return fresh, None
        part = _from_row(code, row)
        return part is not None, part

    def needs_network(self, now: Callable[[], datetime] | None = None) -> tuple[int, int]:
        """(EasyEDA requests, DigiKey searches) a full run would send; cached answers are free."""
        now = (now or (lambda: datetime.now(timezone.utc)))()
        codes = {key for key in self.targets.values() if _CODE.fullmatch(key)}
        queries = {key for key in self.targets.values() if key.startswith("mpn:")}
        return (min(sum(not self._cached(code, now)[0] for code in codes), MAX_PARTS),
                min(len(queries), MAX_DIGIKEY) if self.digikey else 0)

    def _easyeda(self, code: str, fetch) -> RealPart | None:
        _, _, body = fetch(EASYEDA_URL.format(code), timeout=20, max_bytes=EASYEDA_BYTES,
                           headers={"Accept": "application/json"})
        try:
            data = json.loads(body.decode("utf-8"))
            part = parse_component(data) if isinstance(data, dict) else None
        except ValueError as exc:
            raise JlcNetError("easyeda.com answered in an unexpected format.") from exc
        if part is not None and part.lcsc and part.lcsc != code:
            raise JlcNetError("easyeda.com answered with a different part.")
        return replace(part, lcsc=code) if part else None

    def _digikey(self, refs: list[str], query: str, client: _DigiKey, choose, ask) -> Option | tuple[str, str]:
        """The part DigiKey knows under `query`; the user is asked (and the answer remembered)
        only when it is ambiguous or unknown."""
        skipped, missing = ("skipped", "you skipped it"), ("not found", "DigiKey has no match for it")

        def remember(option: Option | None) -> None:
            for ref in refs:
                self._remember(ref, option.key if option else SKIP)
        for attempt in range(2):
            options = client.search(query)
            exact = [o for o in options if o.key.casefold() == "mpn:" + query.casefold()]
            if len(options) == 1 or len(exact) == 1:
                picked = options[0] if len(options) == 1 else exact[0]
                if attempt:   # a part number the user typed
                    remember(picked)
                return picked
            if options:
                picked = choose(refs[0], options)
                remember(picked)
                return picked or skipped
            if attempt:
                return missing
            text = ask(refs[0], f"DigiKey has nothing for '{query}'. Type another manufacturer part "
                                "number to search for, or skip this part.")
            if not text or not text.strip():
                remember(None)
                return skipped
            query = text.strip()
        return missing

    def fetch(self, *, network: bool = True, fetch=request, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] | None = None,
              choose=None, ask=None) -> None:
        """Pads for every target. network=False answers from the cache only and opens no socket."""
        now = now or (lambda: datetime.now(timezone.utc))
        choose, ask = choose or self.choose, ask or self.ask
        easyeda_pace, asked, streak = _Pace(EASYEDA_INTERVAL, sleep, clock), 0, 0
        client = _DigiKey(self.digikey, fetch, _Pace(DIGIKEY_INTERVAL, sleep, clock), clock) if self.digikey else None
        by_code: dict[str, RealPart | tuple[str, str] | None] = {}
        try:
            for code in dict.fromkeys(key for key in self.targets.values() if _CODE.fullmatch(key)):
                hit, part = self._cached(code, now())
                if hit:
                    by_code[code] = part
                elif not network:
                    by_code[code] = ("not looked up", "not saved from an earlier run; looked up offline only")
                elif asked >= MAX_PARTS:
                    by_code[code] = ("not looked up", f"over the limit of {MAX_PARTS} per run; run again")
                elif streak >= MAX_FAILURES:
                    by_code[code] = ("failed", "not asked after repeated failures")
                else:
                    easyeda_pace.wait()
                    asked += 1
                    try:
                        part = self._easyeda(code, fetch)
                        self.cache[code] = _row(part, now().strftime(_TIME))
                        by_code[code], streak = part, 0
                    except JlcNetError as exc:
                        by_code[code], streak = ("failed", str(exc)), streak + 1
            queries: dict[str, list[str]] = {}
            for ref, key in self.targets.items():
                if key.startswith("mpn:"):
                    queries.setdefault(key[4:], []).append(ref)
            searched = streak = 0
            for query, refs in queries.items():
                outcome: Option | tuple[str, str]
                if client is None:
                    outcome = ("not found", "not on LCSC, and no DigiKey credentials are set up")
                elif not network:
                    outcome = ("not looked up", "DigiKey is only asked when you allow lookups")
                elif searched >= MAX_DIGIKEY or streak >= MAX_FAILURES:
                    outcome = ("not looked up", "DigiKey limit reached for this run")
                else:
                    searched += 1
                    try:
                        outcome, streak = self._digikey(refs, query, client, choose, ask), 0
                    except JlcNetError as exc:
                        outcome, streak = ("failed", str(exc)), streak + 1
                for ref in refs:
                    if not isinstance(outcome, Option):
                        self.unchecked[ref] = outcome
                    elif outcome.part is not None:
                        self.parts[ref] = outcome.part
                    else:
                        self.unchecked[ref] = ("not found", "DigiKey lists no pin count for it")
        finally:
            if asked:
                newest = sorted(self.cache.items(), key=lambda item: item[1]["at"])[-MAX_CACHE:]
                _write(self.config_dir, "easyeda.json", {"parts": dict(newest)})
        for ref, key in self.targets.items():
            if _CODE.fullmatch(key):
                result = by_code[key]
                if isinstance(result, RealPart):
                    self.parts[ref] = result
                else:
                    self.unchecked[ref] = result or ("not found", f"{key} has no footprint data at EasyEDA/LCSC")

    # -- results --
    @property
    def summary(self) -> str:
        if not self.candidates:
            return "Footprints: nothing to check"
        counts = Counter(category for category, _ in self.unchecked.values())
        return "Footprints: " + ", ".join([f"{len(self.parts)} checked"]
                                          + [f"{counts[c]} {c}" for c in CATEGORIES if counts[c]])

    def provider(self) -> Callable[[DesignSnapshot], list[Finding]]:
        """A run_heavy provider: the comparison itself is offline, on what fetch() collected."""
        def fit(snapshot: DesignSnapshot) -> list[Finding]:
            by_ref = {comp.reference: comp for comp in snapshot.components}
            found = []
            for ref, part in self.parts.items():
                if ref in by_ref:
                    for item in compare(ref, kicad_pads(by_ref[ref]), part, pad_nets(by_ref[ref])):
                        found.append(replace(item, evidence=item.evidence + " Source: DigiKey.")
                                     if part.source == "digikey" else item)
            if self.unchecked:
                refs = tuple(sorted(self.unchecked, key=_ref_key))
                lines = [f"{ref}: {self.unchecked[ref][1]}" for ref in refs[:20]]
                found.append(Finding(
                    "fit.unverified", Severity.INFO,
                    f"{len(refs)} part(s) were not checked against the real part's footprint", refs,
                    evidence="; ".join(lines) + (f"; and {len(refs) - 20} more" if len(refs) > 20 else ""),
                    fix="Put an LCSC or manufacturer part number field on these parts and run the heavy "
                        "checks again."))
            return found
        return fit
