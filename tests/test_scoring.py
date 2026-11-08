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


# ------------------------------------------------------------------ battery certificate (h1-2026.11)

from auction_agent.scoring.heuristics import (  # noqa: E402
    HEURISTIC_VERSION,
    _warranty_text,
    damage_summary,
    pick_battery_soh,
)

CERT = {"soh_pct": 95.35, "parse_confidence": 1.0}


@pytest.mark.parametrize("listing,cert,expected", [
    (None, None, (None, None)),
    (95.0, None, (95.0, "listing")),
    (None, {"soh_pct": 72.4, "parse_confidence": 1.0}, (72.4, "certificate")),
    (95.0, CERT, (95.0, "listing")),  # the listing shows floor(certificate)
    (95.0, {"soh_pct": 88.0, "parse_confidence": 1.0}, (88.0, "lowest")),
    (80.0, {"soh_pct": 91.0, "parse_confidence": 1.0}, (80.0, "lowest")),
    (95.0, {"soh_pct": 60.0, "parse_confidence": 0.6}, (95.0, "listing")),  # unsure reading ignored
    (None, {"soh_pct": 60.0, "parse_confidence": 0.6}, (None, None)),
    (None, {"soh_pct": None, "parse_confidence": 1.0}, (None, None)),
])
def test_pick_battery_soh(listing, cert, expected):
    assert pick_battery_soh(listing, cert) == expected


EV = dict(fuel="electric", brand="RENAULT", model="ZOE", trim="ZOE R110")


def test_certificate_alone_gates_a_degraded_battery(settings):
    part, r = score(facts(**EV, battery_cert={"soh_pct": 72.4, "parse_confidence": 1.0}), settings, V=100)
    gate = next(g for g in r["gates"] if g["code"] == "battery_soh")
    assert r["tier"] == "red" and gate["text"] == "Batterie dégradée (SoH 72 %)" and gate["src"] == "battery"
    reason = next(x for x in part.reasons if x["code"] == "battery_soh")
    assert reason["text"] == "État de santé batterie 72 %" and reason["evidence"]["src"] == "battery"


def test_soh_is_shown_rounded_down_and_gates_are_unchanged(settings):
    _, r = score(facts(**EV, battery_soh_pct=74.6, battery_soh_source="certificate"), settings, V=100)
    assert next(g["text"] for g in r["gates"] if g["code"] == "battery_soh") == "Batterie dégradée (SoH 74 %)"
    _, ok = score(facts(**EV, battery_soh_pct=75.0, battery_soh_source="listing"), settings, V=100)
    assert not any(g["code"] == "battery_soh" for g in ok["gates"])


def test_agreeing_certificate_keeps_the_listing_value(settings):
    part, r = score(facts(**EV, battery_soh_pct=95.0, battery_cert=CERT), settings, V=80)
    assert "battery_soh_mismatch" not in r["flags"] and not any(x["code"].startswith("battery") for x in part.reasons)


def test_disagreeing_certificate_uses_the_lower_value_and_says_so(settings):
    cert = {"soh_pct": 88.0, "parse_confidence": 1.0}
    part, r = score(facts(**EV, battery_soh_pct=95.0, battery_cert=cert), settings, V=80)
    assert "battery_soh_mismatch" in r["flags"]
    texts = {x["code"]: x for x in part.reasons}
    assert texts["battery_soh"]["text"] == "État de santé batterie 88 %"
    assert texts["battery_soh_mismatch"]["text"] == \
        "SoH annoncé et SoH du certificat batterie différents : 88 % retenu (valeur la plus basse)"
    assert texts["battery_soh_mismatch"]["delta"] == 0.0


