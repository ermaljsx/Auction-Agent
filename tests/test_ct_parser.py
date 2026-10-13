import unicodedata

from auction_agent.ct_parser import (
    RESULT_CRITICAL,
    RESULT_FAVORABLE,
    RESULT_MAJOR,
    RESULT_UNKNOWN,
    parse_controle_technique,
)
from tests.conftest import read_fixture


def test_favorable_with_legend_is_not_misread_as_critical():
    out = parse_controle_technique(read_fixture("ct", "favorable_with_legend.txt"))
    assert out["result"] == RESULT_FAVORABLE
    assert out["result_source"] == "field"
    assert out["counts"] == {"minor": 2, "major": 0, "critical": 0}
    # Footer lines after the minor section are not defects.
    labels = " ".join(d["label"] for d in out["defects"])
    assert "AUTOSUR" not in labels and "contre-visite" not in labels
    assert out["inspection_date"] == "2026-03-12"
    assert out["inspection_km"] == 98765
    assert out["parse_confidence"] == 1.0
    assert out["odometer"] == [
        {"date": "2024-03-10", "km": 72410},
        {"date": "2026-03-12", "km": 98765},
    ]
    assert out["odometer_inconsistent"] is False


def test_plural_headers_and_neant_section():
    out = parse_controle_technique(read_fixture("ct", "majeure_plural.txt"))
    assert out["result"] == RESULT_MAJOR
    assert out["counts"] == {"minor": 1, "major": 2, "critical": 0}
    brake = out["defects"][0]
    assert brake["code"] == "1.1.13.a.2" and brake["system"] == "freinage"
    assert brake["label"].startswith("Plaquettes de frein")
    # Continuation line is appended to the previous defect label.
    assert "côté gauche" in out["defects"][1]["label"]


def test_nfd_encoded_text_still_parses():
    text = unicodedata.normalize("NFD", read_fixture("ct", "majeure_plural.txt"))
    out = parse_controle_technique(text)
    assert out["result"] == RESULT_MAJOR
    assert out["counts"]["major"] == 2


def test_critical_result_and_systems():
    out = parse_controle_technique(read_fixture("ct", "critique.txt"))
    assert out["result"] == RESULT_CRITICAL
    assert out["counts"] == {"minor": 0, "major": 1, "critical": 2}
    assert {d["system"] for d in out["defects"]} == {"freinage", "structure", "direction"}


def test_result_derived_from_defects_when_field_missing():
    out = parse_controle_technique(read_fixture("ct", "no_result_field.txt"))
    assert out["result"] == RESULT_MAJOR
    assert out["result_source"] == "defects"
    assert out["parse_confidence"] < 0.8


def test_volontaire_inspection_has_reduced_confidence():
    out = parse_controle_technique(read_fixture("ct", "volontaire.txt"))
    assert out["nature"] == "volontaire"
    assert out["parse_confidence"] <= 0.6


def test_empty_or_garbage_is_unknown():
    assert parse_controle_technique(None)["result"] == RESULT_UNKNOWN
    out = parse_controle_technique("lorem ipsum " * 20)
    assert out["result"] == RESULT_UNKNOWN and out["parse_confidence"] == 0.3


def test_odometer_rollback_is_flagged():
    text = (
        "Nature du contrôle : Contrôle technique périodique\nRésultat : Favorable\n"
        "Relevés : 01/02/2022 150 000 km ; 03/02/2024 90 000 km\n"
    )
    assert parse_controle_technique(text)["odometer_inconsistent"] is True


def test_suffix_disagreement_lowers_confidence():
    text = (
        "Résultat : Défavorable pour défaillances majeures\n"
        "DÉFAILLANCES MAJEURES :\n1.1.13.a.1 Plaquettes usées\n"
    )
    out = parse_controle_technique(text)
    assert out["defects"][0]["severity"] == "major"  # header wins over suffix
    assert out["suffix_disagreements"] == 1
    assert out["parse_confidence"] == 0.9
