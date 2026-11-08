"""Regression tests for the engine-risk knowledge base (BRIEF v2, blocker B1).

Alcopa trims glue the power to its unit: "3008 1.2 PURETECH 130CH S&S BVM6 ALLURE". A
power figure ended by ``\\b`` never matched those, because a regex word boundary needs a
non-word character after the digits and "C" is a word character. Lots 587, 554, 631 and
505 of the 2026-11-09 Beauvais sale were un-flagged that way.
"""
import json
import re
from pathlib import Path

import pytest

from auction_agent.scoring.heuristics import Facts, load_engine_kb, match_engines

KB_PATH = Path(__file__).resolve().parent.parent / "auction_agent" / "scoring" / "engines.json"
PURETECH = "stellantis_puretech_eb2_turbo"
DV5 = "stellantis_bluehdi_15_dv5"
TCE = "renault_tce_12_h5ft"


def facts(brand: str, model: str, trim: str, fuel: str, cc, year: int) -> Facts:
    return Facts(
        vehicle_id=1, house="alcopa", brand=brand, model=model, trim=trim, fuel=fuel, gearbox="manual",
        year=year, first_registration=None, mileage_km=60_000, engine_cc=cc, battery_soh_pct=None,
        battery_leased=None, warranty=None, comments=None, vat_regime="ttc", price=8_000.0,
        sale_kind="room", sale_date=None, ct=None, ct_doc=None,
    )


def ids(f: Facts) -> list[str]:
    return [e["id"] for e in match_engines(f)]


@pytest.mark.parametrize("model, trim, year", [
    # The four lots of the 2026-11-09 sale, with their trims as stored.
    ("3008", "3008 1.2 PURETECH 130CH S&S BVM6 ALLURE", 2017),  # lot 587
    ("308", "308 PURETECH 110CH S&S BVM6 ALLURE", 2019),  # lot 554
    ("208", "208 PURETECH 110CH S&S BVM6 GT LINE", 2019),  # lot 631
    ("5008", "5008 PURETECH 130CH S&S EAT8 ALLURE", 2022),  # lot 505
    ("208", "208 PURETECH 100CH S&S BVM6 STYLE", 2021),
    ("208", "208 PURETECH 100 S&S BVM6 STYLE", 2021),  # the spaced form still matches
])
def test_puretech_power_glued_to_ch_is_flagged(model, trim, year):
    assert ids(facts("PEUGEOT", model, trim, "petrol", 1199, year)) == [PURETECH]


@pytest.mark.parametrize("trim", ["208 PURETECH 180", "208 PURETECH 83", "208 PURETECH 1300", "208 1.2 PURETECH 82CH"])
def test_other_puretech_powers_are_not_flagged(trim):
    assert ids(facts("PEUGEOT", "208", trim, "petrol", 1199, 2019)) == []


def test_bluehdi_130ch_strong_and_100ch_weak_with_cc():
    assert ids(facts("PEUGEOT", "308", "308 BLUEHDI 130CH S&S EAT8", "diesel", None, 2019)) == [DV5]
    # The weak pattern needs the 1499 cc to tell the 1.5 from the older 1.6.
    assert ids(facts("PEUGEOT", "208", "208 BLUEHDI 100CH S&S BVM6", "diesel", 1499, 2020)) == [DV5]
    assert ids(facts("PEUGEOT", "208", "208 BLUEHDI 100CH S&S BVM6", "diesel", None, 2020)) == []
    assert ids(facts("PEUGEOT", "308", "308 BLUEHDI 1300", "diesel", None, 2019)) == []


def test_tce_130ch_within_its_years():
    assert ids(facts("RENAULT", "CAPTUR", "CAPTUR TCE 120CH EDC", "petrol", 1197, 2015)) == [TCE]
    assert ids(facts("RENAULT", "CAPTUR", "CAPTUR TCE 130CH FAP", "petrol", 1333, 2020)) == []


def test_no_engine_regex_ends_a_number_with_a_word_boundary():
    """Every power figure must end with (?!\\d): a \\b after a digit misses "130CH"."""
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    for entry in kb["engines"]:
        for key in ("text_regex", "weak_regex"):
            pattern = entry["match"].get(key)
            if pattern:
                re.compile(pattern)
                assert not re.search(r"\d\)?\??\\b", pattern), (entry["id"], key, pattern)


def test_every_engine_has_a_buyer_check():
    for entry in load_engine_kb()["engines"]:
        check = entry.get("check_fr")
        assert isinstance(check, str) and check.endswith(".") and len(check) > 20, entry["id"]
