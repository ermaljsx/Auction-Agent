"""ct-2.1 against the layouts of the real Alcopa PVs.

The fixtures under tests/fixtures/ct/alcopa_*.txt are synthetic: every name, address,
number, date and reading is invented, only the layout of each real form is reproduced
(two text columns merged line by line, codes printed with a trailing dot, headers and
codes prefixed by the other column, two-page PVs, voluntary forms). Every test here
fails on ct-2.0, which found no defect, no date and no odometer reading on real PVs.
"""
import pytest

from auction_agent.ct_parser import (
    RESULT_CRITICAL,
    RESULT_FAVORABLE,
    RESULT_MAJOR,
    RESULT_UNKNOWN,
    parse_controle_technique,
)
from auction_agent.privacy import redact
from tests.conftest import read_fixture

ALCOPA = [
    "alcopa_a_majeure.txt", "alcopa_a_favorable.txt", "alcopa_a_critique_2pages.txt", "alcopa_a_volontaire.txt",
    "alcopa_b_critique.txt", "alcopa_b_2pages.txt", "alcopa_b_rollback.txt", "alcopa_cv_volontaire.txt",
]
# Invented centre, controller, holder and vehicle data that must never reach a defect label.
LEFT_COLUMN = ["SARL", "BANC D'ESSAI", "VILLE-TEST", "BOURG-TEST", "S099", "099X", "099Y", "RUE DES ESSAIS",
               "IMPASSE", "0199000000", "Tel :", "GARAGE CONTROLE", "RENAULT", "PEUGEOT", "AB-123-CD", "VF1TEST",
               "DUPONT", "ALLEE DES TESTS", "HAMEAU", "Kilométrages", "Afin de", "Certificat", "SIGNATURE",
               "IDENTIFICATION", "Contre-visite", "NATURE DU PROCHAIN"]


def parse(name):
    return parse_controle_technique(read_fixture("ct", name))


def codes(out, severity=None):
    return [d["code"] for d in out["defects"] if severity is None or d["severity"] == severity]


def assert_clean_labels(out):
    for d in out["defects"]:
        assert d["label"], d["code"]
        for token in LEFT_COLUMN:
            assert token.lower() not in d["label"].lower(), (d["code"], token)


def test_layout_a_codes_after_merged_column_text_and_fields():
    out = parse("alcopa_a_majeure.txt")
    assert out["layout"] == "A"
    assert (out["result"], out["result_source"]) == (RESULT_MAJOR, "field")
    # The first code follows "(8) LIMITE DE VALIDITÉ ...", others follow the validity date,
    # "NATURE DU PROCHAIN CONTRÔLE", "SIGNATURE :", "Date de 1ère mise" or the centre address.
    assert codes(out, "major") == ["1.1.13.a.2", "2.1.3.b.2", "4.1.2.a.2", "8.2.22.c.2"]
    # The minor header is prefixed by the centre agreement number.
    assert codes(out, "minor") == ["4.5.2.a.1", "5.2.3.e.1", "6.2.1.a.1", "6.2.10.a.1"]
    assert out["counts"] == {"minor": 4, "major": 4, "critical": 0}
    assert [d["system"] for d in out["defects"][:2]] == ["freinage", "direction"]
    assert out["suffix_disagreements"] == 0 and out["unsectioned_codes"] == 0
    assert out["nature"] == "periodique"
    assert out["inspection_date"] == "2026-09-14"
    assert out["inspection_km"] == 112480
    assert out["validity_date"] == "2026-11-13" and out["next_control"] == "contre_visite"
    assert out["parse_confidence"] >= 0.8
    obd = next(d for d in out["defects"] if d["code"] == "8.2.22.c.2")
    assert obd["obd_codes"] == ["P0401", "P20A1"]
    # Wrapped labels are joined without the other column.
    brake_light = next(d for d in out["defects"] if d["code"] == "4.1.2.a.2")
    assert brake_light["label"].endswith("dans les limites prescrites par les exigences AVG")
    assert next(d for d in out["defects"] if d["code"] == "5.2.3.e.1")["label"].endswith("corps étranger ARD")
    assert_clean_labels(out)
    # The odometer pair split over two lines ("01/09/2026 :" / "... 112300 km") is kept.
    assert out["odometer"][-1] == {"date": "2026-09-01", "km": 112300}
    assert len(out["odometer"]) == 3 and out["odometer_inconsistent"] is False


