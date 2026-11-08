"""The online MVP: snapshot, publish (PC runner), Blob storage and the site's snapshot function."""
import argparse
import importlib.util
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from auction_agent import cli, db
from auction_agent.blobstore import SNAPSHOT_PATH, LocalStore, StoreError, VercelBlobStore
from auction_agent.snapshot import build_snapshot, sale_state
from tests.conftest import PROJECT_ROOT, read_fixture
from tests.fakes import FakeHttp
from tests.test_ingest import IDS, PDF, detail_html
from tests.test_service_report import FakeClient

ROOM = "/salle-de-vente-encheres/beauvais"
SALE = "/salle-de-vente-encheres/beauvais/10206"
QUERY = "sort=number_asc&category_view=VP"
# Thursday before the Monday 19 October 2026 sale of the synthetic fixtures.
NOW = datetime(2026, 10, 15, 17, 30, tzinfo=timezone.utc)
ROOM_PAGE = ('<html><body><div class="sale"><h5>Lundi 19 octobre 2026 : Véhicules Utilitaires et Tourisme</h5>'
             f'<a href="{SALE}">Voir la vente</a></div></body></html>')
VIN, PLATE = "VR3UPHNEKNT084478", "FK-482-TR"


def site_routes():
    routes = {ROOM: ROOM_PAGE, f"{SALE}?{QUERY}&page=1": read_fixture("alcopa", "sale_page1.html"),
              f"{SALE}?{QUERY}&page=2": read_fixture("alcopa", "sale_page2.html")}
    for pid, path in IDS.items():
        routes[path] = detail_html(pid, f"CT{pid}")
        routes[f"/getDocument/ct/CT{pid}"] = PDF
    return routes


def publish_args(out, **kw):
    base = dict(house="alcopa", url=ROOM, no_crawl=False, no_llm=False, llm_limit=None,
                out=str(out) if out is not None else None)
    base.update(kw)
    return argparse.Namespace(**base)


class Recording(FakeClient):
    """Stands in for the Claude client that make_client builds inside rescore_all (no network)."""

    provider = "anthropic"

    def __init__(self, *_a, reasoning_effort="low", **_kw):
        super().__init__()
        self.model, self.reasoning_effort, self.notes = "claude-haiku-5-5", reasoning_effort, []


def fake_make_client(settings):
    return Recording(reasoning_effort=settings.llm_reasoning_effort)


MAKE_CLIENT = "auction_agent.scoring.service.make_client"
AI_KEY = "sk-ant-test"


@pytest.fixture
def site(monkeypatch):
    http = FakeHttp(site_routes())
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: http)
    monkeypatch.setattr(MAKE_CLIENT, fake_make_client)
    return http


def read_snapshot(out):
    return json.loads((out / SNAPSHOT_PATH).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ publish

def test_publish_crawls_and_never_lists_red_cars(settings, site, tmp_path, capsys):
    """The synthetic lots all carry an unfavourable CT, so all three are red: counted, not listed."""
    out = tmp_path / "out"
    s = settings.replace(anthropic_api_key=AI_KEY)
    assert cli.cmd_publish(publish_args(out), s, now=NOW) == 4
    assert "no car could be put forward" in capsys.readouterr().out
    snap = read_snapshot(out)
    assert snap["schema"] == 2 and snap["sale"]["external_id"] == "10206"
    assert snap["sale"]["date"] == "2026-10-19" and snap["sale"]["state"] == "upcoming"
    assert snap["crawl"]["status"] == "ok" and snap["crawl"]["counts"]["detailed"] == 3
    assert snap["counts"]["red"] == 3 and snap["top"] == [] and snap["cards"] == []
    raw = json.dumps(snap, ensure_ascii=False)
    for secret in (VIN, PLATE, "vin_hmac", "registration", "comments", "photo", '"id"'):
        assert secret not in raw


def test_publish_puts_cars_forward_with_ai_verdicts(settings, monkeypatch, tmp_path):
    """Seeded sale of favourable cars, published without a crawl (as after a blocked run)."""
    from tests.test_service_report import NOW as SEED_NOW, seed
    monkeypatch.setattr(MAKE_CLIENT, fake_make_client)
    s = settings.replace(anthropic_api_key=AI_KEY)
    with db.open_db(s.db_path) as conn:
        seed(conn)
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out, no_crawl=True), s, now=SEED_NOW) == 0
    snap = read_snapshot(out)
    assert snap["crawl"]["status"] == "skipped" and snap["sale"]["state"] == "upcoming"
    assert snap["top"] and snap["llm"]["calls"] >= 1 and snap["llm"]["models_answered"] == ["claude-haiku-5-5"]
    # Every listed car carries a verdict, and the run's provider, tokens and cost are published.
    assert all(c["verdict"] for c in snap["top"] + snap["cards"])
    llm = snap["llm"]
    assert llm["provider"] == "anthropic" and llm["model"] == "claude-haiku-5-5" and llm["reasoning_effort"] == "low"
    assert llm["tokens_in"] > 0 and llm["tokens_out"] > 0 and llm["cost_usd"] > 0
    assert llm["coverage"]["with_verdict"] == llm["coverage"]["listed"] == len(snap["top"]) + len(snap["cards"])
    assert any(n.startswith("AI review: ") and "$" in n for n in llm["notes"])
    raw = json.dumps(snap, ensure_ascii=False)
    for secret in ("vin_hmac", "registration", "comments", "photo", '"id"'):
        assert secret not in raw
    for c in snap["top"] + snap["cards"]:
        assert c["url"] is None or c["url"].startswith("https://www.alcopa-auction.fr/")
        assert c["tier"] != "red" and len(c["reasons"]) <= 3


