from datetime import timezone

import pytest

from auction_agent.adapters import get_adapter_class, load_all
from auction_agent.adapters._parsing import first_int, parse_eur, parse_km_lines
from auction_agent.adapters.alcopa import (
    AlcopaAdapter,
    parse_detail_page,
    parse_sale_page,
    product_id_from_url,
)
from auction_agent.adapters.base import AuctionAdapter, ParseError
from auction_agent.http_client import BlockedError
from tests.conftest import read_fixture
from tests.fakes import FakeHttp

SALE = "/salle-de-vente-encheres/beauvais/10206"


def _http():
    return FakeHttp({
        f"{SALE}?page=1": read_fixture("alcopa", "sale_page1.html"),
        f"{SALE}?page=2": read_fixture("alcopa", "sale_page2.html"),
    })


def test_registry_discovers_alcopa_and_rejects_duplicates():
    assert "alcopa" in load_all()
    assert get_adapter_class("alcopa") is AlcopaAdapter
    with pytest.raises(ValueError):
        type("Dup", (AuctionAdapter,), {"slug": "alcopa"})


def test_mileage_is_not_merged_with_year():
    # The prototype read this card as 202,435,394 km.
    assert parse_km_lines(["GO", "1ère mise : 2024", "35 394 km", "Boîte Manuelle"]) == 35394
    assert parse_km_lines(["EH", "1ère mise : 2026", "10 km"]) == 10


def test_number_helpers():
    assert parse_eur("10 200 €") == 10200.0
    assert parse_eur("10.200 €") == 10200.0
    assert parse_eur("10 200,50 €") == 10200.5
    assert parse_eur("--") is None
    assert first_int("1598 cm3") == 1598  # prototype returned 15983


def test_product_id_and_category_from_url():
    assert product_id_from_url(
        "https://www.alcopa-auction.fr/voiture-occasion/peugeot/208-puretech-100-1120464"
    ) == ("voiture", "1120464")
    assert product_id_from_url("/utilitaire-occasion/renault/master-dci-877816/") == ("utilitaire", "877816")
    assert product_id_from_url("/ro/voiture-second-hand/renault/clio-1049046") == ("voiture", "1049046")
    assert product_id_from_url("/salle-de-vente-encheres/beauvais/10206") is None


def test_parse_sale_page_cards():
    page = parse_sale_page(read_fixture("alcopa", "sale_page1.html"), 1)
    assert page["expected_lots"] == 3 and page["has_next"] is True
    assert page["room"] == "Beauvais" and page["sale_date"].isoformat() == "2026-10-19"
    peugeot, clio = page["stubs"]
    assert peugeot.external_id == "1120464" and peugeot.category == "voiture"
    assert (peugeot.brand, peugeot.model) == ("PEUGEOT", "208")
    assert peugeot.mileage_km == 35394 and peugeot.year == 2019
    assert peugeot.fuel == "petrol" and peugeot.gearbox == "automatic"
    assert peugeot.lot_number == 12 and peugeot.starting_price == 10200.0
    assert peugeot.warranty == "Garantie Essentielle"
    assert peugeot.closing_hint_at.tzinfo == timezone.utc
    assert clio.warranty is None and clio.fuel == "diesel" and clio.gearbox == "manual"


def test_unnumbered_lot_and_unset_price_are_null():
    page = parse_sale_page(read_fixture("alcopa", "sale_page2.html"), 2)
    yaris = page["stubs"][0]
    assert yaris.lot_number is None
    assert yaris.starting_price is None  # '--', not the later "Enchère courante" amount
    assert page["has_next"] is False


def test_list_sale_is_complete_and_enriched():
    adapter = AlcopaAdapter(_http())
    listing = adapter.list_sale(adapter.sale_from_url(SALE))
    assert listing.complete is True and listing.reasons == []
    assert [s.external_id for s in listing.stubs] == ["1120464", "1098892", "1119509"]
    assert listing.sale.expected_lots == 3 and listing.sale.room == "Beauvais"
    assert listing.sale.starts_at is not None and listing.sale.ends_at > listing.sale.starts_at


def test_list_sale_scoped_by_max_pages_is_not_complete():
    adapter = AlcopaAdapter(_http())
    listing = adapter.list_sale(adapter.sale_from_url(SALE), max_pages=1)
    assert listing.complete is False
    assert any(r.startswith("scoped") for r in listing.reasons)


def test_list_sale_page_failure_raises_instead_of_returning_partial():
    http = _http()
    http.routes[f"{SALE}?page=2"] = BlockedError("405")
    adapter = AlcopaAdapter(http)
    with pytest.raises(BlockedError):
        adapter.list_sale(adapter.sale_from_url(SALE))


def test_parse_detail_page():
    detail = parse_detail_page(read_fixture("alcopa", "detail_1120464.html"), "1120464")
    f = detail.fields
    assert f["mileage_km"] == 35394  # first table wins over the duplicated one
    assert f["first_registration"] == "2019-03-15" and f["year"] == 2019
    assert f["engine_cc"] == 1199 and f["co2"] == 118
    assert f["fuel"] == "petrol" and f["gearbox"] == "automatic" and f["vat_regime"] == "ttc"
    assert f["current_price_attr"] == 10200.0 and f["starting_price"] == 10200.0
    assert detail.vin == "VR3UPHNEKNT084478" and detail.registration == "FK-482-TR"
    assert "immatriculation" not in detail.raw_specs and "numéro de série" not in detail.raw_specs
    assert [d.doc_type for d in detail.documents] == ["ct", "maintenance"]
    assert detail.photo_count == 2
    assert detail.comments.startswith("Rayure porte AVG")
    assert "RENAULT" not in detail.comments  # stops at the next heading
    assert detail.sale_url.endswith("/salle-de-vente-encheres/beauvais/10206")


def test_detail_without_spec_table_raises():
    with pytest.raises(ParseError):
        parse_detail_page(read_fixture("alcopa", "detail_consent.html"), "1")


def test_detail_product_id_mismatch_raises():
    with pytest.raises(ParseError):
        parse_detail_page(read_fixture("alcopa", "detail_mismatch.html"), "1120464")


def test_fee_model():
    a = AlcopaAdapter(FakeHttp())
    assert a.fee_model(10000, "room", "particulier", "ttc") == 11440.0
    assert a.fee_model(1000, "room", "particulier", "ttc") == 1360.0  # minimum fee
    assert a.fee_model(10000, "online", "particulier", "ttc") == 10000.0
    assert a.fee_model(10000, "online", "particulier", "ht_recuperable") == 12000.0
    assert a.fee_model(10000, "online", "pro", "ht_recuperable") == 10000.0


def test_sale_from_url_kinds():
    a = AlcopaAdapter(FakeHttp())
    assert a.sale_from_url("/vente-encheres-en-ligne/12902").kind == "online"
    room = a.sale_from_url("https://www.alcopa-auction.fr/salle-de-vente-encheres/paris-sud/10350?x=1")
    assert (room.kind, room.room, room.external_id) == ("room", "Paris Sud", "10350")
    with pytest.raises(ValueError):
        a.sale_from_url("/voiture-occasion/peugeot/208-1120464")
