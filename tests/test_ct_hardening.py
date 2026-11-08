"""Regression tests for the review of ct-2.1 (safety gates, labels, odometer, OBD, CV redaction).

Every fixture and inline text is synthetic: invented centre, controller, holder, plate,
VIN, numbers and dates. Only the layouts of the real Alcopa PVs are reproduced.
"""
import time

import pytest

from auction_agent import ct_parser
from auction_agent.ct_parser import (
    RESULT_CRITICAL,
    RESULT_FAVORABLE,
    RESULT_MAJOR,
    RESULT_UNKNOWN,
    parse_controle_technique,
)
from auction_agent.privacy import redact
from tests.conftest import read_fixture

# Invented centre, controller and vehicle data that must never reach a defect label.
LEFT_COLUMN = ["AUTOBILAN", "FICTIF", "SARL", "BANC D'ESSAI", "VILLE-TEST", "99990", "RUE DES ESSAIS",
               "PARC IMAGINAIRE", "ZA ", "Tel", "0199000000", "S099", "099X", "099Y", "RENAULT", "CLIO", "MEGANE",
               "QUADRI", "L6e", "visé", "Emissions à", "huissier", "IDENTIFICATION DU CENTRE", "RAISON SOCIALE",
               "COORDONN", "ÉQUIPEMENTS ÉLECTRIQUES, ESSIEUX", "ACCESSOIRES DU CHÂSSIS", "ROUES, PNEUS"]


def parse(name):
    return parse_controle_technique(read_fixture("ct", name))


def labels(out):
    return {d["code"]: d["label"] for d in out["defects"]}


def assert_clean(out):
    for d in out["defects"]:
        for token in LEFT_COLUMN:
            assert token.lower() not in d["label"].lower(), (d["code"], token)


# ------------------------------------------------------------------ codes and red gates

def test_l_category_codes_with_suffixes_6_and_7_are_read_in_full():
    out = parse("alcopa_b_l_category.txt")
    assert [(d["code"], d["severity"]) for d in out["defects"]] == [
        ("1.1.12.a.7", "major"), ("4.1.1.a.7", "major"),
        ("6.1.3.a.7", "major"),  # printed after "Contre-visite": ct-2.1 dropped it
        ("5.3.4.a.6", "minor"), ("6.2.1.a.6", "minor"),
    ]
    assert out["defects"][2]["system"] == "structure"
    assert out["result"] == RESULT_MAJOR
    # A code scheme no real PV has been checked against: the reading is flagged uncertain.
    assert "unknown_code_scheme" in out["warnings"] and out["parse_confidence"] < 0.8
    # No brake bench: the km is printed in front of the first measurement line.
    assert out["inspection_km"] == 18340
    assert labels(out)["6.2.1.a.6"] == "CABINE, CARROSSERIE OU CARENAGE : Panneau ou élément endommagé ou mal fixé"
    assert_clean(out)


@pytest.mark.parametrize("shape", [
    "1 - 2.1.3.b.3.ÉTAT DE LA TIMONERIE",       # glued to the label
    "1 - 2.1.3.b .3. ÉTAT DE LA TIMONERIE",     # stray space inside the code
    "1 - 2​.1.3.b.3. ÉTAT DE LA TIMONERIE",  # zero-width space
    "1 - 2,1,3,b,3. ÉTAT DE LA TIMONERIE",      # commas
])
def test_a_critical_code_on_a_voluntary_form_survives_odd_extraction(shape):
    # The CV form prints no result: the code is the only critical signal.
    text = read_fixture("ct", "alcopa_cv_volontaire.txt").replace("1 - 2.1.3.b.2. ÉTAT DE LA TIMONERIE", shape)
    out = parse_controle_technique(text)
    assert ("2.1.3.b.3", "critical") in [(d["code"], d["severity"]) for d in out["defects"]]
    assert out["result"] == RESULT_CRITICAL and out["parse_confidence"] < 0.8


def test_a_code_glued_to_left_column_text_is_read():
    text = read_fixture("ct", "alcopa_a_critique_2pages.txt")
    glued = text.replace("RÉALISÉ 1.1.16.c.3.", "RÉALISÉ1.1.16.c.3.")
    assert glued != text
    out = parse_controle_technique(glued)
    assert [d["code"] for d in out["defects"] if d["severity"] == "critical"] == ["1.1.16.c.3"]
    assert out["result"] == RESULT_CRITICAL


