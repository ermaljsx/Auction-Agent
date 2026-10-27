"""This week's sale for a room (synthetic room and calendar pages; real markup comes from recon)."""
import argparse
from datetime import date, datetime

from auction_agent import cli, db
from auction_agent.adapters.alcopa import (
    AlcopaAdapter,
    parse_sale_links,
    pick_weekly_sale,
    room_from_url,
)
from auction_agent.http_client import BlockedError
from tests.fakes import FakeHttp
from tests.test_ingest import IDS, PDF, detail_html
from tests.conftest import read_fixture

TODAY = date(2026, 10, 27)  # a Tuesday
ROOM = "/salle-de-vente-encheres/beauvais"

ROOM_PAGE = """<html><head><title>Salle de vente Beauvais</title></head><body>
<h1>Salle de vente aux enchères de Beauvais</h1>
<div class="sales">
  <div class="sale"><h5>Vente du lundi 26 octobre 2026 : Véhicules Utilitaires et Tourisme</h5>
    <a href="/salle-de-vente-encheres/beauvais/10206">Voir la vente</a></div>
  <div class="sale"><h5>Lundi 2 novembre : Véhicules non roulants</h5>
    <a href="/salle-de-vente-encheres/beauvais/10230">Voir la vente</a></div>
  <div class="sale"><h5>Lundi 2 novembre : Véhicules Utilitaires et Tourisme</h5>
    <a href="/salle-de-vente-encheres/beauvais/10231">Voir la vente</a></div>
  <div class="sale"><h5>Mardi 3 novembre 2026 : Motos</h5>
    <a href="/salle-de-vente-encheres/beauvais/10240">Voir la vente</a></div>
  <div class="sale"><h5>Lundi 9 novembre 2026 : Véhicules Utilitaires et Tourisme</h5>
    <a href="/salle-de-vente-encheres/beauvais/10199">Voir la vente</a></div>
</div>
<a href="https://evil.example/salle-de-vente-encheres/beauvais/66666">lundi 2 novembre 2026</a>
</body></html>"""

CALENDAR = """<html><body><table>
<tr><td>02/11/2026</td><td>Paris Sud</td><td><a href="/salle-de-vente-encheres/paris-sud/7001">Tourisme</a></td></tr>
<tr><td>02/11/2026</td><td>Beauvais</td><td><a href="/salle-de-vente-encheres/beauvais/10231">Utilitaires et Tourisme</a></td></tr>
</table></body></html>"""


def test_room_url_detection():
    assert room_from_url(ROOM) == "beauvais" and room_from_url(ROOM + "/") == "beauvais"
    assert room_from_url("https://www.alcopa-auction.fr/salle-de-vente-encheres/beauvais") == "beauvais"
    assert room_from_url(ROOM + "/10206") is None
    assert room_from_url("https://evil.example/salle-de-vente-encheres/beauvais") is None


def test_sale_links_carry_their_own_date_and_title():
    links = {c["external_id"]: c for c in parse_sale_links(ROOM_PAGE, TODAY, "beauvais")}
    assert set(links) == {"10206", "10230", "10231", "10240", "10199"}  # foreign host ignored
    assert links["10206"]["sale_date"] == date(2026, 10, 26)
    assert links["10231"]["sale_date"] == date(2026, 11, 2)  # no year: the coming one
    assert "Tourisme" in links["10231"]["text"] and "non roulants" in links["10230"]["text"]


def test_next_monday_tourism_sale_is_chosen_whatever_its_id():
    links = parse_sale_links(ROOM_PAGE, TODAY, "beauvais")
    chosen = pick_weekly_sale(links, TODAY)
    assert chosen["external_id"] == "10231"  # not 10230 (non-runners), not past 10206, not 10199 (lower id, later)
    assert pick_weekly_sale(links, date(2026, 11, 2))["external_id"] == "10231"  # sale day still counts
    assert pick_weekly_sale(links, date(2026, 11, 3))["external_id"] == "10199"


def test_resolver_falls_back_to_the_calendar_and_reports_why():
    undated = ROOM_PAGE.replace("lundi 26 octobre 2026", "").replace("Lundi 2 novembre", "").replace(
        "Mardi 3 novembre 2026", "").replace("Lundi 9 novembre 2026", "").replace("lundi 2 novembre 2026", "")
    http = FakeHttp({ROOM: undated, "/calendrier-des-ventes": CALENDAR})
    sale, diag = AlcopaAdapter(http).resolve_next_sale("beauvais", today=TODAY)
    assert sale.external_id == "10231" and sale.url.endswith("/beauvais/10231?sort=number_asc&category_view=VP")
    assert diag["pages"] == {"room": 5, "calendar": 1} and diag["chosen"] == {"id": "10231", "date": "2026-11-02"}


