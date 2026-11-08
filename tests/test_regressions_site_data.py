"""Regression tests for the data the site redesign had to work around (2026-11-08):
the price basis, the fees note, the young-car CT bonus after a failed CT, and the codes the
page can read instead of matching French text."""
import math

import pytest

from auction_agent.adapters.alcopa import AlcopaAdapter
from auction_agent.ct_parser import RESULT_CRITICAL, RESULT_MAJOR
from auction_agent.report import ct_line
from auction_agent.scoring.heuristics import REPAIR_COST_MAJOR, WEIGHTS, assess
from auction_agent.scoring.service import _price_basis
from auction_agent.snapshot import PARTIAL_FR, _price, _public_card, fees_note
from tests.test_mvp import _publish_seeded, _synthetic_card
from tests.test_scoring import FAV, TODAY, facts, score

NBSP = " "
YOUNG = dict(year=2024, first_registration="2024-02-01")  # under 4 years old at the sale


# ------------------------------------------------------------------ 1. price basis

@pytest.mark.parametrize("starting, current, basis", [
    (None, None, None),        # no price at all
    (8000, 8000, "start"),     # data-current-price is the opening price until someone bids
    (8000, None, "start"),
    (8000, 8300, "bid"),       # above a known opening price: a running bid
    (8000, 7700, "start"),     # below it: the opening price is the price
    (None, 9200, "shown"),     # no opening price: may be the opening price itself, or a bid
    (0, 9200, "shown"),
])
def test_price_basis_is_a_bid_only_above_a_known_opening_price(starting, current, basis):
    assert _price_basis({"starting_price": starting, "current_price_attr": current}) == basis


def test_snapshot_keeps_the_shown_basis_and_defaults_the_unknown_to_start():
    parts = {"start": 9200, "vat": 0, "fees": 1324.8, "repairs": 0, "ct_redo": 0, "total": 10524.8}
    assert _price({**parts, "basis": "shown"})["basis"] == "shown"
    assert _price({**parts, "basis": "bid"})["basis"] == "bid"
    assert _price({**parts, "basis": "estimate"})["basis"] == "start"
    assert _price(parts)["basis"] == "start"


# ------------------------------------------------------------------ 2. fees note

def test_fees_note_is_an_estimated_cost_and_says_what_it_leaves_out(settings):
    note, pro = fees_note(settings.replace(buyer_profile="particulier")), fees_note(settings.replace(buyer_profile="pro"))
    for text in (note, pro):
        assert text.startswith(f"Coût estimé = mise à prix + frais acheteur (14,4{NBSP}% TTC, au moins 360{NBSP}€ en salle)")
        assert "tout compris" not in text.lower()
        assert "Non compris : frais fixes par lot, transport, carte grise." in text
        assert "réparations demandées par le CT (forfait)" in text
    assert "TVA quand un particulier ne peut pas la récupérer" in note and "TVA" not in pro


def test_fees_note_follows_the_adapter_rates(settings, monkeypatch):
    monkeypatch.setattr(AlcopaAdapter, "ROOM_FEE_RATE_TTC", 0.155)
    monkeypatch.setattr(AlcopaAdapter, "ROOM_FEE_MIN_TTC", 400.0)
    assert f"(15,5{NBSP}% TTC, au moins 400{NBSP}€ en salle)" in fees_note(settings)


# ------------------------------------------------------------------ 3. young car, failed CT

def _failed_ct(nature="volontaire", result=RESULT_MAJOR, confidence=0.6, critical=0):
    """A CT like lot 847's on the 2026-11-09 sale: 6 majors and 5 minors, no safety system."""
    defects = [{"code": f"4.{i}.1.a.1", "severity": "major", "system": "eclairage", "label": "Feu"} for i in range(6)]
    defects += [{"code": f"8.{i}.1.a.1", "severity": "minor", "system": "nuisances", "label": "Bruit"} for i in range(5)]
    defects += [{"code": f"1.{i}.1.a.3", "severity": "critical", "system": "freinage", "label": "Frein"}
                for i in range(critical)]
    return {"result": result, "nature": nature, "parse_confidence": confidence, "inspection_date": "2026-09-01",
            "counts": {"minor": 5, "major": 6, "critical": critical}, "defects": defects}


