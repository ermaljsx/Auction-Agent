"""Regression tests for the Phase 1 review findings on parsing and redaction."""
import pytest

from auction_agent.adapters._parsing import first_int, parse_eur, parse_km
from auction_agent.adapters.alcopa import (
    AlcopaAdapter,
    normalise_fuel,
    parse_card,
    parse_detail_page,
    parse_sale_page,
)
from auction_agent.ct_parser import RESULT_MAJOR, parse_controle_technique
from auction_agent.privacy import redact, scrub_for_llm
from tests.conftest import read_fixture
from tests.fakes import FakeHttp

SALE = "/salle-de-vente-encheres/beauvais/10206"


# ------------------------------------------------------------------ detail labels (parsing-1, conformance-3)

def _detail(rows: str) -> str:
    return f'<html><body><table class="table table-striped">{rows}</table></body></html>'


def test_nbsp_labels_still_identify_vin_and_plate():
    html = _detail("<tr><th>Immatriculation&nbsp;:</th><td>FK-482-TR</td></tr>"
                   "<tr><th>Numéro de série&nbsp;:</th><td>VR3UPHNEKNT084478</td></tr>"
                   "<tr><th>Kilométrage&nbsp;:</th><td>35 394 km</td></tr>")
    d = parse_detail_page(html, "1")
    assert d.vin == "VR3UPHNEKNT084478" and d.registration == "FK-482-TR"
    assert d.fields["mileage_km"] == 35394
    assert "VR3UPHNEKNT084478" not in str(d.raw_specs) and "FK-482-TR" not in str(d.raw_specs)


@pytest.mark.parametrize("label", ["VIN", "N° de série", "N° d'identification", "Numéro de châssis"])
def test_vin_label_variants(label):
    d = parse_detail_page(_detail(f"<tr><th>{label}</th><td>VR3UPHNEKNT084478</td></tr>"), "1")
    assert d.vin == "VR3UPHNEKNT084478" and "VR3UPHNEKNT084478" not in str(d.raw_specs)


def test_unknown_label_holding_a_vin_is_redacted_and_still_hashed():
    d = parse_detail_page(_detail("<tr><th>Référence</th><td>VR3UPHNEKNT084478</td></tr>"), "1")
    assert d.vin == "VR3UPHNEKNT084478" and "VR3UPHNEKNT084478" not in str(d.raw_specs)


def test_registration_date_label_is_not_the_plate():
    d = parse_detail_page(_detail("<tr><th>Date de 1ère immatriculation</th><td>15/03/2019</td></tr>"), "1")
    assert d.registration is None


# ------------------------------------------------------------------ redaction (parsing-2, parsing-6)

@pytest.mark.parametrize("text", [
    "N°d'identificationVF3ABCDEFGH123456", "E VF3ABCDEFGH123456Date", "vin vf3abcdefgh123456",
    "VIN VF3 ABCDE FGH123456", "Immat ab-123-cd", "Immat AB 123 CD", "Immat AB–123–CD",
    "ImmatAB-123-CD",
])
def test_redaction_catches_variants(text):
    out = redact(text)
    assert "123456" not in out.replace("[VIN]", "") or "[VIN]" in out
    assert "[VIN]" in out or "[IMMAT]" in out


@pytest.mark.parametrize("text", [
    "HISTORIQUE : 10/03/2024 172 410 KM 12/03/2026 98 765 KM",
    "KILOMETRAGE RELEVE : 98 765 KM 12/03/2026",
    "Puissance 110 CH 12 cylindres",
    "1.1.13.a.2 Plaquettes de frein : usure excessive AV",
    "fuite de 123 km",
])
def test_redaction_does_not_destroy_data(text):
    assert redact(text) == text


def test_ct_mileage_and_odometer_survive_redaction(settings):
    from auction_agent.documents import process_document_bytes
    from tests.conftest import FIXTURES
    doc = process_document_bytes("ct", "u", (FIXTURES / "pdf" / "ct_min.pdf").read_bytes(), settings)
    assert doc.parsed["inspection_km"] == 151220