def test_resolver_returns_none_with_structure_hints():
    http = FakeHttp({ROOM: "<html><head><title>Maintenance</title></head><body><h1>Bientôt</h1></body></html>",
                     "/calendrier-des-ventes": "<html><body></body></html>"})
    sale, diag = AlcopaAdapter(http).resolve_next_sale("beauvais", today=TODAY)
    assert sale is None and diag["chosen"] is None
    assert "Maintenance" in diag["hints"]["room"]


def test_a_block_on_the_room_page_stops_the_run():
    http = FakeHttp({ROOM: BlockedError("405 challenge")})
    try:
        AlcopaAdapter(http).resolve_next_sale("beauvais", today=TODAY)
    except BlockedError:
        pass
    else:  # pragma: no cover
        raise AssertionError("BlockedError must propagate")


# ------------------------------------------------------------------ CLI

def _fake_client(monkeypatch, http):
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: http)


def test_crawl_with_room_url_crawls_this_weeks_sale_quietly(settings, monkeypatch, capsys):
    sale = "/salle-de-vente-encheres/beauvais/10231"
    query = "sort=number_asc&category_view=VP"
    routes = {ROOM: ROOM_PAGE, f"{sale}?{query}&page=1": read_fixture("alcopa", "sale_page1.html"),
              f"{sale}?{query}&page=2": read_fixture("alcopa", "sale_page2.html")}
    for pid, path in IDS.items():
        routes[path] = detail_html(pid, f"CT{pid}")
        routes[f"/getDocument/ct/CT{pid}"] = PDF
    http = FakeHttp(routes)
    _fake_client(monkeypatch, http)
    monkeypatch.setattr("auction_agent.adapters.alcopa.datetime", _FixedDatetime)
    args = argparse.Namespace(house="alcopa", url=ROOM, pages=None, limit=None, no_docs=False,
                              force_unlock=False, quiet=True)
    assert cli.cmd_crawl(args, settings) == 0
    out = capsys.readouterr().out
    assert "chosen: 10231 on 2026-11-02" in out
    assert "detailed: 3" in " ".join(out.split()) and "documents: 3" in " ".join(out.split())
    for pid in IDS:
        assert pid not in out  # no per-vehicle line in public logs
    assert "PEUGEOT" not in out and "[1/" not in out


def test_crawl_exits_3_when_no_sale_is_found(settings, monkeypatch, capsys):
    _fake_client(monkeypatch, FakeHttp({ROOM: "<html></html>", "/calendrier-des-ventes": "<html></html>"}))
    args = argparse.Namespace(house="alcopa", url=ROOM, pages=None, limit=None, no_docs=False,
                              force_unlock=False, quiet=True)
    assert cli.cmd_crawl(args, settings) == 3
    assert "No upcoming sale" in capsys.readouterr().out


def test_recon_reports_robots_and_the_chosen_sale(settings, monkeypatch, capsys):
    http = FakeHttp({ROOM: ROOM_PAGE})
    http.disallow = ("/getDocument/",)
    _fake_client(monkeypatch, http)
    monkeypatch.setattr("auction_agent.adapters.alcopa.datetime", _FixedDatetime)
    assert cli.cmd_recon(argparse.Namespace(house="alcopa", room="beauvais"), settings) == 0
    out = capsys.readouterr().out
    assert "CT document: DISALLOWED" in out and "room page: allowed" in out
    assert "chosen: 10231 on 2026-11-02" in out and http.count("/calendrier-des-ventes") == 0


def test_stats_explains_partial_results(settings, capsys):
    from tests.test_scoring import TODAY as SCORE_TODAY
    from tests.test_service_report import NOW, seed
    from auction_agent.scoring.service import rescore_all
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=3)
        with db.write_tx(conn):
            conn.execute("UPDATE listings SET starting_price = NULL")
        rescore_all(conn, settings, today=SCORE_TODAY, now=NOW, use_llm=False)
    cli.cmd_stats(argparse.Namespace(), settings)
    out = capsys.readouterr().out
    assert "partial 3" in out and "price_unknown × 3" in out


class _FixedDatetime(datetime):
    """datetime with 'now' fixed at Tue 27 Oct 2026 10:00 (Paris), everything else unchanged."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 27, 10, 0, tzinfo=tz)