def _codes(part):
    return {r["code"] for r in part.reasons}


def test_young_car_with_a_failed_voluntary_ct_gets_no_young_car_bonus(settings):
    failed, r = score(facts(**YOUNG, ct=_failed_ct()), settings, V=80)
    passed, _ = score(facts(**YOUNG, ct=dict(FAV, nature="volontaire", parse_confidence=0.6)), settings, V=80)
    assert "ct_not_required" in _codes(passed) and passed.C == 85
    assert "ct_not_required" not in _codes(failed) and failed.C == 70  # neutral: no bonus, no other weight
    reason = next(x for x in failed.reasons if x["code"] == "ct_young_failed")
    assert reason["delta"] == 0.0 and reason["pillar"] == "C" and "CT volontaire défavorable" in reason["text"]
    # Everything else is read as before: no gate for non-safety majors, repairs in the cost, full data.
    assert r["gates"] == [] and r["confidence"] == "full" and "ct_redo" not in r["flags"]
    assert failed.price_parts["repairs"] == 6 * REPAIR_COST_MAJOR["eclairage"]
    assert r["base_score"] == round(WEIGHTS["R"] * failed.R + WEIGHTS["C"] * 70 + WEIGHTS["V"] * 80)


def test_young_car_with_a_failed_ct_keeps_its_ct_gates(settings):
    _, critical = score(facts(**YOUNG, ct=_failed_ct(result=RESULT_CRITICAL, critical=1)), settings, V=100)
    assert critical["tier"] == "red" and "ct_critique" in {g["code"] for g in critical["gates"]}
    assert "ct_young_failed" in {x["code"] for x in critical["reasons"]}


def test_failed_ct_read_with_doubt_says_ct_defavorable_without_voluntary(settings):
    part, _ = score(facts(**YOUNG, ct=_failed_ct(nature="periodique", confidence=0.6)), settings, V=80)
    reason = next(x for x in part.reasons if x["code"] == "ct_young_failed")
    assert "CT défavorable" in reason["text"] and "volontaire" not in reason["text"] and part.C == 70


@pytest.mark.parametrize("ct", [
    None,                                                                # no CT published
    dict(FAV, nature="volontaire", parse_confidence=0.6),                # passed
    dict(_failed_ct(), result="UNKNOWN"),                                # result not read
])
def test_young_car_keeps_the_bonus_without_a_failed_ct(settings, ct):
    part, _ = score(facts(**YOUNG, ct=ct, ct_doc="parsed" if ct else "missing"), settings, V=80)
    assert "ct_not_required" in _codes(part) and "ct_young_failed" not in _codes(part) and part.C == 85


def test_older_car_with_a_failed_voluntary_ct_is_unchanged(settings):
    part, _ = score(facts(ct=_failed_ct()), settings, V=80)  # 2019: a CT is required
    assert "ct_non_lu" in _codes(part) and "ct_young_failed" not in _codes(part) and part.C == 55


@pytest.mark.parametrize("ct, expected", [
    ({"result": "DEFAVORABLE_MAJEURE", "nature": "volontaire", "counts": {"minor": 5, "major": 6}},
     "CT volontaire défavorable, 6 défaillances majeures (pas encore exigé)"),
    ({"result": "DEFAVORABLE_CRITIQUE", "nature": "volontaire", "counts": {"critical": 1, "major": 2}},
     "CT volontaire défavorable, 1 défaillance critique et 2 défaillances majeures (pas encore exigé)"),
    ({"result": "DEFAVORABLE_MAJEURE", "nature": "periodique", "counts": {}}, "CT défavorable (pas encore exigé)"),
])
def test_ct_line_of_a_young_car_with_a_failed_ct(ct, expected):
    """The same line the site's ctLineOf builds, so the page shows the same text either way."""
    assert ct_line(ct, [{"code": "ct_young_failed"}]) == expected


