import json
import subprocess
import sys
from datetime import date, datetime, timezone


from auction_agent import cli, db
from auction_agent.adapters.base import LotStub, SaleRef
from auction_agent.ct_parser import parse_controle_technique
from auction_agent.report import generate_report
from auction_agent.scoring.llm import LLMConfigError, Review, ReviewBatch, parse_reviews
from auction_agent.scoring.service import rescore_all
from tests.conftest import PROJECT_ROOT, read_fixture
from tests.test_ingest import SALE1, make_http

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
NOW_S = "2026-10-13T08:00:00Z"
TODAY = date(2026, 10, 13)


class FakeClient:
    model = "fake-model"

    def __init__(self, adjustment=-3, error=None):
        self.calls, self.adjustment, self.error = [], adjustment, error

    def review(self, payloads):
        self.calls.append(payloads)
        if self.error:
            raise self.error
        return ReviewBatch({p["key"]: Review(p["key"], self.adjustment, ["Point de vigilance"], "Bon état général.")
                            for p in payloads}, tokens_in=100, tokens_out=20)


def fresh_ct():
    text = read_fixture("ct", "favorable_with_legend.txt").replace("12/03/2026", "01/10/2026")
    return parse_controle_technique(text)


def seed(conn, n=8, base_price=10_000.0, comments=None):
    """n similar Toyota Yaris in one upcoming online sale, each with a fresh favourable CT."""
    sale = SaleRef(house="alcopa", external_id="S1", url="https://www.alcopa-auction.fr/vente-encheres-en-ligne/1",
                   kind="online", starts_at=datetime(2026, 10, 16, 14, tzinfo=timezone.utc),
                   ends_at=datetime(2026, 10, 17, 18, tzinfo=timezone.utc))
    stubs = [LotStub(house="alcopa", external_id=str(100000 + i), category="voiture",
                     detail_url=f"https://www.alcopa-auction.fr/voiture-occasion/toyota/yaris-{100000 + i}",
                     brand="TOYOTA", model="YARIS", trim="YARIS 1.5 HYBRIDE 116H", fuel="hybrid", year=2019,
                     mileage_km=60_000 + 1_000 * i, gearbox="automatic", starting_price=base_price + 400 * i)
             for i in range(n)]
    ct = fresh_ct()
    with db.write_tx(conn):
        sale_id = db.upsert_sale(conn, sale, NOW_S)
        db.apply_snapshot(conn, "alcopa", sale_id, stubs, NOW_S)
        for (vid,) in conn.execute("SELECT id FROM vehicles"):
            db.update_vehicle_detail(conn, vid, {"first_registration": "2019-03-15", "comments": comments})
            db.mark_complete(conn, vid, ct_state="present")
            db.upsert_document(conn, vid, {"doc_type": "ct", "source_url": "u", "fetch_status": "ok",
                                           "parse_status": "parsed", "parsed": ct}, NOW_S)
    return sale_id


def llm_settings(settings):
    return settings.replace(anthropic_api_key="k", llm_model="fake-model")


def test_every_vehicle_gets_a_tier(settings):
    with db.open_db(settings.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        assert out["scored"] == 8 and out["green"] + out["yellow"] + out["red"] == 8
        rows = conn.execute("SELECT confidence, value_basis FROM assessments").fetchall()
        assert all(r["confidence"] == "full" and r["value_basis"].startswith("model") for r in rows)


def test_llm_skipped_without_api_key(settings):
    with db.open_db(settings.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, settings, today=TODAY, now=NOW)
    assert out["llm_calls"] == 0 and out["llm_provider"] == "anthropic"
    assert any("ANTHROPIC_API_KEY" in n and ".env.example" in n for n in out["notes"])
    assert not any("OPENAI_API_KEY" in n for n in out["notes"])


def test_llm_skipped_without_the_openai_key_when_openai_is_chosen(settings):
    s = settings.replace(llm_provider="openai", llm_model="gpt-6-luna", llm_reasoning_effort="none",
                         anthropic_api_key="sk-ant-x")
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW)
    note = next(n for n in out["notes"] if "LLM review skipped" in n)
    assert "OPENAI_API_KEY" in note and "remove LLM_PROVIDER=openai" in note and "sk-ant-x" not in note


def test_a_leftover_openai_key_gets_a_switch_hint(settings):
    s = settings.replace(openai_api_key="sk-old")
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW)
    note = next(n for n in out["notes"] if "LLM review skipped" in n)
    assert "ANTHROPIC_API_KEY" in note and "LLM_PROVIDER=openai" in note and "sk-old" not in note