GENERIC = ("PROCÈS-VERBAL DE CONTRÔLE TECHNIQUE\nCentre de contrôle fictif\n"
           "Nature du contrôle : Contrôle technique périodique\nDate du contrôle : 12/10/2026\n"
           "Kilométrage relevé : 120 000 km\n")


@pytest.mark.parametrize("result_lines", [
    "Défavorable pour\ndéfaillances critiques",
    "Défavorable pour défaillances\ncritiques",
    "Défavorable\npour défaillances critiques",
    "Résultat du contrôle :\nDéfavorable pour défaillances\ncritiques",
])
def test_a_wrapped_critical_result_phrase_is_still_critical(result_lines):
    out = parse_controle_technique(GENERIC + result_lines + "\n")
    assert out["result"] == RESULT_CRITICAL  # ct-2.0 read these; ct-2.1 gave UNKNOWN


@pytest.mark.parametrize(("value", "expected"), [
    ("Défavorable (défaillances critiques)", RESULT_CRITICAL),
    ("Défavorable - défaillances critiques", RESULT_CRITICAL),
    ("Défavorable : défaillances critiques", RESULT_CRITICAL),
    ("DÉFAVORABLE CRITIQUE", RESULT_CRITICAL),
    ("Défavorable avec défaillance(s) critique(s)", RESULT_CRITICAL),
    ("Défavorable (défaillances majeures)", RESULT_MAJOR),
])
def test_a_result_value_on_the_line_after_its_label_is_read_like_ct20(value, expected):
    out = parse_controle_technique(GENERIC + "Résultat du contrôle :\n" + value + "\n")
    assert (out["result"], out["result_source"]) == (expected, "field")


def test_result_prose_is_not_a_field_and_a_critical_phrase_wins():
    text = GENERIC + ("Mesures : résultat favorable du test d'opacité\n"
                      "Défavorable pour défaillances critiques\n")
    out = parse_controle_technique(text)
    assert out["result"] == RESULT_CRITICAL
    # A real field that contradicts a critical phrase: the red result wins, flagged.
    text = GENERIC + "Résultat : Favorable\nDéfavorable pour défaillances critiques\n"
    out = parse_controle_technique(text)
    assert out["result"] == RESULT_CRITICAL and out["result_conflict"] is True
    assert out["parse_confidence"] < 0.8


def test_a_severity_legend_does_not_turn_a_favourable_pv_unfavourable():
    text = read_fixture("ct", "alcopa_a_favorable.txt")
    legend = text.replace("Ripage", "Défavorable pour défaillances majeures : contre-visite dans les 2 mois\nRipage", 1)
    assert legend != text
    out = parse_controle_technique(legend)
    assert out["result"] == RESULT_FAVORABLE


@pytest.mark.parametrize("name", ["alcopa_b_2pages.txt", "alcopa_a_critique_2pages.txt"])
def test_a_missing_second_page_lowers_confidence(name):
    text = read_fixture("ct", name)
    out = parse_controle_technique(text)
    assert "pages_missing" not in out["warnings"]
    page1 = text.split("\n\n")[0]
    cut = parse_controle_technique(page1)
    assert "pages_missing" in cut["warnings"] and cut["parse_confidence"] < 0.8
    assert cut["result"] == out["result"]  # the result itself is kept


def test_long_whitespace_runs_stay_fast():
    text = ("Contrôle technique périodique 02/10/2026 26990901\n"
            "Défaillances majeures" + " " * 5000 + "x\n" + "Défaillances majeures :" + " :" * 3000 + "\n"
            "Résultat" + " " * 5000 + "\n" + "01/01/2020 : " + "1 " * 3000 + "\n")
    start = time.perf_counter()
    parse_controle_technique(text)
    assert time.perf_counter() - start < 2.0  # several seconds per line in ct-2.1


def test_an_internal_error_returns_a_flagged_empty_summary(monkeypatch):
    def boom(summary, text):
        raise IndexError("simulated parser bug")
    monkeypatch.setattr(ct_parser, "_parse_into", boom)
    out = parse_controle_technique(read_fixture("ct", "alcopa_b_critique.txt"))
    assert out["result"] == RESULT_UNKNOWN and out["warnings"] == ["parse_error:IndexError"]


# ------------------------------------------------------------------ labels