def test_layout_a_favourable_with_the_unfavourable_slip_is_read():
    out = parse("alcopa_a_favorable.txt")
    assert (out["result"], out["result_source"]) == (RESULT_FAVORABLE, "field")
    assert out["counts"] == {"minor": 3, "major": 0, "critical": 0}
    assert out["parse_confidence"] >= 0.8
    assert out["validity_date"] == "2028-10-04" and out["next_control"] == "periodique"
    assert out["inspection_km"] == 41250 and out["odometer_history"] == "absent"
    assert_clean_labels(out)


def test_layout_a_critical_two_pages_keeps_every_severity():
    out = parse("alcopa_a_critique_2pages.txt")
    assert out["result"] == RESULT_CRITICAL and out["parse_confidence"] >= 0.8
    # The critical code sits on the "(8) LIMITE DE VALIDITÉ" line, and the major header is
    # prefixed by the validity date: majors must not inherit "critical".
    assert codes(out, "critical") == ["1.1.16.c.3"]
    assert codes(out, "major") == ["2.1.5.a.2", "5.3.1.a.2", "6.1.1.a.2"]
    assert codes(out, "minor") == ["4.5.2.a.1"]
    assert out["defects"][0]["system"] == "freinage"
    assert out["inspection_date"] == out["validity_date"] == "2026-10-21"
    assert out["inspection_km"] == 203400
    assert out["odometer_history"] == "coherence_non_realisee"
    assert_clean_labels(out)


def test_layout_b_result_merged_with_the_critical_header():
    out = parse("alcopa_b_critique.txt")
    assert out["layout"] == "B"
    assert (out["result"], out["result_source"]) == (RESULT_CRITICAL, "position")
    assert codes(out, "critical") == ["5.2.3.d.3"]  # its header shares the result line
    assert codes(out, "major") == ["1.2.1.b.2", "5.3.3.a.2"]
    # "Contre-visite DEFAILLANCES MINEURES :" opens the minor section (ct-2.0 ended it).
    assert codes(out, "minor") == ["4.5.2.a.1", "6.2.10.a.1"]
    assert out["parse_confidence"] >= 0.8
    assert out["inspection_date"] == "2026-10-12" and out["inspection_km"] == 190112
    assert out["validity_date"] == "2026-10-12"
    assert next(d for d in out["defects"] if d["code"] == "5.3.3.a.2")["label"].endswith(
        "SUSPENSION : Mauvaise attache d'un composant au châssis ou à l'essieu (AVD)")
    assert_clean_labels(out)
    assert out["odometer"] == [
        {"date": "2020-03-14", "km": 98500}, {"date": "2022-03-20", "km": 131200},
        {"date": "2024-03-18", "km": 162900},  # "18/03/2024 :" / "162900km /" on two lines
        {"date": "2026-09-19", "km": 190050},
    ]


def test_layout_b_two_pages_continue_the_section_and_the_label():
    out = parse("alcopa_b_2pages.txt")
    assert out["result"] == RESULT_MAJOR and out["parse_confidence"] >= 0.8
    assert codes(out, "major") == ["4.1.1.b.2", "8.4.1.a.2"]
    # Page 2 has no header: its code stays in the minor section.
    assert codes(out, "minor") == ["4.5.2.a.1", "5.2.3.h.1", "6.2.1.a.1"]
    tyre = next(d for d in out["defects"] if d["code"] == "5.2.3.h.1")
    assert tyre["label"].endswith("manifestement sous-gonflé (ARG)")
    assert out["inspection_km"] == 154020 and out["validity_date"] == "2027-01-02"
    assert len(out["odometer"]) == 3
    assert_clean_labels(out)


def test_layout_b_odometer_rollback_against_the_current_reading():
    out = parse("alcopa_b_rollback.txt")
    assert out["inspection_km"] == 128500 and out["odometer"][-1]["km"] == 141000
    assert out["odometer_inconsistent"] is True
    # Even without the CT's own odometer defect, the current reading below the history is caught.
    text = read_fixture("ct", "alcopa_b_rollback.txt")
    lines = [line for line in text.splitlines() if "7.11.1" not in line and "celui relev" not in line]
    out2 = parse_controle_technique("\n".join(lines))
    assert out2["counts"]["minor"] == 0 and out2["odometer_inconsistent"] is True