def test_llm_results_are_cached_and_price_changes_cost_nothing(settings):
    s = llm_settings(settings)
    client = FakeClient()
    with db.open_db(s.db_path) as conn:
        seed(conn)
        first = rescore_all(conn, s, today=TODAY, now=NOW, client=client)
        assert first["llm_calls"] >= 1 and first["llm_triggered"] > 0
        sent = [p for batch in client.calls for p in batch]
        assert all("price" not in json.dumps(p) and "detail_url" not in p for p in sent)
        assert all("VIN" not in json.dumps(p) for p in sent)
        calls_after_first = len(client.calls)
        rescore_all(conn, s, today=TODAY, now=NOW, client=client)
        assert len(client.calls) == calls_after_first  # unchanged payloads: cache hit
        with db.write_tx(conn):
            conn.execute("UPDATE listings SET starting_price = starting_price - 500")
        rescore_all(conn, s, today=TODAY, now=NOW, client=client)
        assert len(client.calls) == calls_after_first  # price is not part of the payload
        verdicts = [r[0] for r in conn.execute("SELECT verdict FROM assessments WHERE verdict IS NOT NULL")]
        assert verdicts and verdicts[0] == "Bon état général."


def test_llm_config_error_falls_back_to_heuristics(settings):
    s = llm_settings(settings)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=FakeClient(error=LLMConfigError("bad model")))
        assert out["scored"] == 8 and "bad model" in out["notes"]
        assert conn.execute("SELECT count(*) FROM assessments").fetchone()[0] == 8


def test_llm_budget_is_respected(settings):
    s = llm_settings(settings).replace(llm_batch_size=1)
    client = FakeClient()
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=client, llm_limit=1)
    assert len(client.calls) == 1
    assert any("budget" in n for n in out["notes"]) or out["llm_triggered"] == 1


def test_parse_reviews_validates_output():
    raw = json.dumps({"items": [
        {"key": "1", "adjustment": -40, "reasons": ["a", "b", "c", "d"], "verdict": "ok"},
        {"key": "999", "adjustment": 3, "reasons": [], "verdict": "intrus"},
    ]})
    out = parse_reviews(raw, {"1", "2"})
    assert list(out) == ["1"] and out["1"].adjustment == -15 and len(out["1"].reasons) == 3


def test_scoring_imports_without_llm_sdks():
    code = ("import sys; sys.modules['openai'] = None; sys.modules['anthropic'] = None; "
            "sys.modules['google'] = None; "
            "import auction_agent.scoring.service, auction_agent.cli, auction_agent.scoring.llm_anthropic, "
            "auction_agent.scoring.llm_openai, auction_agent.scoring.estimate; print('ok')")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", cwd=PROJECT_ROOT)
    assert result.stdout.strip() == "ok", result.stderr


def test_report_escapes_untrusted_text(settings):
    with db.open_db(settings.db_path) as conn:
        seed(conn)
        with db.write_tx(conn):
            conn.execute("UPDATE vehicles SET trim = '<script>alert(1)</script>' WHERE id = 1")
        rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        path = generate_report(conn, settings, now=NOW)
    html = path.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "Risque élevé selon nos critères" in html or "Profil" in html


def test_report_handles_missing_values_and_price_filter(settings):
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=2)
        with db.write_tx(conn):
            conn.execute("UPDATE listings SET starting_price = NULL")
            conn.execute("UPDATE vehicles SET mileage_km = NULL, year = NULL")
        rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        path = generate_report(conn, settings, max_price=5000, now=NOW)
    html = path.read_text(encoding="utf-8")
    assert "prix inconnu" in html and "Données incomplètes" in html


def test_cli_end_to_end(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("AUCTION_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUCTION_AGENT_REPORTS_DIR", str(tmp_path / "reports"))
    http = make_http()
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: http)
    assert cli.main(["crawl", "--url", SALE1]) == 0
    assert cli.main(["advise", "--no-llm"]) == 0
    reports = list((tmp_path / "reports").glob("recommendation_*.html"))
    assert len(reports) == 1
    html = reports[0].read_text(encoding="utf-8")
    assert "PEUGEOT 208" in html and "Voir l'annonce" in html
    with db.open_db(tmp_path / "data" / "auction.db") as conn:
        assert conn.execute("SELECT count(*) FROM assessments").fetchone()[0] == 3
    assert cli.main(["stats"]) == 0


# ------------------------------------------------------------------ site redesign (snapshot v2)

import pytest  # noqa: E402

from auction_agent.report import ct_line, short_trim  # noqa: E402


