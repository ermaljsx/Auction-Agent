from dataclasses import replace
from datetime import date

import pytest

from auction_agent.ct_parser import parse_controle_technique
from auction_agent.scoring.heuristics import (
    Facts,
    assess,
    build_cohort,
    finalize,
    match_engines,
    value_score,
)
from tests.conftest import read_fixture

TODAY = date(2026, 10, 13)
SALE = date(2026, 10, 19)
FAV = parse_controle_technique(read_fixture("ct", "favorable_with_legend.txt"))
MAJ = parse_controle_technique(read_fixture("ct", "majeure_plural.txt"))
CRIT = parse_controle_technique(read_fixture("ct", "critique.txt"))
fee = lambda price, *_: price  # noqa: E731


def facts(**kw) -> Facts:
    base = dict(
        vehicle_id=1, house="alcopa", brand="TOYOTA", model="YARIS", trim="YARIS 1.5 HYBRIDE",
        fuel="hybrid", gearbox="automatic", year=2019, first_registration="2019-03-15",
        mileage_km=60_000, engine_cc=1490, battery_soh_pct=None, battery_leased=None, warranty=None,
        comments=None, vat_regime="ttc", price=12_000.0, sale_kind="online", sale_date=SALE,
        ct=FAV, ct_doc="parsed",
    )
    base.update(kw)
    return Facts(**base)


def score(f: Facts, settings, V=None, llm=0):
    part = assess(f, settings, TODAY, fee)
    if V is None:
        V, basis, ref = 50.0, "value_unknown:few_comparables", None
    else:
        basis, ref = "model_y1_band:n=5", 12_000.0
    return part, finalize(part, V, basis, ref, llm, settings)


def test_clean_car_with_good_value_is_green(settings):
    part, r = score(facts(), settings, V=80)
    assert r["confidence"] == "full" and r["tier"] == "green"
    assert r["final_score"] >= settings.green_min


def test_unknown_value_is_partial_and_capped_at_yellow(settings):
    _, r = score(facts(), settings)  # no comparables
    assert r["confidence"] == "partial" and r["max_tier"] == "yellow"
    assert r["final_score"] <= settings.green_min - 1


def test_unknown_price_is_partial(settings):
    part, r = score(facts(price=None), settings, V=80)
    assert part.effective_price is None
    assert r["confidence"] == "partial" and r["tier"] != "green"


def test_critical_ct_is_red_whatever_the_price(settings):
    _, r = score(facts(ct=CRIT, mileage_km=210_400), settings, V=100)
    assert r["tier"] == "red" and r["final_score"] <= settings.yellow_min - 21
    assert any(g["code"] == "ct_critique" for g in r["gates"])


def test_major_brake_defect_caps_at_yellow_and_adds_repair_cost(settings):
    part, r = score(facts(ct=MAJ, mileage_km=151_220), settings, V=100)
    assert r["max_tier"] == "yellow" and r["tier"] != "green"
    assert part.effective_price > 12_000  # repair estimate included


def test_unreadable_ct_caps_at_yellow(settings):
    _, r = score(facts(ct=None, ct_doc="unreadable"), settings, V=90)
    assert r["max_tier"] == "yellow" and "ct_non_lu" in r["flags"]


def test_missing_ct_on_old_car_is_partial_but_young_car_is_fine(settings):
    _, old = score(facts(ct=None, ct_doc="missing"), settings, V=80)
    assert old["confidence"] == "partial" and "ct_absent" in old["flags"]
    _, young = score(facts(ct=None, ct_doc="missing", year=2024, first_registration="2024-02-01"), settings, V=80)
    assert young["confidence"] == "full" and "ct_absent" not in young["flags"]


def test_comment_dealbreakers(settings):
    _, r = score(facts(comments="Véhicule VGE, moteur HS"), settings, V=100)
    assert r["tier"] == "red"
    _, r2 = score(facts(comments="Kilométrage non garanti"), settings, V=100)
    assert r2["max_tier"] == "yellow"


def test_engine_kb_matches_puretech_and_caps_at_configured_tier(settings):
    f = facts(brand="PEUGEOT", model="208", trim="208 PURETECH 110 S&S EAT8 ALLURE", fuel="petrol", engine_cc=1199)
    assert [e["id"] for e in match_engines(f)] == ["stellantis_puretech_eb2_turbo"]
    _, r = score(f, settings, V=100)
    assert r["max_tier"] == "yellow"
    _, r_red = score(f, settings.replace(engine_risk_max_tier="red"), V=100)
    assert r_red["tier"] == "red"
    # Non-turbo PureTech and later years are not matched.
    assert match_engines(replace(f, trim="208 1.2 PURETECH 82")) == []
    assert match_engines(replace(f, year=2024)) == []


def test_ct_older_than_six_months_needs_redo_for_private_buyer(settings):
    # FAV inspected 2026-03-12; sale 2026-10-19 + 21 days -> > 6 months.
    part, r = score(facts(), settings, V=80)
    assert "ct_redo" in r["flags"]
    assert part.effective_price == 12_080.0
    part_pro, r_pro = score(facts(), settings.replace(buyer_profile="pro"), V=80)
    assert "ct_redo" not in r_pro["flags"]


def test_ev_battery_gate_skipped_when_battery_is_leased(settings):
    ev = dict(fuel="electric", brand="RENAULT", model="ZOE", trim="ZOE R110", battery_soh_pct=70.0)
    _, r = score(facts(**ev), settings, V=100)
    assert r["tier"] == "red"
    _, leased = score(facts(**ev, comments="Batterie en location"), settings, V=100)
    assert not any(g["code"] == "battery_soh" for g in leased["gates"])


def test_llm_adjustment_cannot_lift_above_gate(settings):
    _, r = score(facts(ct=MAJ), settings, V=100, llm=5)
    assert r["tier"] != "green"


def test_no_invented_defaults(settings):
    part, r = score(facts(year=None, first_registration=None, mileage_km=None), settings, V=80)
    assert "mileage_unknown" in r["flags"] and "age_unknown" in r["flags"]
    assert r["confidence"] == "partial"


def test_age_follows_injected_date(settings):
    old = assess(facts(first_registration="2010-01-01", year=2010), settings, date(2026, 1, 1), fee)
    older = assess(facts(first_registration="2010-01-01", year=2010), settings, date(2027, 6, 1), fee)
    assert older.R < old.R or any(r["code"] == "age" for r in older.reasons)


def test_cohort_value_uses_comparables_and_excludes_self_and_gated(settings):
    cars = [facts(vehicle_id=i, price=10_000.0 + 500 * i) for i in range(1, 7)]
    cars.append(facts(vehicle_id=99, price=1_000.0, ct=CRIT))  # gated: excluded from cohort
    parts = [assess(f, settings, TODAY, fee) for f in cars]
    cohort = build_cohort(parts)
    cheap = assess(facts(vehicle_id=50, price=9_000.0), settings, TODAY, fee)
    V, basis, ref = value_score(cheap, cohort)
    assert basis.startswith("model_y1_band") and V > 50
    assert ref == pytest.approx(11_750.0 + 80)  # comparables also carry the CT-redo cost