def test_b_labels_skip_the_centre_block_merged_before_wrapped_headings():
    out = parse("alcopa_b_centre_wraps.txt")
    assert out["result"] == RESULT_MAJOR and out["parse_confidence"] >= 0.8
    got = labels(out)
    assert got["4.7.1.b.1"] == ("ÉTAT ET FONCTIONNEMENT (DISPOSITIF D'ÉCLAIRAGE DE LA PLAQUE D'IMMATRICULATION "
                                "ARRIÈRE) : Source lumineuse partiellement défectueuse (AR)")
    assert got["5.3.3.a.1"] == ("TUBES DE POUSSÉE, JAMBES DE FORCE, TRIANGLES ET BRAS DE SUSPENSION : Détérioration "
                                "d'un silentbloc de liaison au châssis ou à l'essieu (AVG, AVD)")
    # An upper-case-only heading line is kept when it continues the heading.
    assert got["4.2.1.a.1"] == ("ÉTAT ET FONCTIONNEMENT (FEUX DE POSITION AVANT, ARRIÈRE ET LATÉRAUX, FEUX DE "
                                "GABARIT, FEUX D'ENCOMBREMENT ET FEUX DE JOUR) : Source lumineuse partiellement "
                                "défectueuse (AVG)")
    # A location printed after another column's text, and the catalogue token "OBD".
    assert got["5.3.4.b.1"] == "ROTULES DE SUSPENSION : Capuchon antipoussière détérioré (AVD, AVG)"
    assert got["8.2.12.e.1"].endswith("dysfonctionnement du témoin OBD")
    assert_clean(out)


def test_a_labels_skip_the_centre_name_and_address_merged_before_wrapped_headings():
    out = parse("alcopa_a_centre_wraps.txt")
    assert out["counts"] == {"minor": 1, "major": 6, "critical": 0} and out["parse_confidence"] >= 0.8
    got = labels(out)
    assert got["4.4.3.a.2"].startswith("CONFORMITÉ AVEC LES EXIGENCES (INDICATEURS DE DIRECTION ET FEUX DE "
                                       "SIGNAL DE DÉTRESSE) : Feu, couleur émise")
    assert got["4.8.1.b.2"].startswith("ÉTAT (CATADIOPTRES, MARQUAGE DE VISIBILITÉ RÉFLÉCHISSANT ET PLAQUES "
                                       "RÉFLÉCHISSANTES ARRIÈRE) : Catadioptre")
    assert got["6.1.4.a.2"].startswith("PARE-CHOCS, PROTECTION LATÉRALE ET DISPOSITIFS ANTI-ENCASTREMENT "
                                       "ARRIÈRE : Mauvaise fixation")
    assert got["5.2.3.e.1"].endswith("corps étranger ARD, ARG")  # location after the make and model
    assert_clean(out)


def test_voluntary_form_function_list_never_reaches_labels():
    out = parse("alcopa_a_volontaire_wraps.txt")
    got = labels(out)
    assert got["4.4.1.b.2"] == ("ÉTAT ET FONCTIONNEMENT (INDICATEURS DE DIRECTION ET FEUX DE SIGNAL DE DÉTRESSE) : "
                                "Glace fortement défectueuse (lumière émise affectée) D")
    assert got["6.1.4.a.2"].endswith("blessures en cas de contact AVG")
    assert got["5.2.3.d.2"].endswith("entaillé AVD")
    assert_clean(out)
    # A heading outside the list of known wrapped headings, behind the function list.
    text = read_fixture("ct", "alcopa_a_volontaire_wraps.txt").replace(
        "6.1.4.a.2. PARE-CHOCS, PROTECTION LATÉRALE ET DISPOSITIFS ANTI-ENCASTREMENT\n"
        "ACCESSOIRES DU CHÂSSIS, AUTRE MATÉRIEL ARRIÈRE : Mauvaise fixation ou endommagement susceptible de causer des\n"
        "blessures en cas de contact AVG",
        "3.2.1.c.2. ÉTAT DES\nACCESSOIRES DU CHÂSSIS, AUTRE MATÉRIEL VITRAGES : Vitrage dans un état inacceptable AVG")
    assert labels(parse_controle_technique(text))["3.2.1.c.2"] == \
        "ÉTAT DES VITRAGES : Vitrage dans un état inacceptable AVG"


# ------------------------------------------------------------------ odometer and OBD