@pytest.mark.parametrize("model, trim, expected", [
    ("DS7 CROSSBACK", "DS7 CROSSBACK PURETECH 180 EAT8 GRAND CHIC", "PURETECH 180 EAT8 GRAND CHIC"),
    ("GRAND SCÉNIC IV", "GRAND SCENIC IV DCI 110 ENERGY BUSINESS 7 PL", "DCI 110 ENERGY BUSINESS 7 PL"),
    ("GRAND SCENIC IV BUSINESS", "GRAND SCÉNIC IV DCI 110 ENERGY BUSINESS", "DCI 110 ENERGY BUSINESS"),
    ("3008 BUSINESS", "3008 BLUEHDI 130CH S&S EAT8 ALLURE BUSINESS", "BLUEHDI 130CH S&S EAT8 ALLURE BUSINESS"),
    ("GRAND SCENIC IV", "GRAND SCENIC IV - TCE 140", "TCE 140"),
    ("SERIE 1 F20 LCI", "116D EFFICIENTDYNAMICS EDITION", "116D EFFICIENTDYNAMICS EDITION"),
    ("YARIS", "YARIS", None),
    ("YARIS", "  ", None),
    ("YARIS", None, None),
    (None, "CLIO 1.5 DCI", "CLIO 1.5 DCI"),
])
def test_short_trim(model, trim, expected):
    assert short_trim(model, trim) == expected


def _reason(code):
    return {"code": code}


@pytest.mark.parametrize("ct, codes, expected", [
    ({"result": "FAVORABLE", "counts": {"minor": 0}}, ["ct_favorable"], "CT favorable, sans défaillance"),
    ({"result": "FAVORABLE", "counts": {"minor": 1}}, ["ct_favorable"], "CT favorable, 1 défaillance mineure"),
    ({"result": "FAVORABLE", "counts": {"minor": 3}}, ["ct_favorable"], "CT favorable, 3 défaillances mineures"),
    ({"result": "DEFAVORABLE_MAJEURE", "counts": {"major": 2}}, ["ct_major"], "CT défavorable, 2 défaillances majeures"),
    ({"result": "DEFAVORABLE_CRITIQUE", "counts": {"critical": 1}}, ["ct_critique"], "CT défavorable, 1 défaillance critique"),
    ({"result": "UNKNOWN", "counts": {}}, ["ct_non_lu"], "CT illisible"),
    ({"result": "FAVORABLE", "nature": "volontaire", "counts": {}}, ["ct_non_lu"], "CT volontaire, pas retenu pour la note"),
    (None, ["ct_non_lu"], "CT illisible"),
    (None, ["ct_not_required"], "CT pas encore exigé (moins de 4 ans)"),
    (None, ["ct_absent"], "Pas de CT publié"),
])
def test_ct_line(ct, codes, expected):
    assert ct_line(ct, [_reason(c) for c in codes]) == expected


def test_report_reasons_no_longer_repeat_the_gates(settings):
    from auction_agent.report import build_report_context
    with db.open_db(settings.db_path) as conn:
        seed(conn, comments="Kilométrage non garanti")
        rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        ctx = build_report_context(conn, settings, now=NOW)
    for card in ctx["cards"]:
        gates = {g["text"] for g in card["gates"]}
        assert "Kilométrage non garanti" in gates
        assert not gates & {r["text"] for r in card["reasons"]}
        assert gates <= {r["text"] for r in card["all_reasons"] if r["pillar"] == "gate"}
        assert card["subtitle"] == "1.5 HYBRIDE 116H" and card["ct_line"].startswith("CT favorable")
        assert card["detail_raw"]["ct_doc"] == "parsed" and card["ct_raw"]["result"] == "FAVORABLE"


def test_a_binding_gate_is_not_repeated_in_the_top_reasons(settings):
    """A gate that caps the score has the largest negative delta: it would lead the reasons
    (HTML report, v1 snapshot) if the filter did not leave gates out."""
    from auction_agent.report import build_report_context
    with db.open_db(settings.db_path) as conn:
        seed(conn, comments="Moteur HS")
        rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        ctx = build_report_context(conn, settings, now=NOW)
    binding = [r for c in ctx["cards"] for r in c["all_reasons"] if r["pillar"] == "gate" and r["delta"] < 0]
    assert binding, "the seed must produce a binding gate"
    for card in ctx["cards"]:
        gates = {g["text"] for g in card["gates"]}
        assert "Mention « moteur hs » dans les commentaires" in gates
        assert not gates & {r["text"] for r in card["reasons"]}, card["reasons"]