def test_voluntary_form_a_headers_with_legend():
    out = parse("alcopa_a_volontaire.txt")
    assert out["layout"] == "A-vol" and out["nature"] == "volontaire"
    assert codes(out, "major") == ["5.2.3.d.2"] and codes(out, "minor") == ["3.4.1.b.1"]
    assert (out["result"], out["result_source"]) == (RESULT_MAJOR, "defects")  # no result printed
    assert out["parse_confidence"] < 0.8  # scoring keeps voluntary PVs as "not a regulatory CT"
    assert out["inspection_date"] == "2026-10-08" and out["inspection_km"] == 87650
    assert_clean_labels(out)


def test_voluntary_cv_form_numbered_entries_and_holder_block():
    out = parse("alcopa_cv_volontaire.txt")
    assert out["layout"] == "C" and out["nature"] == "volontaire"
    # "1 - 2.1.3.b.2." entries, the ordinal is not the function; 2-line legend headers.
    assert codes(out, "major") == ["2.1.3.b.2", "4.1.2.a.2"]
    assert codes(out, "minor") == ["6.2.10.a.1"]
    assert out["defects"][0]["system"] == "direction"
    assert out["inspection_date"] == "2026-10-15" and out["inspection_km"] == 64210
    assert out["odometer_history"] == "non_renseigne"
    assert out["parse_confidence"] < 0.8
    assert_clean_labels(out)  # the holder's name and address follow the defects


def test_driving_school_function_12_is_not_a_direction_defect():
    text = ("ZA99000801\nCT Véhicule école 02/10/2026 26990701\n"
            "Défavorable pour défaillances majeures\nDEFAILLANCES MAJEURES :\n"
            "12.2.2.b.2. RÉTROVISEUR EXTÉRIEUR COMPLÉMENTAIRE : Absent\n"
            "Contre-visite 2.1.3.b.2. ÉTAT DE LA TIMONERIE DE DIRECTION : Usure excessive\n")
    out = parse_controle_technique(text)
    assert out["nature"] == "vehicule_ecole"
    assert [(d["code"], d["system"]) for d in out["defects"]] == [
        ("12.2.2.b.2", "vehicule_ecole"), ("2.1.3.b.2", "direction")]
    assert out["result"] == RESULT_MAJOR and out["parse_confidence"] >= 0.8


def test_nature_comes_from_the_nature_date_line_not_from_the_next_control():
    # Every unfavourable periodic PV prints "Contre-visite" (the NEXT control) as a prefix.
    assert all(parse(name)["nature"] == "periodique"
               for name in ALCOPA if "volontaire" not in name)
    # A real contre-visite PV (no such PV in the corpus yet: layout assumed to be form B).
    text = ("ZA99000901\nContre-visite 18/11/2026 26991001\nFavorable\n"
            "Contrôle technique périodique Afin de faciliter les campagnes de rappel « graves »\n"
            "AVANT ARRIERE\n133250 G D G D\nCT : 02/09/2028 Feux de croisement (-2.5% à -0.5%) -1.0% -1.2%\n")
    out = parse_controle_technique(text)
    assert out["nature"] == "contre_visite" and out["inspection_date"] == "2026-11-18"
    assert out["result"] == RESULT_FAVORABLE and out["inspection_km"] == 133250
    # The validity of a contre-visite keeps the original period: it is not read as a result.
    assert out["result_conflict"] is False and out["parse_confidence"] >= 0.8


@pytest.mark.parametrize("name", ALCOPA)
def test_redaction_keeps_every_value_the_parser_needs(name):
    """documents.text is stored redacted and re-parsed when the parser changes."""
    text = read_fixture("ct", name)
    original, stored = parse_controle_technique(text), parse_controle_technique(redact(text))
    assert [(d["code"], d["severity"]) for d in original["defects"]] == \
           [(d["code"], d["severity"]) for d in stored["defects"]]
    keys = ("result", "result_source", "nature", "inspection_date", "inspection_km", "validity_date",
            "next_control", "odometer", "odometer_inconsistent", "counts", "parse_confidence", "layout")
    assert {k: original[k] for k in keys} == {k: stored[k] for k in keys}
    assert original["defects"], name  # every fixture lists defects