def test_llm_scrub_removes_prices_phones_and_mail():
    out = scrub_for_llm("Frais 1 250 €. Mise à prix 6 900 € HT. Tél 06 12 34 56 78, a.b@x.fr, ab-123-cd")
    assert "€" not in out and "06 12" not in out and "@" not in out and "ab-123-cd" not in out


def test_llm_scrub_removes_links_and_ht_amounts():
    out = scrub_for_llm("Photos sur https://www.alcopa-auction.fr/x?id=12 ou www.garage.fr/p ; reprise 6 900 HT")
    assert "http" not in out and "www." not in out and "6 900" not in out
    assert scrub_for_llm("Moteur 1.6 HDi 110 ch, 120 000 km, boîte HS") == "Moteur 1.6 HDi 110 ch, 120 000 km, boîte HS"


# ------------------------------------------------------------------ CT parser (parsing-3, 8, 9, 11)

def test_page_furniture_does_not_end_the_section():
    text = ("Résultat : Défavorable pour défaillances majeures\nDÉFAILLANCES MAJEURES :\n"
            "5.3.1.b.2 Amortisseur\nPage 1/2\n\nPROCÈS-VERBAL DE CONTRÔLE TECHNIQUE N° 123456\n"
            "1.1.13.a.2 Plaquettes de frein\n2.1.3.a.2 Direction\n")
    out = parse_controle_technique(text)
    assert out["counts"]["major"] == 3


def test_legend_lines_are_not_section_headers():
    text = ("Résultat : Défavorable pour défaillances majeures\n"
            "Défaillances critiques : danger direct\nDéfaillances majeures : contre-visite sous 2 mois\n"
            "Défaillances mineures : sans contre-visite\nDéfaillances constatées :\n"
            "1.1.13.a.2 Plaquettes de frein : usure excessive\n2.1.3.a.2 Direction : jeu excessif\n")
    out = parse_controle_technique(text)
    assert out["counts"]["minor"] == 0
    assert out["counts"]["major"] == 2 or out["parse_confidence"] < 0.8


def test_defect_on_header_line_and_s_headers_and_wrapped_numbers():
    out = parse_controle_technique(
        "Résultat : Défavorable pour défaillances majeures\n"
        "DÉFAILLANCES MAJEURES 1.1.13.a.2 Plaquettes de frein usées\n5.3.1.b.2 Amortisseur\n1.6 mm\n")
    assert out["counts"]["major"] == 2 and all(d["code"] != "1.6" for d in out["defects"])
    out2 = parse_controle_technique(
        "Résultat : Défavorable pour défaillances majeures\nDéfaillance(s) majeure(s) :\n1.1.13.a.2 Plaquettes\n")
    assert out2["counts"]["major"] == 1 and out2["parse_confidence"] >= 0.8


def test_result_without_matching_defects_lowers_confidence():
    out = parse_controle_technique("Résultat : Défavorable pour défaillances majeures\nAucune information\n" * 3)
    assert out["result"] == RESULT_MAJOR and out["parse_confidence"] <= 0.6


def test_registration_date_is_not_an_odometer_reading():
    text = ("Résultat : Favorable\nDate de 1ère immatriculation : 15/03/2019   Kilométrage relevé : 98 765 km\n"
            "Historique : 10/03/2024 72 410 km ; 12/03/2026 98 765 km\n")
    assert parse_controle_technique(text)["odometer_inconsistent"] is False


# ------------------------------------------------------------------ listing walk (parsing-4, parsing-5)

def test_disabled_next_button_and_clamped_pages_terminate():
    page1 = read_fixture("alcopa", "sale_page1.html")
    page2 = read_fixture("alcopa", "sale_page2.html").replace(
        "</nav>", '<button name="page" value="suivant" disabled>›</button></nav>')
    http = FakeHttp({f"{SALE}?page=1": page1})
    for n in range(2, 60):
        http.routes[f"{SALE}?page={n}"] = page2
    adapter = AlcopaAdapter(http)
    listing = adapter.list_sale(adapter.sale_from_url(SALE))
    assert listing.pages_fetched <= 3


