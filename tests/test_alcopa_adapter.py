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


# ------------------------------------------------------------------ 2026 detail layout (synthetic, invented values)

LAYOUT_2026 = "detail_layout_2026.html"


def test_2026_layout_reads_bare_text_comment_damages_warranty_and_sale():
    detail = parse_detail_page(read_fixture("alcopa", LAYOUT_2026), "1234567")
    # The comment is a bare text node (no <p>); the HTML comment beside it is not read.
    assert detail.comments == "Révision faite en 2025. Deux clés, carnet présent."
    assert detail.fields["warranty"] == "Garantie Essentielle de 3 mois incluse"
    assert detail.sale_url == "https://www.alcopa-auction.fr/salle-de-vente-encheres/beauvais/10999"
    assert detail.photo_count == 2  # the two /damages/ pictures are not counted
    assert detail.fields["current_price_attr"] == 6300.0
    assert detail.vin == "VF1TESTZZ00000003" and "VF1TESTZZ00000003" not in str(detail.raw_specs)
    # Deleted items (the item or its damage) are skipped; the rim is kept for display.
    assert detail.damages == [
        {"zone": "Pare-choc avant", "kind": "Peinture"},
        {"zone": "Porte avant droite", "kind": "Débosselage"},
        {"zone": "Jante avant gauche", "kind": "Peinture"},
    ]


def test_damages_never_carry_photos_files_or_staff_ids():
    import json
    detail = parse_detail_page(read_fixture("alcopa", LAYOUT_2026), "1234567")
    dump = json.dumps(detail.damages, ensure_ascii=False).lower()
    for word in ("http", ".jpg", "photo", "bucket", "user", "created", "proges", "900001", "example.test"):
        assert word not in dump
    assert all(set(d) == {"zone", "kind"} for d in detail.damages)


def _layout_with_source(source: str) -> str:
    html = read_fixture("alcopa", LAYOUT_2026)
    start = html.index('data-source="') + len('data-source="')
    return html[:start] + source + html[html.index('"', start):]


@pytest.mark.parametrize("source,expected", [
    ("[]", []), ("{not json", None), ("&quot;text&quot;", None), ("{}", None), ("[1, &quot;x&quot;, null]", []),
])
def test_odd_damage_json_never_raises(source, expected):
    assert parse_detail_page(_layout_with_source(source), "1234567").damages == expected


def test_damage_labels_are_cleaned_and_links_rejected():
    import html as html_lib
    import json
    items = [{"product_id": 1234567, "is_deleted": 0, "is_show": 1,
              "damage": {"is_deleted": 0, "zone_label": "  Capot\n avant ", "type_label": "https://x.test/a.jpg"}},
             {"product_id": "1234567", "damage": {"zone_label": "photos/12/a.jpg", "type_label": "Rayure"}},
             {"product_id": 1234567, "is_show": 0, "damage": {"zone_label": "Toit", "type_label": "Grêle"}},
             {"product_id": 1234567, "damage": {"zone_label": "", "type_label": None}},
             {"product_id": 1234567, "damage": {"zone_label": "Aile " + "x" * 100, "type_label": "Peinture"}}]
    source = html_lib.escape(json.dumps(items), quote=True)
    damages = parse_detail_page(_layout_with_source(source), "1234567").damages
    assert damages == [{"zone": "Capot avant", "kind": "Défaut"}, {"zone": "Zone inconnue", "kind": "Rayure"},
                       {"zone": ("Aile " + "x" * 100)[:60], "kind": "Peinture"}]


def test_damage_product_id_of_another_car_is_refused():
    with pytest.raises(ParseError):
        parse_detail_page(read_fixture("alcopa", LAYOUT_2026), "7654321")
    # Items of another product next to this car's own are dropped, the page is kept.
    import html as html_lib
    import json
    items = [{"product_id": 1234567, "damage": {"zone_label": "Capot", "type_label": "Peinture"}},
             {"product_id": 7654321, "damage": {"zone_label": "Toit", "type_label": "Grêle"}}]
    detail = parse_detail_page(_layout_with_source(html_lib.escape(json.dumps(items), quote=True)), "1234567")
    assert detail.damages == [{"zone": "Capot", "kind": "Peinture"}]