def test_unsure_certificate_is_ignored_and_leased_battery_still_exempt(settings):
    unsure = {"soh_pct": 60.0, "parse_confidence": 0.6}
    _, r = score(facts(**EV, battery_soh_pct=95.0, battery_cert=unsure), settings, V=80)
    assert not any(g["code"] == "battery_soh" for g in r["gates"]) and "battery_soh_mismatch" not in r["flags"]
    low = {"soh_pct": 70.0, "parse_confidence": 1.0}
    _, leased = score(facts(**EV, battery_cert=low, comments="Batterie en location"), settings, V=100)
    assert not any(g["code"] == "battery_soh" for g in leased["gates"])


# ------------------------------------------------------------------ body damage (h1-2026.11)

def dmg(*pairs):
    return [{"zone": z, "kind": k} for z, k in pairs]


# The two real pages measured on 2026-11-07, as zone/kind pairs.
LIKE_3008 = dmg(("Jante avant gauche", "Peinture"), ("Jante arrière gauche", "Peinture"),
                ("Aile arrière gauche", "Peinture"), ("Coffre", "Peinture"), ("Coffre", "Peinture"),
                ("Pare-choc arrière", "Peinture"), ("Aile arrière droite", "Peinture"),
                ("Jante arrière droite", "Peinture"), ("Jante avant droite", "Peinture"))
LIKE_ARKANA = dmg(("Jante avant gauche", "Peinture"), ("Aile avant gauche", "Peinture"),
                  ("Porte avant gauche", "Peinture"), ("Porte avant gauche", "Peinture"),
                  ("Porte arrière gauche", "Peinture"), ("Porte arrière gauche", "Débosselage"),
                  ("Jante arrière gauche", "Peinture"), ("Pare-choc arrière", "Peinture"),
                  ("Jante arrière droite", "Peinture"), ("Porte avant droite", "Peinture"),
                  ("Retroviseur droit", "Peinture"), ("Jante avant droite", "Peinture"),
                  ("Pare-choc avant", "Peinture"), *[("Capot", "Peinture")] * 8)


def damage_reason(part):
    return next((x for x in part.reasons if x["code"] == "body_damage"), None)


def test_a_few_marks_cost_one_condition_point(settings):
    clean, _ = score(facts(), settings, V=80)
    part, r = score(facts(damages=LIKE_3008), settings, V=80)
    reason = damage_reason(part)
    assert part.C == clean.C - 1 and "body_damage" in r["flags"]
    assert reason["text"] == ("Défauts esthétiques sur 4 éléments de carrosserie "
                              "(aile arrière gauche, coffre, pare-choc arrière…)")
    assert reason["delta"] == -0.3 and reason["evidence"] == {"src": "listing", "ref": "damages"}


def test_many_marks_are_capped(settings):
    clean, _ = score(facts(), settings, V=80)
    part, r = score(facts(damages=LIKE_ARKANA), settings, V=80)
    assert part.C == clean.C - 6  # 10 points, capped at 6: at most 2.1 points of the final score
    assert damage_reason(part)["text"] == (
        "Défauts esthétiques sur 8 éléments de carrosserie, dont 1 avec débosselage ou remplacement "
        "(porte arrière gauche, aile avant gauche, porte avant gauche…)")
    assert not any(g["code"] == "body_damage" for g in r["gates"])  # never a gate


@pytest.mark.parametrize("damages", [None, [], dmg(("Jante avant gauche", "Rayure"), ("Enjoliveur", "Cassé"),
                                                   ("Pneu arrière droit", "Usé"), ("Roue de secours", "Absente"),
                                                   ("Jante arrière droite", "Peinture")),
                                     dmg(("Capot", "Peinture"), ("Toit", "Peinture"), ("Hayon", "Rayure"))])
def test_wheels_only_or_few_marks_cost_nothing(settings, damages):
    clean, _ = score(facts(), settings, V=80)
    part, r = score(facts(damages=damages), settings, V=80)
    assert part.C == clean.C and damage_reason(part) is None and "body_damage" not in r["flags"]