def test_provisional_picks_when_no_car_is_complete(settings, monkeypatch, tmp_path):
    from tests.test_service_report import NOW as SEED_NOW, seed
    monkeypatch.setattr(MAKE_CLIENT, fake_make_client)
    s = settings.replace(anthropic_api_key=AI_KEY)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        with db.write_tx(conn):
            conn.execute("UPDATE listings SET starting_price = NULL")  # prices not published yet
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out, no_crawl=True), s, now=SEED_NOW) == 0
    snap = read_snapshot(out)
    assert snap["provisional"] is True and snap["top"]
    assert all(c["confidence"] == "partial" and c["verdict"] for c in snap["top"])


def _earlier_sale(conn, n=45):
    """An earlier upcoming sale of cheaper (better-scored) cars than the seeded sale S1."""
    from auction_agent.adapters.base import LotStub, SaleRef
    from tests.test_service_report import NOW_S, fresh_ct
    sale = SaleRef(house="alcopa", external_id="S0", url="https://www.alcopa-auction.fr/vente-encheres-en-ligne/0",
                   kind="online", starts_at=datetime(2026, 10, 14, 14, tzinfo=timezone.utc),
                   ends_at=datetime(2026, 10, 15, 18, tzinfo=timezone.utc))
    stubs = [LotStub(house="alcopa", external_id=str(200000 + i), category="voiture",
                     detail_url=f"https://www.alcopa-auction.fr/voiture-occasion/toyota/yaris-{200000 + i}",
                     brand="TOYOTA", model="YARIS", trim="YARIS 1.5 HYBRIDE 116H", fuel="hybrid", year=2019,
                     mileage_km=60_000 + 1_000 * i, gearbox="automatic", starting_price=5_000.0 + 10 * i)
             for i in range(n)]
    ct = fresh_ct()
    with db.write_tx(conn):
        sale_id = db.upsert_sale(conn, sale, NOW_S)
        db.apply_snapshot(conn, "alcopa", sale_id, stubs, NOW_S)
        for (vid,) in conn.execute("SELECT vehicle_id FROM listings WHERE sale_id = ?", (sale_id,)).fetchall():
            db.update_vehicle_detail(conn, vid, {"first_registration": "2019-03-15"})
            db.mark_complete(conn, vid, ct_state="present")
            db.upsert_document(conn, vid, {"doc_type": "ct", "source_url": "u", "fetch_status": "ok",
                                           "parse_status": "parsed", "parsed": ct}, NOW_S)


def test_publish_reviews_every_car_listed_for_the_published_sale(settings, monkeypatch, tmp_path):
    """Another upcoming sale with better cars must not take the review slots of the sale shown."""
    from tests.test_service_report import NOW as SEED_NOW, seed
    monkeypatch.setattr(MAKE_CLIENT, fake_make_client)
    s = settings.replace(anthropic_api_key=AI_KEY, top_n=2, green_min=99, yellow_min=40)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        _earlier_sale(conn)
    out = tmp_path / "out"
    cli.cmd_publish(publish_args(out, no_crawl=True), s, now=SEED_NOW)
    snap = read_snapshot(out)
    assert snap["sale"]["external_id"] == "S1"
    cov = snap["llm"]["coverage"]
    assert cov["listed"] == 8 and cov["with_verdict"] == cov["listed"], cov


def test_foreign_lot_links_are_never_published(settings, tmp_path):
    def plant(conn):
        conn.execute("UPDATE listings SET detail_url = 'https://tracker.example/lot?id=1'")

    snap = _publish_seeded(settings, tmp_path, prepare=plant)
    cars = snap["top"] + snap["cards"] + snap["compact"]
    assert cars and all(c["url"] is None for c in cars)
    assert "tracker.example" not in json.dumps(snap)