def test_history_skips_the_registration_line_and_continues_on_page_2():
    out = parse("alcopa_b_centre_wraps.txt")
    assert out["odometer"] == [
        {"date": "2019-07-03", "km": 52100}, {"date": "2021-07-17", "km": 61500},
        {"date": "2023-06-14", "km": 70250},  # not "05/05/2009 70250km" from the vehicle line
        {"date": "2025-06-04", "km": 80100},
        {"date": "2026-02-24", "km": 84200},  # "24/02/2026 :" / "84200km" on page 2
    ]
    assert out["inspection_km"] == 88800 and out["odometer_inconsistent"] is False


def test_a_rollback_against_the_last_page_2_reading_is_caught():
    text = read_fixture("ct", "alcopa_b_centre_wraps.txt").replace("88800 G D G D", "83900 G D G D")
    assert parse_controle_technique(text)["odometer_inconsistent"] is True


def test_obd_codes_wrapped_onto_the_next_lines_are_kept():
    b = {d["code"]: d.get("obd_codes") for d in parse("alcopa_b_centre_wraps.txt")["defects"]}
    assert b["8.2.12.d.2"] == ["P0401", "P0420", "P2002"]  # behind the centre agreement number
    a = {d["code"]: d.get("obd_codes") for d in parse("alcopa_a_centre_wraps.txt")["defects"]}
    assert a["8.2.22.c.2"] == ["P0401", "P0402", "P0403", "P0404", "P0405"]  # behind a left-column label


@pytest.mark.parametrize("name", ["alcopa_b_centre_wraps.txt", "alcopa_a_centre_wraps.txt", "alcopa_b_l_category.txt",
                                  "alcopa_a_volontaire_wraps.txt"])
def test_new_layouts_parse_the_same_from_the_stored_text(name):
    text = read_fixture("ct", name)
    original, stored = parse_controle_technique(text), parse_controle_technique(redact(text))
    assert original == stored


# ------------------------------------------------------------------ CV form redaction

CV = read_fixture("ct", "alcopa_cv_volontaire.txt")
HOLDER = ("DUPONT-ESSAI", "ALLEE DES TESTS", "HAMEAU-FICTIF")


def test_cv_holder_lines_carrying_a_defect_keep_the_defect():
    # A long defect list runs down beside the holder block.
    text = CV.replace("DUPONT-ESSAI CAMILLE\n",
                      "DUPONT-ESSAI CAMILLE 2 - 1.1.16.c.3. CYLINDRES OU ÉTRIERS DE FREINS : Défaut grave (AVG)\n")
    stored = redact(text)
    assert not any(h in stored for h in HOLDER)
    original, reparsed = parse_controle_technique(text), parse_controle_technique(stored)
    assert [(d["code"], d["severity"]) for d in original["defects"]] == \
           [(d["code"], d["severity"]) for d in reparsed["defects"]]
    assert original["result"] == reparsed["result"] == RESULT_CRITICAL
    assert redact(stored) == stored  # idempotent


@pytest.mark.parametrize("variant", [
    lambda t: t.replace("VF7TESTAB00000002 GO", "VF7TESTAB00000002 GPL"),
    lambda t: t.replace("VF7TESTAB00000002 GO", "VF7TESTAB00000002 ES/GP"),
    lambda t: t.replace("64210 C3\n", ""),
    lambda t: t.replace("64210 C3", "Non relevé C3"),
    lambda t: t.replace("DUPONT-ESSAI CAMILLE\n", "DUPONT-ESSAI CAMILLE\nCHEZ MME FICTIVE\nBATIMENT B\nAPPT 3\n"),
    lambda t: ("X" * 60 + "\n") * 8 + t,
    lambda t: t.replace("\n", "\r\n"),
    lambda t: t.replace("VF7TESTAB00000002 GO", "VF7TESTAB0000 0002 GO ?"),  # vehicle line not recognised
])
def test_cv_holder_block_is_redacted_on_layout_variants(variant):
    text = variant(CV)
    stored = redact(text)
    assert not any(h in stored for h in HOLDER)
    assert "0 - IDENTIFICATION DU VÉHICULE" in stored


def test_cv_km_is_kept_for_the_reparse_with_other_energy_codes():
    text = CV.replace("VF7TESTAB00000002 GO", "VF7TESTAB00000002 GPL")
    assert parse_controle_technique(redact(text))["inspection_km"] == 64210