def test_a_dent_counts_double(settings):
    part, _ = score(facts(damages=dmg(("Capot", "Peinture"), ("Porte avant droite", "Débosselage"),
                                      ("Aile avant droite", "Remplacement"))), settings, V=80)
    clean, _ = score(facts(), settings, V=80)
    assert part.C == clean.C - 2  # 1 + 2 + 2 = 5 points, 3 free
    assert damage_reason(part)["text"] == (
        "Défauts esthétiques sur 3 éléments de carrosserie, dont 2 avec débosselage ou remplacement "
        "(porte avant droite, aile avant droite, capot)")


def test_damage_summary_counts_pairs_in_first_seen_order():
    assert damage_summary(LIKE_3008)[:4] == [
        {"zone": "Jante avant gauche", "kind": "Peinture", "n": 1},
        {"zone": "Jante arrière gauche", "kind": "Peinture", "n": 1},
        {"zone": "Aile arrière gauche", "kind": "Peinture", "n": 1},
        {"zone": "Coffre", "kind": "Peinture", "n": 2},
    ]
    assert damage_summary(None) == [] and damage_summary([{"zone": 1}, "x"]) == []


def test_no_reason_mentions_photos(settings):
    part, r = score(facts(damages=LIKE_ARKANA, warranty="Garantie", comments="Rayure capot"), settings, V=80)
    assert not any("photo" in x["text"].lower() for x in r["reasons"])


# ------------------------------------------------------------------ warranty wording (h1-2026.11)

@pytest.mark.parametrize("stored,text", [
    ("Garantie", "Garantie mécanique incluse (formule non précisée)"),
    (" GARANTIE ", "Garantie mécanique incluse (formule non précisée)"),
    ("Garantie Sérénité de 3 mois incluse", "Garantie Sérénité de 3 mois incluse"),
    ("Garantie Essentielle", "Garantie Essentielle"),
    ("3 mois pièces", "Garantie : 3 mois pièces"),
])
def test_warranty_reason_text(settings, stored, text):
    assert _warranty_text(stored) == text
    part, _ = score(facts(warranty=stored), settings, V=80)
    reason = next(x for x in part.reasons if x["code"] == "warranty")
    assert reason["text"] == text and "Garantie : Garantie" not in reason["text"]


def test_heuristic_version_is_bumped():
    assert HEURISTIC_VERSION == "h1-2026.11b"


# ------------------------------------------------------------------ comments: negation within one clause

def comment_result(settings, text):
    part, r = score(facts(comments=text), settings, V=100)
    return ({g["code"] for g in r["gates"]}, {x["code"] for x in part.reasons if x["code"].startswith("comment_")})


@pytest.mark.parametrize("text,gates,minor", [
    ("Km non garanti, moteur HS", {"comment_dealbreaker", "mileage_not_guaranteed"}, set()),
    ("Sans carnet, moteur HS", {"comment_dealbreaker"}, set()),
    ("Sans carnet moteur HS", {"comment_dealbreaker"}, set()),
    ("Pas de fumée", set(), set()),
    ("Pas de voyant, bruit train avant", set(), {"comment_bruit"}),
    ("Pas de voyant mais bruit train avant", set(), {"comment_bruit"}),
    ("Pare-chocs rayé", set(), set()),
    ("euro 6", set(), set()),
    ("Véhicule non VGE", set(), set()),
    ("Pas de moteur HS", set(), set()),
    ("Non roulant", {"comment_dealbreaker"}, set()),
    # A negation that belongs to an earlier phrase never cancels "kilométrage non garanti".
    ("Vendu sans garantie kilométrage non garanti", {"mileage_not_guaranteed"}, set()),
    ("Sans carnet kilometrage non garanti", {"mileage_not_guaranteed"}, set()),
    ("Km non garanti", {"mileage_not_guaranteed"}, set()),
])
def test_comment_negation_stays_in_its_clause(settings, text, gates, minor):
    assert comment_result(settings, text) == (gates, minor)


def test_leased_battery_comment_still_detected(settings):
    part, _ = score(facts(**EV, battery_soh_pct=70.0, comments="Batterie en location"), settings, V=100)
    assert "battery_leased" in part.flags


# ------------------------------------------------------------------ site redesign (snapshot v2)

