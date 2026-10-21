"""Footprint lookup: identification, EasyEDA and DigiKey fetches, caches, pacing. Fake transports only."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from velatrace import part_lookup
from velatrace.audit_rules import BETA_RULES, run_heavy
from velatrace.findings import Severity
from velatrace.jlc_http import JlcNetError
from velatrace.models import Component, DesignSnapshot, Pin
from velatrace.part_lookup import DigiKeyLogin, PartLookup

FIXTURES = Path(__file__).parent / "fixtures" / "fit"
T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
SECRET = "synthetic-client-secret-1234"
LOGIN = DigiKeyLogin("synthetic-client-id", SECRET)
HEADER = "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical"


def part(ref="J1", value="Conn_01x06", fields=None, pads=6, footprint=HEADER):
    """A 1 x 6 through-hole header (a column of pads)."""
    return Component(ref, value, footprint,
                     tuple(Pin(str(i + 1), "N", offset_mm=(0.0, 2.54 * i), through_hole=True) for i in range(pads)),
                     fields or {})


def snapshot(*components, path=None):
    return DesignSnapshot(tuple(components), "test", path=path)


class Net:
    """Stands in for jlc_http.request on all three hosts and keeps the clock honest."""

    def __init__(self, tokens=("tok-1", "tok-2"), search="digikey_keyword_example.json", easyeda=None):
        self.calls, self.time, self.sleeps, self.tokens = [], 0.0, [], list(tokens)
        self.search = json.loads((FIXTURES / search).read_text(encoding="utf-8")) if search else {"Products": []}
        self.easyeda = easyeda or {}
        self.search_status = []     # statuses to fail the next searches with

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs, self.time))
        self.time += 0.2
        if url.startswith("https://easyeda.com/"):
            code = url.split("/products/")[1].split("/")[0]
            answer = self.easyeda.get(code)
            if isinstance(answer, Exception):
                raise answer
            return 200, {}, json.dumps(answer if answer is not None else json.loads(
                (FIXTURES / f"{code}.json").read_text(encoding="utf-8"))).encode()
        if url == part_lookup.DIGIKEY_TOKEN_URL:
            return 200, {}, json.dumps({"access_token": self.tokens.pop(0), "expires_in": 599,
                                        "token_type": "Bearer"}).encode()
        assert url == part_lookup.DIGIKEY_SEARCH_URL
        if self.search_status:
            error = JlcNetError("api.digikey.com answered HTTP %d." % self.search_status[0])
            error.status = self.search_status.pop(0)
            raise error
        return 200, {}, json.dumps(self.search).encode()

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.time += seconds

    def clock(self):
        return self.time

    def urls(self, host):
        return [url for url, _, _ in self.calls if host in url]

    def run(self, lookup, network=True, **kwargs):
        lookup.fetch(network=network, fetch=self.fetch, sleep=self.sleep, clock=self.clock, now=lambda: T0, **kwargs)
        return lookup


class Catalogue:
    def __init__(self, mpns=None, hits=None):
        self.mpns, self.hits, self.searched = mpns or {}, hits or {}, []

    def find_mpn(self, mpn):
        return self.mpns.get(mpn)

    def search(self, text, **kwargs):
        self.searched.append(text)
        return self.hits.get(text, [])


def cat(code, mpn="X", package="SOIC-8", description="a part"):
    return SimpleNamespace(lcsc=code, mpn=mpn, package=package, description=description)


def nobody(*args):
    raise AssertionError("nobody should have been asked")


def lookup_for(*components, tmp_path, path=None, **kwargs):
    return PartLookup(snapshot(*components, path=path), tmp_path, **kwargs)


# ---- identification ----

def test_lcsc_field_then_mpn_field_then_catalogue_search(tmp_path):
    catalogue = Catalogue(mpns={"RP2040": cat("C2040")}, hits={"STM32": [cat("C8734")]})
    lookup = lookup_for(part("J1", fields={"LCSC": "c65114", "MPN": "RP2040"}),
                        part("J2", fields={"Manufacturer Part Number": "RP2040"}),
                        part("J3", "STM32"), tmp_path=tmp_path, catalogue=catalogue, choose=nobody, ask=nobody)
    lookup.identify()
    assert lookup.targets == {"J1": "C65114", "J2": "C2040", "J3": "C8734"}
    assert catalogue.searched == ["STM32"]      # the LCSC field and the MPN never reach the free-text search


def test_several_catalogue_hits_ask_to_choose_and_the_answer_is_remembered(tmp_path):
    board = tmp_path / "a.kicad_pcb"
    hits = [cat("C1", "AAA"), cat("C2", "BBB")]
    seen = []

    def choose(ref, options):
        seen.append((ref, [o.key for o in options]))
        return options[1]
    first = lookup_for(part("J1", "Conn"), tmp_path=tmp_path, path=board, catalogue=Catalogue(hits={"Conn": hits}),
                       choose=choose, ask=nobody)
    first.identify()
    assert seen == [("J1", ["C1", "C2"])] and first.targets == {"J1": "C2"}
    again = lookup_for(part("J1", "Conn"), tmp_path=tmp_path, path=board, catalogue=Catalogue(hits={"Conn": hits}),
                       choose=nobody, ask=nobody)
    again.identify()
    assert again.targets == {"J1": "C2"}        # no dialog the second time
    other_board = lookup_for(part("J1", "Conn"), tmp_path=tmp_path, path=tmp_path / "b.kicad_pcb",
                             catalogue=Catalogue(hits={"Conn": hits}), choose=lambda ref, options: options[0], ask=nobody)
    other_board.identify()
    assert other_board.targets == {"J1": "C1"}


def test_a_single_catalogue_hit_is_used_without_asking_or_remembering(tmp_path):
    lookup = lookup_for(part("J1", "Conn"), tmp_path=tmp_path, path=tmp_path / "a.kicad_pcb",
                        catalogue=Catalogue(hits={"Conn": [cat("C9")]}), choose=nobody, ask=nobody)
    lookup.identify()
    assert lookup.targets == {"J1": "C9"}
    assert not (tmp_path / "part-fit" / "choices.json").exists()


def test_no_hit_asks_for_a_part_number(tmp_path):
    prompts = []
    answers = iter(["c65114", "RP2040", "  ", "who knows"])
    catalogue = Catalogue(mpns={"RP2040": cat("C2040")})

    def ask(ref, prompt):
        prompts.append((ref, prompt))
        return next(answers)
    lookup = lookup_for(part("J1"), part("J2"), part("J3"), part("J4"), tmp_path=tmp_path,
                        path=tmp_path / "a.kicad_pcb", catalogue=catalogue, ask=ask)
    lookup.identify()
    assert [ref for ref, _ in prompts] == ["J1", "J2", "J3", "J4"] and "J1 (Conn_01x06" in prompts[0][1]
    assert lookup.targets == {"J1": "C65114", "J2": "C2040"}
    assert lookup.unchecked["J3"][0] == "skipped" and lookup.unchecked["J4"][0] == "not found"
    # skips and good answers are remembered; an answer nobody can use is asked again next time
    again = lookup_for(part("J1"), part("J2"), part("J3"), part("J4"), tmp_path=tmp_path,
                       path=tmp_path / "a.kicad_pcb", catalogue=catalogue, ask=lambda ref, prompt: None)
    again.identify()
    assert again.targets == lookup.targets and again.unchecked["J3"][0] == "skipped"
    assert again.unchecked["J4"][0] == "skipped"        # this time the user pressed Skip


def test_declining_a_choice_is_a_remembered_skip(tmp_path):
    lookup = lookup_for(part("J1", "Conn"), tmp_path=tmp_path, path=tmp_path / "a.kicad_pcb",
                        catalogue=Catalogue(hits={"Conn": [cat("C1"), cat("C2")]}), choose=lambda ref, options: None)
    lookup.identify()
    assert lookup.unchecked == {"J1": ("skipped", "you skipped it")}
    saved = json.loads((tmp_path / "part-fit" / "choices.json").read_text(encoding="utf-8"))
    assert list(saved["boards"].values()) == [{"J1": "skip"}]


def test_passives_and_excluded_parts_are_never_looked_up(tmp_path):
    resistor = Component("R1", "10k", "Resistor_SMD:R_0402_1005Metric",
                         (Pin("1", "A", offset_mm=(-0.5, 0)), Pin("2", "B", offset_mm=(0.5, 0))), {})
    lookup = lookup_for(resistor, part("J1"), part("J2"), tmp_path=tmp_path, excluded={"J2"}, ask=nobody)
    assert [c.reference for c in lookup.candidates] == ["J1"]


def test_choices_file_is_capped_and_a_damaged_one_is_ignored(tmp_path):
    folder = tmp_path / "part-fit"
    folder.mkdir()
    (folder / "choices.json").write_text("not json", encoding="utf-8")
    lookup = lookup_for(part("J1"), tmp_path=tmp_path, path=tmp_path / "a.kicad_pcb", ask=lambda ref, prompt: "C1")
    lookup.identify()
    assert lookup.targets == {"J1": "C1"}
    for index in range(part_lookup.MAX_CHOICES + 5):
        lookup._remember(f"U{index}", "C1")
    saved = json.loads((folder / "choices.json").read_text(encoding="utf-8"))
    assert len(next(iter(saved["boards"].values()))) == part_lookup.MAX_CHOICES


# ---- EasyEDA ----

def test_easyeda_is_asked_once_per_part_politely_and_the_parsed_pads_are_cached(tmp_path):
    net = Net()
    lookup = lookup_for(part("J1", fields={"LCSC": "C65114"}), part("J2", fields={"LCSC": "C65114"}),
                        part("J3", fields={"LCSC": "C124378"}), tmp_path=tmp_path)
    lookup.identify()
    assert lookup.needs_network(now=lambda: T0) == (2, 0)
    net.run(lookup)
    assert net.urls("easyeda.com") == [part_lookup.EASYEDA_URL.format("C65114"),
                                       part_lookup.EASYEDA_URL.format("C124378")]
    call = net.calls[0][1]
    assert call["max_bytes"] <= 1_000_000 and "method" not in call and "body" not in call
    assert net.sleeps == [0.8]                                  # 1 s between request starts, 0.2 s per request
    assert sorted(lookup.parts) == ["J1", "J2", "J3"] and lookup.parts["J1"].lcsc == "C65114"
    saved = json.loads((tmp_path / "part-fit" / "easyeda.json").read_text(encoding="utf-8"))
    assert set(saved["parts"]) == {"C65114", "C124378"} and len(saved["parts"]["C65114"]["pads"]) == 6
    assert "packageDetail" not in json.dumps(saved) and "shape" not in json.dumps(saved)   # parsed fields only
    again = lookup_for(part("J1", fields={"LCSC": "C65114"}), tmp_path=tmp_path)
    again.identify()
    assert again.needs_network(now=lambda: T0) == (0, 0)
    Net().run(again, network=False)
    assert len(again.parts["J1"].pads) == 6


def test_cached_only_opens_nothing_and_says_what_was_not_looked_up(tmp_path):
    lookup = lookup_for(part("J1", fields={"LCSC": "C65114"}), tmp_path=tmp_path, digikey=LOGIN)
    lookup.identify()

    def no_network(*args, **kwargs):
        raise AssertionError("network")
    lookup.fetch(network=False, fetch=no_network)
    assert lookup.unchecked["J1"][0] == "not looked up" and lookup.summary == "Footprints: 0 checked, 1 not looked up"


def test_an_unknown_part_is_cached_as_missing_for_a_while_and_a_bad_answer_is_a_failure(tmp_path):
    net = Net(easyeda={"C1": {"success": False}, "C2": JlcNetError("easyeda.com timed out.")})
    lookup = lookup_for(part("J1", fields={"LCSC": "C1"}), part("J2", fields={"LCSC": "C2"}), tmp_path=tmp_path)
    lookup.identify()
    net.run(lookup)
    assert lookup.unchecked["J1"][0] == "not found" and lookup.unchecked["J2"] == ("failed", "easyeda.com timed out.")
    again = lookup_for(part("J1", fields={"LCSC": "C1"}), part("J2", fields={"LCSC": "C2"}), tmp_path=tmp_path)
    again.identify()
    assert again.needs_network(now=lambda: T0) == (1, 0)                       # C1 is remembered as missing
    assert again.needs_network(now=lambda: T0 + part_lookup.MISSING_AGE + timedelta(days=1)) == (2, 0)


def test_three_failures_in_a_row_stop_the_run_and_the_limit_is_honoured(tmp_path):
    net = Net(easyeda={f"C{i}": JlcNetError("easyeda.com timed out.") for i in range(1, 6)})
    lookup = lookup_for(*(part(f"J{i}", fields={"LCSC": f"C{i}"}) for i in range(1, 6)), tmp_path=tmp_path)
    lookup.identify()
    net.run(lookup)
    assert len(net.urls("easyeda.com")) == part_lookup.MAX_FAILURES
    assert [lookup.unchecked[f"J{i}"][1] for i in (4, 5)] == ["not asked after repeated failures"] * 2
    many = lookup_for(*(part(f"J{i}", fields={"LCSC": f"C{i + 10}"}) for i in range(1, part_lookup.MAX_PARTS + 3)),
                      tmp_path=tmp_path / "many")
    many.identify()
    assert many.needs_network(now=lambda: T0) == (part_lookup.MAX_PARTS, 0)
    Net(easyeda={f"C{i + 10}": {"success": False} for i in range(1, part_lookup.MAX_PARTS + 3)}).run(many)
    assert sum("limit" in why for _, why in many.unchecked.values()) == 2


def test_a_different_part_in_the_answer_is_refused(tmp_path):
    other = json.loads((FIXTURES / "C124378.json").read_text(encoding="utf-8"))
    lookup = lookup_for(part("J1", fields={"LCSC": "C65114"}), tmp_path=tmp_path)
    lookup.identify()
    Net(easyeda={"C65114": other}).run(lookup)
    assert lookup.unchecked["J1"] == ("failed", "easyeda.com answered with a different part.")


def test_mic_header_end_to_end_gives_a_layout_note(tmp_path):
    """A 1 x 6 footprint on a part LCSC says is the 2 x 3 header C65114."""
    board = snapshot(part("J1", fields={"LCSC": "C65114"}))
    lookup = PartLookup(board, tmp_path)
    lookup.identify()
    Net().run(lookup)
    found = run_heavy(board, providers=[lookup.provider()])
    assert [(item.rule, item.severity) for item in found] == [("fit.layout", Severity.INFO)]
    assert "footprint 6 × 1, part 2 × 3" in found[0].title and lookup.summary == "Footprints: 1 checked"


# ---- DigiKey ----

def digikey_lookup(tmp_path, *, ask=nobody, choose=nobody, **kwargs):
    lookup = lookup_for(part("J1", "EXAMPLE-2X3-HDR"), tmp_path=tmp_path, path=tmp_path / "a.kicad_pcb",
                        catalogue=Catalogue(), digikey=LOGIN, choose=choose, ask=ask, **kwargs)
    lookup.identify()
    return lookup


def test_parts_not_on_lcsc_go_to_digikey_when_credentials_exist(tmp_path):
    lookup = digikey_lookup(tmp_path)
    assert lookup.targets == {"J1": "mpn:EXAMPLE-2X3-HDR"} and lookup.needs_network(now=lambda: T0) == (0, 1)
    net = Net()
    net.run(lookup)
    token, search = net.calls
    assert token[0] == part_lookup.DIGIKEY_TOKEN_URL and token[1]["method"] == "POST"
    assert token[1]["content_type"] == "application/x-www-form-urlencoded"
    assert token[1]["body"].decode() == (f"client_id=synthetic-client-id&client_secret={SECRET}"
                                         "&grant_type=client_credentials")
    assert search[0] == part_lookup.DIGIKEY_SEARCH_URL and search[1]["content_type"] == "application/json"
    assert json.loads(search[1]["body"]) == {"Keywords": "EXAMPLE-2X3-HDR", "Limit": 10, "Offset": 0}
    assert search[1]["headers"]["Authorization"] == "Bearer tok-1"
    assert search[1]["headers"]["X-DIGIKEY-Client-Id"] == "synthetic-client-id"
    real = lookup.parts["J1"]
    assert (real.source, real.title, real.pin_count, real.through_hole, real.rows, real.pads) == (
        "digikey", "EXAMPLE-2X3-HDR", 6, True, 2, ())
    assert lookup.summary == "Footprints: 1 checked"


def test_digikey_findings_say_where_they_came_from_and_flag_a_wrong_layout(tmp_path):
    board = snapshot(part("J1", "EXAMPLE-2X3-HDR"))      # a single-row footprint for a 2-row connector
    lookup = PartLookup(board, tmp_path, digikey=LOGIN, ask=nobody)
    lookup.identify()
    Net().run(lookup)
    found = run_heavy(board, providers=[lookup.provider()])
    assert [item.rule for item in found] == ["fit.layout"] and found[0].severity == Severity.INFO
    assert found[0].evidence.endswith("Source: DigiKey.") and "1 row(s)" not in found[0].title


def test_token_is_reused_refreshed_on_401_and_calls_are_spaced(tmp_path):
    lookup = lookup_for(part("J1", "EXAMPLE-2X3-HDR"), part("J2", "EXAMPLE-1X6-HDR"), tmp_path=tmp_path,
                        digikey=LOGIN, ask=nobody, choose=nobody)
    lookup.identify()
    net = Net()
    net.run(lookup)
    assert [url.rsplit("/", 1)[1] for url, _, _ in net.calls] == ["token", "keyword", "keyword"]
    starts = [started for _, _, started in net.calls]
    assert all(b - a >= part_lookup.DIGIKEY_INTERVAL - 1e-9 for a, b in zip(starts, starts[1:]))
    expired = Net(tokens=("old", "new"))
    expired.search_status = [401]
    again = digikey_lookup(tmp_path / "again")
    expired.run(again)
    assert [url.rsplit("/", 1)[1] for url, _, _ in expired.calls] == ["token", "keyword", "token", "keyword"]
    assert expired.calls[3][1]["headers"]["Authorization"] == "Bearer new" and "J1" in again.parts


def test_ambiguous_digikey_results_ask_and_the_answer_is_remembered(tmp_path):
    seen = []

    def choose(ref, options):
        seen.append((ref, [o.key for o in options]))
        return options[1]
    lookup = lookup_for(part("J1", "EXAMPLE"), tmp_path=tmp_path, path=tmp_path / "a.kicad_pcb", digikey=LOGIN,
                        catalogue=Catalogue(), ask=nobody)
    lookup.identify()
    Net().run(lookup, choose=choose)
    assert seen == [("J1", ["mpn:EXAMPLE-2X3-HDR", "mpn:EXAMPLE-1X6-HDR", "mpn:EXAMPLE-NO-DATA"])]
    assert lookup.parts["J1"].title == "EXAMPLE-1X6-HDR"
    again = lookup_for(part("J1", "EXAMPLE"), tmp_path=tmp_path, path=tmp_path / "a.kicad_pcb", digikey=LOGIN,
                       catalogue=Catalogue(), ask=nobody)
    again.identify()
    assert again.targets == {"J1": "mpn:EXAMPLE-1X6-HDR"}
    Net().run(again)                                           # exact match: no dialog
    assert again.parts["J1"].title == "EXAMPLE-1X6-HDR"


def test_digikey_without_results_asks_for_another_part_number_or_skips(tmp_path):
    lookup = lookup_for(part("J1", "NOTHING"), part("J2", "NOTHING2"), tmp_path=tmp_path, digikey=LOGIN,
                        catalogue=Catalogue(), ask=nobody)
    lookup.identify()
    prompts = []

    def ask(ref, prompt):
        prompts.append(ref)
        return "EXAMPLE-2X3-HDR" if ref == "J1" else None
    net = Net(search=None)
    original, found = net.fetch, json.loads((FIXTURES / "digikey_keyword_example.json").read_text(encoding="utf-8"))

    def fetch(url, **kwargs):      # only the typed number finds something
        typed = url == part_lookup.DIGIKEY_SEARCH_URL and json.loads(kwargs["body"])["Keywords"] == "EXAMPLE-2X3-HDR"
        net.search = found if typed else {"Products": []}
        return original(url, **kwargs)
    net.fetch = fetch
    net.run(lookup, ask=ask)
    assert prompts == ["J1", "J2"] and lookup.parts["J1"].title == "EXAMPLE-2X3-HDR"
    assert lookup.unchecked["J2"][0] == "skipped"


def test_digikey_needs_credentials_and_an_unusable_record_is_not_found(tmp_path):
    no_login = lookup_for(part("J1", "EXAMPLE-2X3-HDR"), tmp_path=tmp_path, catalogue=Catalogue(), ask=lambda r, p: "X1")
    no_login.identify()
    assert no_login.targets == {}          # nothing to look it up with: not found, asked once
    parked = lookup_for(part("J1", "EXAMPLE-NO-DATA"), tmp_path=tmp_path / "p", digikey=LOGIN, catalogue=Catalogue())
    parked.identify()
    Net().run(parked)
    assert parked.unchecked["J1"] == ("not found", "DigiKey lists no pin count for it")


def test_digikey_data_and_credentials_never_reach_the_disk_or_the_findings(tmp_path):
    board = snapshot(part("J1", "EXAMPLE-2X3-HDR"), path=tmp_path / "a.kicad_pcb")
    lookup = PartLookup(board, tmp_path, digikey=LOGIN, catalogue=Catalogue(), ask=nobody)
    lookup.identify()
    net = Net()
    net.run(lookup)
    found = run_heavy(board, providers=[lookup.provider()])
    for path in tmp_path.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "EXAMPLE" not in text and "CONN HEADER" not in text and SECRET not in text, path
    shown = repr(found) + lookup.summary + repr(LOGIN) + repr(lookup.digikey) + repr(lookup.parts)
    assert SECRET not in shown and "tok-1" not in shown
    failing = Net(tokens=("t",))
    failing.search_status = [429]
    again = PartLookup(board, tmp_path / "x", digikey=LOGIN, catalogue=Catalogue(), ask=nobody)
    again.identify()
    failing.run(again)
    assert again.unchecked["J1"] == ("failed", "api.digikey.com answered HTTP 429.")
    assert SECRET not in repr(again.unchecked) + repr(found)


def test_malformed_digikey_answers_are_failures_not_crashes(tmp_path):
    for search in ({"Products": "x"}, {"nothing": 1}, {"Products": [1, {"ManufacturerProductNumber": "A"}]}):
        lookup = digikey_lookup(tmp_path / str(len(repr(search))))
        net = Net(search=None)
        net.search = search
        net.run(lookup, ask=lambda ref, prompt: None)
        assert lookup.unchecked["J1"][0] in {"failed", "not found", "skipped"} and not lookup.parts


# ---- summary and the unverified note ----

def test_unverified_parts_are_one_info_finding_and_the_summary_counts_them(tmp_path):
    board = snapshot(part("J1", fields={"LCSC": "C65114"}), part("J2", fields={"LCSC": "C404"}),
                     part("J3"), part("J4"), part("J5", fields={"LCSC": "C5"}))
    answers = {"J3": None, "J4": "ZZZ"}
    lookup = PartLookup(board, tmp_path, ask=lambda ref, prompt: answers[ref])
    lookup.identify()
    Net(easyeda={"C404": {"success": False}, "C5": JlcNetError("easyeda.com timed out.")}).run(lookup)
    assert lookup.summary == "Footprints: 1 checked, 2 not found, 1 skipped, 1 failed"
    found = [item for item in run_heavy(board, providers=[lookup.provider()]) if item.rule == "fit.unverified"]
    assert len(found) == 1 and found[0].severity == Severity.INFO and "fit.unverified" in BETA_RULES
    assert found[0].refs == ("J2", "J3", "J4", "J5") and "J2: C404 has no footprint data" in found[0].evidence
    assert PartLookup(snapshot(), tmp_path).summary == "Footprints: nothing to check"


def test_nothing_unchecked_means_no_unverified_finding(tmp_path):
    board = snapshot(part("J1", fields={"LCSC": "C65114"}))
    lookup = PartLookup(board, tmp_path)
    lookup.identify()
    Net().run(lookup)
    assert "fit.unverified" not in {item.rule for item in lookup.provider()(board)}