def test_failed_voluntary_ct_on_a_young_car_through_publish(settings, tmp_path):
    import json

    def young_failed(conn):
        conn.execute("UPDATE vehicles SET year = 2024, first_registration = '2024-02-01' WHERE id = 1")
        conn.execute("UPDATE documents SET parsed_json = ? WHERE vehicle_id = 1 AND doc_type = 'ct'",
                     (json.dumps(_failed_ct()),))

    snap = _publish_seeded(settings, tmp_path, prepare=young_failed)
    car = next(c for c in snap["top"] + snap["cards"] if c["mileage"] == 60_000)
    assert car["ct_result"] == "DEFAVORABLE_MAJEURE"
    assert car["ct_line"] == "CT volontaire défavorable, 6 défaillances majeures (pas encore exigé)"
    codes = [r["code"] for r in car["detail"]["reasons"]]
    assert "ct_young_failed" in codes and "ct_not_required" not in codes
    assert car["detail"]["price"]["repairs"] == 6 * REPAIR_COST_MAJOR["eclairage"]


# ------------------------------------------------------------------ 4. codes and the fee rate

def test_fee_split_gives_the_fee_rate_of_the_sale_kind():
    a = AlcopaAdapter(None)
    assert a.fee_split(2000, "room", "particulier", "ttc")["fee_rate"] == 0.144  # at the 360 € minimum too
    assert a.fee_split(10000, "room", "pro", "ttc")["fee_rate"] == 0.144
    assert a.fee_split(10000, "online", "particulier", "ttc")["fee_rate"] == 0.0


def test_price_parts_carry_the_fee_rate(settings):
    a = AlcopaAdapter(None)
    part = assess(facts(sale_kind="room"), settings, TODAY, a.fee_model, fee_split=a.fee_split)
    assert part.price_parts["fee_rate"] == 0.144 and part.price_parts["fees"] == round(12_000 * 0.144, 2)
    assert assess(facts(), settings, TODAY, lambda p, *_: p + 500).price_parts["fee_rate"] is None


@pytest.mark.parametrize("raw, expected", [(0.144, 0.144), (0.0, 0.0), (0.12345, 0.1235), (None, None), (True, None),
                                           (math.nan, None), (1.5, None), (-0.1, None), ("0.144", None)])
def test_snapshot_fee_rate_is_not_rounded_to_the_cent(raw, expected):
    parts = {"start": 8900, "vat": 0, "fees": 1281.6, "repairs": 0, "ct_redo": 0, "total": 10181.6, "fee_rate": raw}
    assert _price(parts)["fee_rate"] == expected


def test_room_sale_publishes_the_fee_rate(settings, tmp_path):
    snap = _publish_seeded(settings, tmp_path, prepare=lambda conn: conn.execute("UPDATE sales SET kind = 'room'"))
    cars = snap["top"] + snap["cards"]
    assert cars
    for c in cars:
        p = c["detail"]["price"]
        assert p["fee_rate"] == 0.144
        assert abs(p["fees"] - max(0.144 * p["start"], 360.0)) < 0.02


def test_gate_codes_and_incomplete_keys_follow_their_texts():
    card = _synthetic_card()
    card["gates"] = [{"code": "engine_risk", "text": "Motorisation à risque connu : x"},
                     {"code": "odometer", "text": ""},  # no text: listed in neither
                     {"code": "mileage_not_guaranteed", "text": "Kilométrage non garanti"}]
    card["flags"] = ["mileage_unknown", "value_unknown", "price_unknown", "battery_leased"]
    out = _public_card(card)
    assert out["gates"] == ["Motorisation à risque connu : x", "Kilométrage non garanti"]
    assert out["gate_codes"] == ["engine_risk", "mileage_not_guaranteed"]
    d = out["detail"]
    assert d["incomplete_keys"] == ["price_unknown", "value_unknown", "mileage_unknown"]  # PARTIAL_FR order
    assert d["incomplete"] == [PARTIAL_FR[k] for k in d["incomplete_keys"]]
    assert [r["code"] for r in d["reasons"]] == [f"r{i}" for i in range(6, -1, -1)]  # by points, as before
    card["confidence"] = "full"
    assert _public_card(card)["detail"]["incomplete_keys"] == []