def test_holder_block_of_the_cv_form_is_redacted():
    out = redact(read_fixture("ct", "alcopa_cv_volontaire.txt"))
    for secret in ("DUPONT-ESSAI CAMILLE", "8 ALLEE DES TESTS", "99992 HAMEAU-FICTIF", "VF7TESTAB00000002", "AB-123-CD"):
        assert secret not in out
    assert out.count("[DONNEES PERSONNELLES]") == 3
    assert "64210 C3" in out and "0 - IDENTIFICATION DU VÉHICULE" in out
    # Other layouts are left alone by that rule.
    a = read_fixture("ct", "alcopa_a_favorable.txt")
    assert redact(a) == a


# ------------------------------------------------------------------ safety properties

def test_a_suffix_3_code_is_critical_whatever_its_section():
    text = ("Contrôle technique périodique 02/10/2026 26990901\nDéfavorable pour défaillances majeures\n"
            "DEFAILLANCES MAJEURES :\n1.1.13.a.2. PLAQUETTES : Usure excessive (AV)\n"
            "DEFAILLANCES MINEURES :\n5.2.3.d.3. PNEUMATIQUES : Corde visible (AVG)\n")
    out = parse_controle_technique(text)
    tyre = next(d for d in out["defects"] if d["code"] == "5.2.3.d.3")
    assert tyre["severity"] == "critical"
    assert out["result"] == RESULT_CRITICAL  # never below a critical defect
    assert out["result_conflict"] is True and out["parse_confidence"] < 0.8


def test_a_code_before_any_header_is_kept_with_its_own_severity():
    text = ("Contrôle technique périodique 02/10/2026 26990902\nDéfavorable pour défaillances critiques\n"
            "6.2.1.a.3. CARROSSERIE : Élément susceptible de provoquer des blessures (AR)\n"
            "DEFAILLANCES MAJEURES :\n2.1.3.b.2. DIRECTION : Usure excessive (G)\n")
    out = parse_controle_technique(text)
    assert codes(out, "critical") == ["6.2.1.a.3"] and out["result"] == RESULT_CRITICAL
    assert out["unsectioned_codes"] == 1 and out["parse_confidence"] < 0.8


def test_same_day_validity_on_a_known_form_is_critical_without_a_result_line():
    text = read_fixture("ct", "alcopa_b_critique.txt").replace(
        "Défavorable pour défaillances critiques DEFAILLANCES CRITIQUES :", "DEFAILLANCES MAJEURES :")
    text = text.replace("5.2.3.d.3.", "5.2.3.d.2.")
    out = parse_controle_technique(text)
    assert out["counts"]["critical"] == 0
    assert (out["result"], out["result_source"]) == (RESULT_CRITICAL, "validity")
    assert out["parse_confidence"] < 0.8


def test_boilerplate_legends_and_measurements_never_make_results_or_defects():
    text = "\n".join([
        "ZA99000999",
        "Contrôle technique périodique 02/10/2026 26990999",
        "(7) RÉSULTAT DU CONTRÔLE (6) DÉFAILLANCES ET NIVEAUX DE GRAVITÉ",
        "FONCTIONS CONTRÔLÉES DÉFAILLANCES CONSTATÉES",
        "INFORMATION SUR LE CONTRÔLE TECHNIQUE DÉFAVORABLE",
        "Code(s) défaillance(s) standard(s) relevé(s) concernant le",
        "Le témoin fait état d'une défaillance du système",
        "Défaillances majeures : contre-visite sous deux mois",
        "Opacité des fumées C1: 03.02 - C2: 01.13 Lambda 1.005",
        "12 RUE DU 8.2.2 AVENUE  Ripage (-8 à +8 m/km) : +1.0 m/km",
        "Taux d'efficacité global (≥58 %) : 61 %  Forces verticales : 900 daN",
        "Contre-visite 10/02/2024 : 61000km / 12/02/2026 : 88000km",
    ])
    out = parse_controle_technique(text)
    assert out["defects"] == [] and out["result"] == RESULT_UNKNOWN
    assert out["parse_confidence"] < 0.8
    assert out["nature"] == "periodique" and out["inspection_date"] == "2026-10-02"