from auction_agent.adapters.alcopa import AlcopaAdapter  # noqa: E402
from auction_agent.scoring.heuristics import SYSTEM_FR  # noqa: E402

_ALCOPA = AlcopaAdapter(None)


def _parts_sum(parts):
    return round(parts["start"] + parts["vat"] + parts["fees"] + parts["repairs"] + parts["ct_redo"], 2)


@pytest.mark.parametrize("kw", [
    dict(ct=FAV, sale_kind="room"),                                   # fees, and a CT to redo (> 6 months)
    dict(ct=MAJ, sale_kind="room", vat_regime="ht_recuperable"),      # repairs and VAT
    dict(ct=CRIT, sale_kind="online", price=7_777.0),                 # critical repairs, no fees
    dict(ct=FAV, sale_kind="room", sale_date=date(2026, 3, 20)),      # fresh CT: no redo
])
def test_price_parts_sum_to_effective_price(settings, kw):
    part = assess(facts(**kw), settings, date(2026, 3, 15) if "sale_date" in kw else TODAY,
                  _ALCOPA.fee_model, fee_split=_ALCOPA.fee_split)
    parts = part.price_parts
    assert parts["total"] == part.effective_price == _parts_sum(parts)
    assert parts["start"] == kw.get("price", 12_000.0)
    assert (parts["vat"] > 0) == (kw.get("vat_regime") == "ht_recuperable")
    assert (parts["fees"] > 0) == (kw["sale_kind"] == "room")
    assert (parts["repairs"] > 0) == (kw["ct"] is not FAV)
    assert parts["ct_redo"] == (80.0 if "ct_redo" in part.flags else 0.0)
    r = finalize(part, 50.0, "value_unknown:few_comparables", None, 0, settings)
    assert r["price_parts"] == parts


def test_price_parts_without_a_split_and_without_a_price(settings):
    part = assess(facts(), settings, TODAY, lambda price, *_a: price + 500)
    assert part.price_parts["vat"] == 0.0 and part.price_parts["fees"] == 500.0
    assert part.price_parts["total"] == _parts_sum(part.price_parts) == part.effective_price
    assert assess(facts(price=None), settings, TODAY, fee).price_parts is None


def test_major_defect_gate_names_the_system_in_french(settings):
    _, r = score(facts(ct=MAJ, mileage_km=151_220), settings, V=100)
    gate = next(g for g in r["gates"] if g["code"] == "ct_major_safety")
    assert gate["text"].startswith("Défaillance majeure : freinage (")
    assert "sur freinage" not in gate["text"]
    assert set(SYSTEM_FR) >= {"identification", "freinage", "direction", "visibilite", "eclairage", "liaisons_sol",
                              "structure", "equipements", "nuisances", "supplementaires", "vehicule_ecole", "autre"}
    assert SYSTEM_FR["structure"] == "Châssis et carrosserie"


def test_km_per_year_reason_has_one_space(settings):
    part = assess(facts(mileage_km=250_000), settings, TODAY, fee)
    text = next(x["text"] for x in part.reasons if x["code"] == "km_per_year")
    assert "  " not in text and text.endswith(" km/an, au-dessus de la normale")


@pytest.mark.parametrize("n, kind, expected", [
    (0, "mineure", "sans défaillance"),
    (1, "majeure", "1 défaillance majeure"),
    (3, "critique", "3 défaillances critiques"),
])
def test_defect_counts_use_real_french_plurals(n, kind, expected):
    from auction_agent.scoring.heuristics import _defect_count

    assert _defect_count(n, kind) == expected


@pytest.mark.parametrize("ct", [FAV, MAJ, CRIT])
def test_ct_reason_texts_have_no_bracketed_plurals(ct, settings):
    reasons = assess(facts(ct=ct), settings, TODAY, fee).reasons
    texts = [r["text"] for r in reasons if r["evidence"]["src"] == "ct"]
    assert texts and not any("(s)" in t for t in texts)