def test_models_answered_ignores_reviews_of_an_older_prompt(settings, tmp_path):
    """Only the models behind reviews of the current prompt are named (an old p2 OpenAI row is not)."""
    from auction_agent.scoring.llm import PROMPT_VERSION

    def plant(conn):
        vid = conn.execute("SELECT id FROM vehicles ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("""INSERT INTO llm_assessments (vehicle_id, kind, llm_input_hash, prompt_version, llm_model,
                            adjustment, reasons_json, verdict, status, created_at)
                        VALUES (?, 'verdict', 'old', 'p2-2026.10', 'gpt-6-luna', 0, '[]', 'v', 'ok',
                                '2026-10-01T00:00:00Z')""", (vid,))
        conn.execute("""INSERT INTO llm_assessments (vehicle_id, kind, llm_input_hash, prompt_version, llm_model,
                            adjustment, reasons_json, verdict, status, created_at)
                        VALUES (?, 'verdict', 'new', ?, 'claude-haiku-5-5', 0, '[]', 'v', 'ok',
                                '2026-10-01T00:00:00Z')""", (vid, PROMPT_VERSION))

    snap = _publish_seeded(settings, tmp_path, prepare=plant)
    assert snap["llm"]["models_answered"] == ["claude-haiku-5-5"]


def test_snapshot_never_carries_comments_or_damage_photos(settings, tmp_path):
    """Cars with a seller comment and a damage list: neither the comment nor anything that
    looks like a photo reaches the published snapshot."""
    from tests.test_scoring import LIKE_ARKANA
    from tests.test_service_report import NOW as SEED_NOW, seed
    comment = "Rayure capot, voir https://photos.static.example.test/p/1.jpg"
    with db.open_db(settings.db_path) as conn:
        seed(conn, comments=comment)
        with db.write_tx(conn):
            conn.execute("UPDATE vehicles SET damages_json = ?", (json.dumps(LIKE_ARKANA, ensure_ascii=False),))
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out, no_crawl=True, no_llm=True), settings, now=SEED_NOW) == 0
    snap = read_snapshot(out)
    raw = json.dumps(snap, ensure_ascii=False)
    assert snap["top"] and "Défauts esthétiques sur 8 éléments de carrosserie" in raw  # zones only
    for secret in ("Rayure capot", "photos.static", ".jpg", "example.test", "bucket", "photo", "comments"):
        assert secret not in raw


def test_publish_without_ai_key_is_flagged(settings, site, tmp_path, capsys):
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out), settings, now=NOW) == 4
    printed = capsys.readouterr().out
    assert "AI review is off: set ANTHROPIC_API_KEY in .env" in printed
    assert "LLM: anthropic claude-haiku-5-5, reasoning effort low, ANTHROPIC_API_KEY not set" in printed
    assert read_snapshot(out)["llm"]["enabled"] is False  # the site still gets the snapshot


def test_publish_with_a_leftover_openai_model_scores_with_the_rules(settings, monkeypatch, tmp_path, capsys):
    """A .env still saying LLM_MODEL=gpt-6-luna with the Claude provider: rules only, a clear
    note, exit 4; the crawl and the upload still happen."""
    from tests.test_service_report import NOW as SEED_NOW, seed
    s = settings.replace(anthropic_api_key=AI_KEY, llm_model="gpt-6-luna")
    with db.open_db(s.db_path) as conn:
        seed(conn)
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out, no_crawl=True), s, now=SEED_NOW) == 4
    snap = read_snapshot(out)
    assert snap["top"] and snap["llm"]["error"] == "config" and snap["llm"]["calls"] == 0
    assert any("LLM_MODEL=gpt-6-luna is an OpenAI model" in n for n in snap["llm"]["notes"])
    assert "AI review stopped (config error" in capsys.readouterr().out


def test_blocked_run_keeps_the_last_good_cars(settings, monkeypatch, tmp_path):
    from auction_agent.http_client import BlockedError
    from tests.test_service_report import NOW as SEED_NOW, seed
    monkeypatch.setattr(MAKE_CLIENT, fake_make_client)
    s = settings.replace(anthropic_api_key=AI_KEY)
    with db.open_db(s.db_path) as conn:
        seed(conn)
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out, no_crawl=True), s, now=SEED_NOW) == 0
    good = read_snapshot(out)
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: FakeHttp({ROOM: BlockedError("405 challenge")}))
    assert cli.cmd_publish(publish_args(out), s, now=SEED_NOW + timedelta(hours=2)) == 2
    snap = read_snapshot(out)
    assert snap["crawl"]["status"] == "blocked" and snap["crawl"]["reasons"]
    assert "http" not in " ".join(snap["crawl"]["reasons"])  # URLs are redacted
    assert [c["title"] for c in snap["top"]] == [c["title"] for c in good["top"]]


def test_no_sale_this_week_exits_3_and_still_publishes(settings, monkeypatch, tmp_path):
    http = FakeHttp({ROOM: "<html></html>", "/calendrier-des-ventes": "<html></html>"})
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: http)
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out), settings, now=NOW) == 3
    snap = read_snapshot(out)
    assert snap["crawl"]["status"] == "no_sale" and snap["sale"] is None and snap["top"] == []


def test_publish_without_blob_token_stops_before_crawling(settings, site, capsys):
    args = publish_args(None)
    assert cli.cmd_publish(args, settings, now=NOW) == 2
    assert "BLOB_READ_WRITE_TOKEN" in capsys.readouterr().out and site.calls == {}


def test_sale_states():
    def row(day):
        start = datetime(2026, 10, 18, 22, 0, tzinfo=timezone.utc)  # Mon 19 Oct 00:00 Paris
        return {"starts_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ends_at": (start + timedelta(hours=23, minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ")} if day else None
    sale = row(True)
    assert sale_state(sale, datetime(2026, 10, 15, 12, tzinfo=timezone.utc)) == "upcoming"
    assert sale_state(sale, datetime(2026, 10, 19, 5, tzinfo=timezone.utc)) == "today"        # 07:00 Paris
    assert sale_state(sale, datetime(2026, 10, 19, 9, tzinfo=timezone.utc)) == "in_progress"  # 11:00 Paris
    assert sale_state(sale, datetime(2026, 10, 20, 9, tzinfo=timezone.utc)) == "over"
    assert sale_state(None, NOW) == "unknown"


def test_snapshot_lists_no_red_car_and_respects_the_limit(settings, site, tmp_path):
    s = settings.replace(anthropic_api_key=AI_KEY)
    cli.cmd_publish(publish_args(tmp_path / "out"), s, now=NOW)
    with db.open_db(s.db_path) as conn:
        sale_id = conn.execute("SELECT id FROM sales").fetchone()[0]
        with db.write_tx(conn):
            conn.execute("UPDATE assessments SET tier = 'red'")
        snap = build_snapshot(conn, s, sale_id=sale_id, now=NOW, public_limit=0)
    assert snap["top"] == [] and snap["cards"] == [] and snap["counts"]["red"] >= 1


# ------------------------------------------------------------------ storage

def test_local_store_writes_atomically_inside_its_folder(tmp_path):
    store = LocalStore(tmp_path / "out")
    store.put(SNAPSHOT_PATH, b"{}", "application/json")
    assert (tmp_path / "out" / SNAPSHOT_PATH).read_bytes() == b"{}"
    with pytest.raises(StoreError):
        store.put("../escape.json", b"{}", "application/json")


def test_vercel_blob_store_checks_the_token(monkeypatch):
    with pytest.raises(StoreError, match="not set"):
        VercelBlobStore()
    with pytest.raises(StoreError, match="malformed"):
        VercelBlobStore("sk-not-a-blob-token")


def test_vercel_blob_store_uploads_privately_and_never_echoes_the_token(monkeypatch):
    calls = []
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "")  # registered, so the store's write is undone

    def fake_put(path, data, **kw):
        calls.append((path, data, kw))

    monkeypatch.setattr("vercel.blob.sync.put_blob", fake_put)
    store = VercelBlobStore("vercel_blob_rw_Abc123_secretpart")
    store.put(SNAPSHOT_PATH, b"{}", "application/json")
    assert calls == [(SNAPSHOT_PATH, b"{}", {"access": "private", "content_type": "application/json",
                                             "allow_overwrite": True, "cache_control_max_age": 60})]

    def boom(*_a, **_kw):
        raise RuntimeError("401 vercel_blob_rw_Abc123_secretpart rejected")

    monkeypatch.setattr("vercel.blob.sync.put_blob", boom)
    with pytest.raises(StoreError) as info:
        store.put(SNAPSHOT_PATH, b"{}", "application/json")
    assert "secretpart" not in str(info.value)


# ------------------------------------------------------------------ the site's snapshot function

def _web_api():
    spec = importlib.util.spec_from_file_location("web_snapshot", PROJECT_ROOT / "web" / "api" / "snapshot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOKEN = "vercel_blob_rw_HEl4l39fl2Yk5Yqa_s3cr3tvalue"


def test_blob_url_matches_the_official_sdk():
    from vercel.blob._internal.validation import construct_delivery_url
    api = _web_api()
    assert api.blob_url(TOKEN) == construct_delivery_url("store_HEl4l39fl2Yk5Yqa", "snapshot/latest.json", "private")


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self, _n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_snapshot_function_answers():
    api = _web_api()
    seen = []

    def ok(request, timeout):
        seen.append((request.full_url, request.headers.get("Authorization"), timeout))
        return _Response(b'{"schema": 1}')

    assert api.load_snapshot(TOKEN, opener=ok) == (200, b'{"schema": 1}')
    assert seen[0][0].endswith("/snapshot/latest.json") and seen[0][1] == f"Bearer {TOKEN}"

    def missing(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    def down(request, timeout):
        raise urllib.error.URLError("no route")

    for opener, expected in ((missing, (503, "no_snapshot")), (down, (502, "storage_unavailable")),
                             (lambda r, timeout: _Response(b"<html>"), (502, "snapshot_unreadable"))):
        status, body = api.load_snapshot(TOKEN, opener=opener)
        assert (status, json.loads(body)["status"]) == expected
        assert b"s3cr3t" not in body and b"blob.vercel-storage" not in body
    assert json.loads(api.load_snapshot("", opener=ok)[1])["status"] == "not_configured"
    assert json.loads(api.load_snapshot("sk-wrong", opener=ok)[1])["status"] == "not_configured"


def test_site_files_are_safe():
    app = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in app and "insertAdjacentHTML" not in app and "eval(" not in app
    vercel = json.loads((PROJECT_ROOT / "web" / "vercel.json").read_text(encoding="utf-8"))
    headers = {h["key"]: h["value"] for h in vercel["headers"][0]["headers"]}
    assert "default-src 'self'" in headers["Content-Security-Policy"] and headers["X-Robots-Tag"].startswith("noindex")
    assert (PROJECT_ROOT / "web" / "robots.txt").read_text(encoding="utf-8").strip().endswith("Disallow: /")


def test_windows_task_runs_thursday_evening_and_saturday_and_catches_up():
    task = (PROJECT_ROOT / "scripts" / "windows" / "install_task.ps1").read_text(encoding="utf-8")
    assert "-DaysOfWeek Thursday -At \"19:17\"" in task and "-DaysOfWeek Saturday -At \"09:43\"" in task
    assert "-StartWhenAvailable" in task and "-MultipleInstances IgnoreNew" in task
    runner = (PROJECT_ROOT / "scripts" / "windows" / "run_publish.ps1").read_text(encoding="utf-8")
    assert "auction_agent publish" in runner and "-Encoding utf8" in runner


# ------------------------------------------------------------------ review findings (Fable 5.1)

def test_network_failure_while_finding_the_sale_is_a_failure_not_no_sale(settings, monkeypatch, tmp_path):
    """F1: both pages unreachable -> 'failed' (exit 2, red banner), never 'no sale this week'."""
    from auction_agent.http_client import FetchError
    http = FakeHttp({ROOM: FetchError("HTTP 503"), "/calendrier-des-ventes": FetchError("HTTP 503")})
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: http)
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out), settings, now=NOW) == 2
    assert read_snapshot(out)["crawl"]["status"] == "failed"


def test_one_unreachable_page_still_allows_no_sale(settings, monkeypatch, tmp_path):
    from auction_agent.http_client import FetchError
    http = FakeHttp({ROOM: FetchError("HTTP 503"), "/calendrier-des-ventes": "<html></html>"})
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: http)
    assert cli.cmd_publish(publish_args(tmp_path / "out"), settings, now=NOW) == 3


def test_malformed_storage_response_maps_to_json_502():
    """W5: http.client errors (IncompleteRead…) are not OSError: still a clean JSON 502."""
    import http.client
    api = _web_api()

    def truncated(request, timeout):
        raise http.client.IncompleteRead(b"{", 10)

    status, body = api.load_snapshot(TOKEN, opener=truncated)
    assert status == 502 and json.loads(body)["status"] == "storage_unavailable"


def test_task_runs_on_battery_and_hidden():
    """W2: Task Scheduler's defaults skip or kill the run on a laptop on battery."""
    task = (PROJECT_ROOT / "scripts" / "windows" / "install_task.ps1").read_text(encoding="utf-8")
    assert "-AllowStartIfOnBatteries" in task and "-DontStopIfGoingOnBatteries" in task
    assert "-WindowStyle Hidden" in task


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="needs node")
def test_page_derives_the_sale_state_at_viewing_time():
    """F2/W1: the snapshot is published before the sale, so the page must work out 'in progress'
    and 'over' itself; Paris calendar day, sale under way from 08:00."""
    import subprocess
    script = (
        "const m = require(process.argv[1]);"
        "const s = {date: '2026-10-19', state: 'upcoming'};"
        "const at = ['2026-10-17T07:43:00Z','2026-10-19T05:00:00Z','2026-10-19T09:00:00Z','2026-10-20T09:00:00Z'];"
        "console.log(at.map(t => m.saleStateNow(s, new Date(t))).join(','));"
    )
    out = subprocess.run(["node", "-e", script, str(PROJECT_ROOT / "web" / "app.js")],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
    assert out == "upcoming,today,in_progress,over"


def test_page_uses_the_real_weekday_and_never_submits_the_filters():
    app = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "Vente du lundi" not in app and 'weekday: "long"' in app  # W3
    assert 'addEventListener("submit"' in app and "preventDefault" in app  # W4


# ------------------------------------------------------------------ snapshot v2 (catalogue site)

V1_CARD_FIELDS = {"title", "trim", "year", "mileage", "fuel", "gearbox", "tier", "tier_label", "score", "pillars",
                  "confidence", "effective_price", "starting_price", "sale_date", "lot", "ct", "ct_counts", "verdict",
                  "url", "reasons", "gates"}
DETAIL_KEYS = {"score", "value", "incomplete", "price", "ct", "ct_doc", "damages", "battery", "reasons"}
BLOCKED_KEYS = {"id", "vehicle_id", "vin_hmac", "comments", "photo_count", "first_registration", "storage_location",
                "raw_specs_json", "detail_url", "detail_raw", "ct_raw", "all_reasons", "flags"}


def _publish_seeded(settings, tmp_path, *, comments=None, prepare=None):
    from tests.test_service_report import NOW as SEED_NOW, seed
    with db.open_db(settings.db_path) as conn:
        seed(conn, comments=comments)
        if prepare is not None:
            with db.write_tx(conn):
                prepare(conn)
    out = tmp_path / "out"
    assert cli.cmd_publish(publish_args(out, no_crawl=True, no_llm=True), settings, now=SEED_NOW) == 0
    return read_snapshot(out)


def _keys(obj, found=None):
    found = set() if found is None else found
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            _keys(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, found)
    return found


def test_snapshot_v2_detail(settings, tmp_path):
    snap = _publish_seeded(settings, tmp_path)
    assert snap["schema"] == 2 and snap["top"]
    assert snap["scoring"] == {"weights": {"Fiabilité": 35, "État": 35, "Prix": 30},
                               "green_min": settings.green_min, "yellow_min": settings.yellow_min}
    assert "TVA si elle n'est pas récupérable" in snap["fees_note"] and "CT à refaire" in snap["fees_note"]
    for c in snap["top"] + snap["cards"]:
        assert V1_CARD_FIELDS <= set(c)  # schema 1 fields are all kept
        assert c["subtitle"] == "1.5 HYBRIDE 116H" and c["ct_result"] == "FAVORABLE"
        assert c["ct_line"] == "CT favorable, 2 défaillances mineures"
        d = c["detail"]
        assert set(d) == DETAIL_KEYS
        p = d["price"]
        parts = p["start"] + p["vat"] + p["fees"] + p["repairs"] + p["ct_redo"]
        assert abs(parts - p["total"]) < 0.02 and abs(p["total"] - c["effective_price"]) < 0.02
        assert p["start"] == c["starting_price"]
        ct = d["ct"]
        assert ct["result"] == "FAVORABLE" and ct["nature"] == "périodique" and ct["date"] == "2026-10-01"
        assert ct["km"] == 98765 and ct["valid_until"] == "2028-03-11" and ct["more"] == 0
        assert [x["severity"] for x in ct["defects"]] == ["minor", "minor"]
        assert all(x["system"] and x["label"] and x["code"] for x in ct["defects"])
        assert {x["system"] for x in ct["defects"]} == {"Essieux, roues, pneus, suspension",
                                                        "Feux et équipements électriques"}
        assert d["ct_doc"] == "parsed" and d["damages"] is None and d["battery"] is None
        assert {r["text"] for r in c["reasons"]} <= {r["text"] for r in d["reasons"]}
        assert all(r["pillar"] in ("Fiabilité", "État", "Prix", "IA") for r in d["reasons"])
        assert d["value"]["known"] is True and "médiane de" in d["value"]["text"]
        assert d["score"]["before_cap"] >= c["score"]
    assert set(snap["counts"]["compact"]) == {"green", "yellow", "red"} and "partial_why" in snap["counts"]


def test_a_running_bid_is_published_as_a_bid_not_as_the_opening_price(settings, tmp_path):
    """The price is the higher of the opening price and the card's "Enchère courante": the
    price table must say which one it starts from."""
    def bids(conn):
        # Car 1: a running bid and no opening price; car 2: a bid above its opening price.
        conn.execute("UPDATE listings SET current_price_attr = 10700, starting_price = NULL WHERE vehicle_id = 1")
        conn.execute("UPDATE listings SET current_price_attr = starting_price + 300 WHERE vehicle_id = 2")
        conn.execute("UPDATE listings SET current_price_attr = starting_price - 300 WHERE vehicle_id = 3")

    snap = _publish_seeded(settings, tmp_path, prepare=bids)
    by_km = {c["mileage"]: c for c in snap["top"] + snap["cards"]}
    first, second, third = by_km[60_000], by_km[61_000], by_km[62_000]
    assert first["starting_price"] is None
    assert first["detail"]["price"]["start"] == 10_700 and first["detail"]["price"]["basis"] == "bid"
    assert second["detail"]["price"]["start"] == second["starting_price"] + 300
    assert second["detail"]["price"]["basis"] == "bid"
    assert third["detail"]["price"]["start"] == third["starting_price"]
    assert third["detail"]["price"]["basis"] == "start"
    assert all(c["detail"]["price"]["basis"] == "start" for km, c in by_km.items() if km > 62_000)


def test_gate_text_is_shown_once(settings, tmp_path):
    snap = _publish_seeded(settings, tmp_path, comments="Kilométrage non garanti")
    cars = snap["top"] + snap["cards"]
    assert cars
    for c in cars:
        assert c["gates"] == ["Kilométrage non garanti"]
        assert "Kilométrage non garanti" not in {r["text"] for r in c["reasons"]}
        assert "Kilométrage non garanti" not in {r["text"] for r in c["detail"]["reasons"]}
        assert "kilométrage non garanti" in c["detail"]["incomplete"]
    assert snap["counts"]["partial_why"]["mileage_not_guaranteed"] == len(cars)


def test_compact_lists_every_other_lot(settings, site, tmp_path):
    """The crawl fixture's three lots are red: one line each, with their main reason and link."""
    out = tmp_path / "out"
    s = settings.replace(anthropic_api_key=AI_KEY)
    cli.cmd_publish(publish_args(out), s, now=NOW)
    snap = read_snapshot(out)
    assert snap["top"] == [] and snap["cards"] == [] and len(snap["compact"]) == 3
    assert snap["counts"]["compact"] == {"green": 0, "yellow": 0, "red": 3}
    lots = [c["lot"] for c in snap["compact"]]
    assert lots == sorted(lots, key=lambda lot: (lot is None, lot or 0))  # by lot number, unnumbered last
    for c in snap["compact"]:
        assert set(c) == {"lot", "title", "year", "mileage", "fuel", "gearbox", "effective_price", "score", "tier",
                          "ct_result", "why", "url"}
        assert c["tier"] == "red" and c["why"] and c["url"].startswith("https://www.alcopa-auction.fr/")


def test_cars_beyond_the_list_move_to_the_compact_list(settings):
    from auction_agent.scoring.service import rescore_all
    from tests.test_service_report import NOW as SEED_NOW, seed
    s = settings.replace(top_n=2)
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        rescore_all(conn, s, now=SEED_NOW, use_llm=False)
        snap = build_snapshot(conn, s, sale_id=sale_id, now=SEED_NOW, public_limit=0)
    assert len(snap["top"]) == 2 and snap["cards"] == [] and len(snap["compact"]) == 6
    assert snap["counts"]["compact"]["red"] == 0 and sum(snap["counts"]["compact"].values()) == 6
    assert not {c["mileage"] for c in snap["top"]} & {c["mileage"] for c in snap["compact"]}  # seeded lots: no number


def test_snapshot_carries_no_identifiers(settings, tmp_path):
    """No identifier key anywhere; CT defect labels and damage text are redacted again."""
    def plant(conn):
        row = conn.execute("SELECT id, parsed_json FROM documents WHERE doc_type = 'ct'").fetchone()
        ct = json.loads(row["parsed_json"])
        ct["defects"].append({"code": "0.1.1.a.1", "severity": "minor", "system": "identification",
                              "label": f"Plaque {PLATE} illisible, VIN {VIN} frappé"})
        ct["counts"]["minor"] += 1
        conn.execute("UPDATE documents SET parsed_json = ? WHERE id = ?", (json.dumps(ct), row["id"]))
        conn.execute("UPDATE vehicles SET damages_json = ?",
                     (json.dumps([{"zone": f"Porte {PLATE}", "kind": "Rayure"}, {"zone": "Capot", "kind": "Peinture"}]),))

    snap = _publish_seeded(settings, tmp_path, prepare=plant)
    raw = json.dumps(snap, ensure_ascii=False)
    assert not _keys(snap) & BLOCKED_KEYS
    for secret in (VIN, PLATE, "vin_hmac", "registration", "comments", "photo", '"id"'):
        assert secret not in raw
    labels = [x["label"] for c in snap["top"] + snap["cards"] if c["detail"]["ct"] for x in c["detail"]["ct"]["defects"]]
    assert any("[IMMAT]" in x and "[VIN]" in x for x in labels)
    zones = [x["zone"] for c in snap["top"] + snap["cards"] for x in c["detail"]["damages"] or []]
    assert "Porte [IMMAT]" in zones and "Capot" in zones
    for c in snap["top"] + snap["cards"] + snap["compact"]:
        assert c["url"] is None or c["url"].startswith("https://www.alcopa-auction.fr/")
    assert snap["sale"]["url"] is None or snap["sale"]["url"].startswith("https://www.alcopa-auction.fr/")


def test_ai_text_is_scrubbed_wherever_it_is_published(settings):
    """A verdict or an AI reason carrying a link, a phone or an e-mail (for instance after a
    prompt injection through a seller comment) is scrubbed in the verdict, the v1 reasons
    and the sheet's reasons alike."""
    from auction_agent.scoring.service import rescore_all
    from tests.test_service_report import NOW as SEED_NOW, seed
    verdict = "Voir www.exemple-arnaque.fr ou appeler le 06 12 34 56 78."
    reason = {"code": "llm", "pillar": "llm", "delta": 0.0, "text": "Avis IA : contacter jean.dupont@example.com",
              "evidence": {"src": "llm", "ref": "1"}}
    with db.open_db(settings.db_path) as conn:
        sale_id = seed(conn)
        rescore_all(conn, settings, now=SEED_NOW, use_llm=False)
        with db.write_tx(conn):
            for vid, reasons_json in conn.execute("SELECT vehicle_id, reasons_json FROM assessments").fetchall():
                conn.execute("UPDATE assessments SET verdict = ?, reasons_json = ? WHERE vehicle_id = ?",
                             (verdict, json.dumps([reason] + json.loads(reasons_json)), vid))
        snap = build_snapshot(conn, settings, sale_id=sale_id, now=SEED_NOW)
    cars = snap["top"] + snap["cards"]
    assert cars
    raw = json.dumps(snap, ensure_ascii=False)
    for leaked in ("exemple-arnaque", "06 12 34", "jean.dupont", "@"):
        assert leaked not in raw, leaked
    for c in cars:
        assert c["verdict"] == "Voir [LIEN] ou appeler le [TEL]."
        assert "Avis IA : contacter [EMAIL]" in {r["text"] for r in c["reasons"]}
        assert "Avis IA : contacter [EMAIL]" in {r["text"] for r in c["detail"]["reasons"]}


def _synthetic_card():
    label = "Défaillance " + "x" * 300
    defects = [{"code": f"{i % 9}.1.1.a.1", "severity": ("minor", "major", "critical")[i % 3],
                "system": "structure", "label": label} for i in range(45)]
    reasons = [{"text": f"Raison {i} " + "y" * 80, "src": "annonce", "pillar": "C", "delta": -1.0 * i, "code": f"r{i}"}
               for i in range(7)]
    return {
        "id": 1, "title": "PEUGEOT 308", "trim": "308 1.2 PURETECH", "year": 2019, "mileage": 120_000,
        "fuel": "Essence", "gearbox": "Manuelle", "tier": "yellow", "tier_label": "Profil correct", "score": 70,
        "pillars": {"Fiabilité": 80, "État": 60, "Prix": 50}, "confidence": "partial", "flags": ["value_unknown"],
        "effective_price": 9_999.0, "starting_price": 8_000.0, "sale_date": "09/11/2026", "lot": 751,
        "url": "https://www.alcopa-auction.fr/voiture-occasion/peugeot/308-1234567", "ct": "CT favorable",
        "ct_counts": {"minor": 15, "major": 15, "critical": 15}, "reasons": reasons[:3], "all_reasons": reasons,
        "gates": [], "verdict": "v" * 300, "subtitle": "1.2 PURETECH", "base_score": 72, "llm_adjustment": 0,
        "value": 50, "value_basis": "value_unknown:few_comparables",
        "detail_raw": {"price": {"start": 8000, "vat": 0, "fees": 1152, "repairs": 847, "ct_redo": 0, "total": 9999},
                       "ct_doc": "parsed", "damages": [{"zone": "Capot", "kind": "Peinture"}] * 3, "battery": None},
        "ct_raw": {"result": "DEFAVORABLE_CRITIQUE", "nature": "periodique", "inspection_date": "2026-10-01",
                   "inspection_km": 120_000, "validity_date": None, "odometer_inconsistent": False,
                   "defects": defects},
        "ct_result": "DEFAVORABLE_CRITIQUE", "ct_line": "CT défavorable, 15 défaillances critiques",
    }


def test_snapshot_size_budget(settings, tmp_path, monkeypatch):
    from auction_agent import snapshot as snapshot_module
    card = snapshot_module._public_card(_synthetic_card())
    assert len(snapshot_module.encode(card)) < 16_000  # real cards: 2.2 KB on average, 3.7 KB at most
    ct = card["detail"]["ct"]
    assert len(ct["defects"]) == snapshot_module.DEFECT_LIMIT == 40 and ct["more"] == 5
    assert [d["severity"] for d in ct["defects"][:15]] == ["critical"] * 15
    assert all(len(d["label"]) <= 200 for d in ct["defects"])
    assert card["detail"]["damages"] == [{"zone": "Capot", "kind": "Peinture", "n": 3}]
    assert len(snapshot_module.encode(snapshot_module._compact(_synthetic_card()))) < 500
    # Over the guard, the one-line list goes and the count says so.
    from auction_agent.scoring.service import rescore_all
    from tests.test_service_report import NOW as SEED_NOW, seed
    s = settings.replace(top_n=2)
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        rescore_all(conn, s, now=SEED_NOW, use_llm=False)
        full = build_snapshot(conn, s, sale_id=sale_id, now=SEED_NOW, public_limit=0)
        monkeypatch.setattr(snapshot_module, "MAX_SNAPSHOT_BYTES", 2_000)
        small = build_snapshot(conn, s, sale_id=sale_id, now=SEED_NOW, public_limit=0)
    assert len(full["compact"]) == 6 and "compact_dropped" not in full["counts"]
    assert small["compact"] == [] and small["counts"]["compact_dropped"] == 6
    assert small["counts"]["compact"] == {"green": 0, "yellow": 0, "red": 0}
    assert len(small["top"]) == 2  # the sheets stay


def test_cap_text_names_the_binding_gate_or_the_missing_data():
    from auction_agent import snapshot as snapshot_module
    card = _synthetic_card()
    card["score"], card["base_score"] = 79, 84
    assert snapshot_module._score(card)["cap"] == "Plafonnée à 79 tant que les données sont incomplètes"
    card["all_reasons"] = card["all_reasons"] + [{"text": "Kilométrage non garanti", "src": "commentaires",
                                                  "pillar": "gate", "delta": -5.0, "code": "mileage_not_guaranteed"}]
    score = snapshot_module._score(card)
    assert score["cap"] == "Plafonnée à 79 : Kilométrage non garanti" and score["cap_gate"] == "Kilométrage non garanti"
    card["score"] = card["base_score"] = 70
    assert snapshot_module._score(card)["cap"] is None


@pytest.mark.parametrize("basis, value, text", [
    ("model_y1_band:n=7", 62, "12 % sous la médiane de 7 voitures comparables"),
    ("brand_fuel_y2:n=9", 42, "8 % au-dessus de la médiane de 9 voitures comparables"),
    ("model_y2_band1:n=5", 50, "au niveau de la médiane de 5 voitures comparables"),
    ("value_unknown:few_comparables", 50, "pas encore comparé : trop peu de voitures comparables"),
    ("value_unknown:price_unknown", 50, "pas encore comparé : mise à prix pas encore publiée"),
])
def test_value_text(basis, value, text):
    from auction_agent.snapshot import _value
    assert _value({"value_basis": basis, "value": value}) == {"known": not basis.startswith("value_unknown"), "text": text}