def test_comment_section_stops_at_the_next_heading_and_can_be_empty():
    table = '<table class="table table-striped"><tr><th>Marque</th><td>X</td></tr></table>'
    html = (f"<html><body>{table}<div><h3>Commentaires</h3> Pneus neufs. <br/> Vidange faite"
            "<h3>Lots similaires</h3> RENAULT CLIO</div></body></html>")
    assert parse_detail_page(html, "1").comments == "Pneus neufs. Vidange faite"
    empty = f"<html><body>{table}<div><h3>Commentaires</h3>   <!-- rien --> </div></body></html>"
    assert parse_detail_page(empty, "1").comments is None


def test_detail_warranty_without_cover_is_not_stored():
    html = read_fixture("alcopa", LAYOUT_2026).replace("Garantie Essentielle de 3 mois incluse*", "Sans garantie")
    assert "warranty" not in parse_detail_page(html, "1234567").fields


def test_sale_link_is_the_sale_not_the_room_menu():
    html = read_fixture("alcopa", LAYOUT_2026).replace(
        '<a href="/salle-de-vente-encheres/beauvais/10999">Retour à la liste</a>', "")
    # The room links of the menu and the breadcrumb come first; the sale link is still found.
    assert parse_detail_page(html, "1234567").sale_url.endswith("/salle-de-vente-encheres/beauvais/10999")
    no_sale = html.replace("/salle-de-vente-encheres/beauvais/10999", "/salle-de-vente-encheres/beauvais")
    assert parse_detail_page(no_sale, "1234567").sale_url is None


def test_multiline_card_price_and_current_bid():
    page = parse_sale_page(read_fixture("alcopa", "sale_card_multiline.html"), 1)
    assert page["parse_failures"] == 0 and page["expected_lots"] == 2
    auris, clio = page["stubs"]
    assert auris.starting_price == 15700.0 and auris.current_price_attr is None
    assert auris.warranty == "Garantie" and auris.lot_number == 777
    assert clio.starting_price is None and clio.current_price_attr == 8700.0 and clio.warranty is None
    # The older card layout: "Mise à prix : --" then the bid.
    yaris = parse_sale_page(read_fixture("alcopa", "sale_page2.html"), 2)["stubs"][0]
    assert yaris.starting_price is None and yaris.current_price_attr == 3000.0


@pytest.mark.parametrize("kind", ["room", "online"])
@pytest.mark.parametrize("buyer", ["particulier", "pro"])
@pytest.mark.parametrize("regime", ["ttc", "ht_recuperable"])
@pytest.mark.parametrize("price", [1000, 2499, 10000, 12345.5])
def test_fee_split_adds_up_to_the_fee_model(kind, buyer, regime, price):
    """The split is the fee model's one source of truth: price + VAT + fees == fee_model."""
    a = AlcopaAdapter(FakeHttp())
    split = a.fee_split(price, kind, buyer, regime)
    assert round(price + split["vat"] + split["fees"], 2) == a.fee_model(price, kind, buyer, regime)
    assert (split["vat"] > 0) == (regime == "ht_recuperable" and buyer == "particulier")
    assert (split["fees"] == 0) == (kind == "online")
    if kind == "room":
        assert split["fees"] >= a.ROOM_FEE_MIN_TTC


def test_fee_model_is_unchanged_by_the_split():
    """The old formula, kept here as the reference: identical results on a grid of prices."""
    a = AlcopaAdapter(FakeHttp())

    def old(price, kind, buyer, regime):
        base = price * (1 + a.VAT_RATE) if regime == "ht_recuperable" and buyer == "particulier" else price
        return round(base, 2) if kind == "online" else round(base + max(price * a.ROOM_FEE_RATE_TTC, a.ROOM_FEE_MIN_TTC), 2)

    for price in [*range(100, 60_000, 37), 999.99, 2500.01, 17_777.77]:
        for kind in ("room", "online"):
            for buyer in ("particulier", "pro"):
                for regime in ("ttc", "ht_recuperable"):
                    assert a.fee_model(price, kind, buyer, regime) == old(price, kind, buyer, regime)


def test_default_fee_split_puts_the_whole_extra_in_fees():
    class Flat(AuctionAdapter):
        def sale_from_url(self, url): ...
        def discover_sales(self): ...
        def list_sale(self, sale, max_pages=None): ...
        def fetch_detail(self, stub): ...
        def fee_model(self, price, sale_kind, buyer, vat_regime):
            return round(price * 1.1, 2)

    assert Flat(FakeHttp()).fee_split(1000, "room", "particulier", "ttc") == {"vat": 0.0, "fees": 100.0}
