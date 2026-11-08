"""The snapshot fields the v2 redesign reads (BRIEF v2, blocker B3). All additive: schema 2."""
import json

import pytest

from auction_agent.display import display_case, display_title
from auction_agent.snapshot import _compact, _public_card, ct_grid, encode
from tests.test_mvp import _publish_seeded, _synthetic_card


def _ct(result, *defects):
    return {"result": result, "defects": [{"code": code, "severity": sev} for code, sev in defects]}


@pytest.mark.parametrize("ct, grid", [
    (_ct("FAVORABLE"), "........."),
    (_ct("FAVORABLE", ("4.5.2.a.1", "minor"), ("5.3.3.a.1", "minor")), "....nn..."),  # lot 538
    (_ct("DEFAVORABLE_MAJEURE", ("1.1.1.a.3", "minor"), ("1.2.1.b.1", "major"), ("6.1.1.a.1", "major")), ".M....M.."),
    (_ct("DEFAVORABLE_CRITIQUE", ("1.1.1.a.3", "major"), ("1.1.2.a.1", "critical"), ("0.1.1.a.1", "minor")), "nC......."),
    # Function 12 (véhicule école) counts in ct_counts but has no cell; nor do malformed codes.
    (_ct("DEFAVORABLE_MAJEURE", ("12.1.1.a.1", "major"), ("x", "major"), ("8.1.1.a.1", "major")), "........M"),
    (_ct("UNKNOWN", ("1.1.1.a.3", "major")), "uuuuuuuuu"),
    (None, "uuuuuuuuu"),
])
def test_ct_grid(ct, grid):
    assert ct_grid(ct) == grid and len(grid) == 9


@pytest.mark.parametrize("raw, shown", [
    # The titles and trims of the design screens, as the auction site writes them.
    ("MINI HATCH 5 PORTES F55 LCI", "MINI Hatch 5 portes F55 LCI"),
    ("COOPER 136 CH FINITION BUSINESS", "Cooper 136 ch Finition Business"),
    ("TCE 130 FAP 4X2 PRESTIGE", "TCe 130 FAP 4x2 Prestige"),
    ("CITROEN C3", "Citroën C3"),
    ("PURETECH 83 S&S BVM5 YOU!", "PureTech 83 S&S BVM5 You!"),
    ("1.0 T-GDI 100 HYBRID 48V INITIA", "1.0 T-GDi 100 Hybrid 48V Initia"),
    ("LAND ROVER RANGE ROVER EVOQUE", "Land Rover Range Rover Evoque"),
    ("BLUEHDI 180 EAT8 EXECUTIVE", "BlueHDi 180 EAT8 Executive"),
    ("35 TFSI COD 150 S TRONIC 7", "35 TFSI COD 150 S tronic 7"),
    ("PHEV 180 E-EAT8 ACTIVE PACK", "PHEV 180 e-EAT8 Active Pack"),
    ("1.6 CRDI 136 HYBRID 48V DCT-7 EXECUTIVE", "1.6 CRDi 136 Hybrid 48V DCT-7 Executive"),
    ("PURETECH 130CH S&S BVM6 GT LINE", "PureTech 130 ch S&S BVM6 GT Line"),
    ("RENAULT MEGANE IV BERLINE", "Renault Mégane IV Berline"),
    ("BMW SERIE 3 G20", "BMW Série 3 G20"),
    ("320E 204 CH BVA8 LOUNGE", "320e 204 ch BVA8 Lounge"),
    ("MERCEDES-BENZ CLASSE A", "Mercedes-Benz Classe A"),
    ("E-TECH 140 - 21N BUSINESS", "E-Tech 140 - 21N Business"),
    ("2.2L SKYACTIV-D 175 CH 4X4 BVA SELECTION", "2.2L Skyactiv-D 175 ch 4x4 BVA Selection"),
    (None, None),
])
def test_display_case(raw, shown):
    assert display_case(raw) == shown