def test_empty_middle_page_and_unknown_count_are_incomplete():
    page1 = read_fixture("alcopa", "sale_page1.html").replace("Nombre de lots : <b>3</b>", "")
    http = FakeHttp({f"{SALE}?page=1": page1, f"{SALE}?page=2": "<html><body></body></html>"})
    adapter = AlcopaAdapter(http)
    listing = adapter.list_sale(adapter.sale_from_url(SALE))
    assert listing.complete is False
    assert "expected_lots_unknown" in listing.reasons and "empty_page=2" in listing.reasons


@pytest.mark.parametrize("snippet,expected", [
    ("Nombre de lots&nbsp;: <b>40</b>", 40), ("Nombre de lots : <strong>120</strong>", 120),
    ("Nombre de lots : <b>1 234</b>", 1234),
])
def test_expected_lots_variants(snippet, expected):
    assert parse_sale_page(f"<html><body><p>{snippet}</p></body></html>")["expected_lots"] == expected


# ------------------------------------------------------------------ fuels, numbers, timestamps

@pytest.mark.parametrize("label,fuel", [
    ("Hybride essence", "hybrid"), ("Essence-électricité (hybride rechargeable)", "plugin_hybrid"),
    ("Essence-électricité (hybride non rechargeable)", "hybrid"), ("Gazole-électricité (hybride non rechargeable)", "hybrid"),
    ("Essence", "petrol"), ("Électrique", "electric"),
])
def test_hybrid_fuel_labels(label, fuel):
    assert normalise_fuel(label) == fuel


def test_number_parsers_have_a_left_boundary():
    assert parse_km("35.394 km") == 35394
    assert parse_km("35,394 km") is None  # ambiguous: never a fragment
    assert first_int("1.598 cm3") == 1598
    assert parse_eur("10200.00 €") == 10200.0
    assert parse_eur("10,200 €") is None


@pytest.mark.parametrize("ts", ["1792396800000", "99999999999999999999"])
def test_bad_or_millisecond_countdown_does_not_break_the_card(ts):
    html = read_fixture("alcopa", "sale_page1.html").replace('data-ts="1792396800"', f'data-ts="{ts}"')
    page = parse_sale_page(html, 1)
    assert page["parse_failures"] == 0 and len(page["stubs"]) == 2


def test_lookalike_hosts_and_foreign_document_links_are_refused():
    from bs4 import BeautifulSoup
    card = BeautifulSoup('<div><h5 class="card-title"><a href="https://evilalcopa-auction.fr/voiture-occasion/a/b-123456">X</a></h5></div>', "lxml")
    with pytest.raises(Exception):
        parse_card(card.div)
    html = _detail("<tr><th>Marque</th><td>X</td></tr>") .replace(
        "</table>", '</table><a href="http://169.254.169.254/getDocument/ct/x">CT</a>')
    assert parse_detail_page(html, "1").documents == []


# ------------------------------------------------------------------ real captured pages (conformance-9)

def _real_pages():
    from tests.conftest import FIXTURES
    folder = FIXTURES / "alcopa" / "real"
    return sorted(folder.glob("*.html")) if folder.exists() else []


@pytest.mark.parametrize("path", _real_pages(), ids=lambda p: p.name)
def test_real_captured_pages_parse(path):
    """Runs only after `python -m auction_agent capture` (git-ignored pages); skipped otherwise."""
    html = path.read_text(encoding="utf-8")
    if path.name.startswith("sale-"):
        page = parse_sale_page(html, 1)
        assert page["stubs"] and page["parse_failures"] == 0
    else:
        detail = parse_detail_page(html, "real")
        assert detail.fields, "no spec field recognised: selectors drifted"
        if detail.vin:
            assert detail.vin not in str(detail.raw_specs)