def test_display_title_drops_a_brand_the_model_repeats():
    assert display_title("DS DS7 CROSSBACK EXECUTIVE") == "DS 7 Crossback Executive"
    assert display_title("KIA SPORTAGE") == "Kia Sportage"
    assert display_title("DS DS") == "DS DS"  # nothing to drop without a number


def test_cards_and_compact_lots_carry_the_v2_fields():
    card, compact = _public_card(_synthetic_card()), _compact(_synthetic_card())
    assert card["ct_grid"] == compact["ct_grid"] and len(card["ct_grid"]) == 9
    assert compact["ct_counts"] == {"minor": 15, "major": 15, "critical": 15}
    assert card["title_display"] == compact["title_display"] == "Peugeot 308"
    assert card["subtitle_display"] == "1.2 PureTech"
    assert [r["code"] for r in card["reasons"]] == ["r0", "r1", "r2"]
    assert compact["why_code"] == "r6"
    assert len(encode(compact)) < 500


def test_online_sale_fee_model_has_no_buyer_fee(settings, tmp_path):
    snap = _publish_seeded(settings, tmp_path)  # the seeded sale is online
    assert snap["sale"]["fee_model"] == {"rate": 0.0, "min": 0.0, "vat_rate": 0.2, "venue": "online"}
    assert all(c["detail"]["price"]["fees"] == 0 for c in snap["top"] + snap["cards"])


def test_publish_carries_the_sale_fee_model_sales_and_engines(settings, tmp_path):
    def puretech(conn):  # a room sale; car 1 gets a 1.2 PureTech written the way Alcopa writes it
        conn.execute("UPDATE sales SET kind = 'room'")
        conn.execute("""UPDATE vehicles SET brand = 'PEUGEOT', model = '208', trim = '208 PURETECH 110CH S&S BVM6 ALLURE',
                        fuel = 'petrol', engine_cc = 1199, year = 2019 WHERE id = 1""")

    snap = _publish_seeded(settings, tmp_path, prepare=puretech)
    sale = snap["sale"]
    assert sale["house"] == "alcopa"
    assert sale["fee_model"] == {"rate": 0.144, "min": 360.0, "vat_rate": 0.2, "venue": "room"}
    fees = [c["detail"]["price"] for c in snap["top"] + snap["cards"]]
    assert all(abs(p["fees"] - max(0.144 * p["start"], 360)) < 0.02 for p in fees)  # the model the page uses
    assert snap["sales"] == [dict(sale, lots=sum(snap["counts"][t] for t in ("green", "yellow", "red")))]
    kb = snap["engines"]["stellantis_puretech_eb2_turbo"]
    assert kb["label"] and kb["check"].startswith("Demandez une facture")
    lots = snap["top"] + snap["cards"] + snap["compact"]
    flagged = [c for c in lots if c.get("engine")]
    assert [c["engine"] for c in flagged] == ["stellantis_puretech_eb2_turbo"]
    for c in snap["top"] + snap["cards"]:
        assert len(c["ct_grid"]) == 9 and len(c["gate_params"]) == len(c["gate_codes"]) == len(c["gates"])
        assert isinstance(c["detail"]["price"]["vat_applies"], bool)
        assert all(isinstance(r.get("code"), str) for r in c["reasons"])
        assert c["title_display"] and c["title_display"] != c["title"]
    for c in snap["compact"]:
        assert len(c["ct_grid"]) == 9 and set(c["ct_counts"] or {}) <= {"minor", "major", "critical"}
    # Rule output carries its numbers, so a page in English can word it.
    reasons = [r for c in snap["top"] + snap["cards"] for r in c["detail"]["reasons"]]
    engine = next(r for r in reasons if r["code"] == "engine_kb")
    assert engine["params"] == {"ref": "stellantis_puretech_eb2_turbo"}
    ct = next(r for r in reasons if r["code"] == "ct_favorable")
    assert ct["params"] == {"n": 2}
    raw = json.dumps(snap, ensure_ascii=False)
    assert '"id"' not in raw and "registration" not in raw
