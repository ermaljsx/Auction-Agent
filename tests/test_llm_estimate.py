"""`llm-estimate`: real payloads from the database, priced offline; no client, no network, no writes."""
import sys

import pytest

from auction_agent import cli, db
from auction_agent.scoring import estimate as estimate_mod
from auction_agent.scoring.estimate import estimate_llm_cost, format_estimate, resolve_sale_id
from auction_agent.scoring.llm import payload_hash
from auction_agent.scoring.service import llm_payload, plan_reviews
from tests.test_scoring import TODAY
from tests.test_service_report import NOW, seed


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """The CLI on a temporary database at the seed's date, with every way to reach an API cut."""
    monkeypatch.setenv("AUCTION_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(estimate_mod, "utc_now", lambda: NOW)

    def no_client(*_a, **_kw):
        raise AssertionError("llm-estimate must never build an LLM client")

    monkeypatch.setattr("auction_agent.scoring.service.make_client", no_client)
    monkeypatch.setattr("auction_agent.scoring.llm.make_client", no_client)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.setitem(sys.modules, "openai", None)
    return tmp_path / "data" / "auction.db"


def _counts(conn):
    return tuple(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("assessments", "llm_assessments"))


def test_estimate_prints_every_priced_model_and_writes_nothing(offline, capsys):
    with db.open_db(offline) as conn:
        seed(conn)
        before = _counts(conn)
    assert cli.main(["llm-estimate"]) == 0
    out = capsys.readouterr().out
    for text in ("claude-haiku-5-5", "gpt-6-luna", "Per run", "Per week (x2)", "Per month", "Batch API",
                 "no API call", "8 would be reviewed", "8 to send this run in 1 batch(es)"):
        assert text in out, text
    with db.open_db(offline) as conn:
        assert _counts(conn) == before


def test_all_prices_the_whole_sale(offline, capsys):
    with db.open_db(offline) as conn:
        seed(conn)
        with db.write_tx(conn):  # two broken engines: red, so neither listed nor reviewed
            conn.execute("UPDATE vehicles SET comments = 'Moteur HS' WHERE id IN (1, 2)")
    assert cli.main(["llm-estimate", "--runs-per-week", "3"]) == 0
    listed = capsys.readouterr().out
    assert "6 would be reviewed" in listed and "Per week (x3)" in listed
    assert cli.main(["llm-estimate", "--all"]) == 0
    whole = capsys.readouterr().out
    assert "every active car of the sale, 8" in whole and "8 to send" in whole


def test_no_sale_prints_a_message(offline, capsys):
    with db.open_db(offline):
        pass
    assert cli.main(["llm-estimate"]) == 3
    assert "No sale in the database" in capsys.readouterr().out
    assert cli.main(["llm-estimate", "--sale-id", "424242"]) == 3


def test_sale_id_accepts_the_house_number(settings):
    with db.open_db(settings.db_path) as conn:
        sale_id = seed(conn)
        assert resolve_sale_id(conn, None) == sale_id
        assert resolve_sale_id(conn, sale_id) == sale_id
        assert resolve_sale_id(conn, "S1") == sale_id  # the seed's external id
        assert resolve_sale_id(conn, 999) is None


def test_cached_cars_and_measured_answers_are_used(settings):
    s = settings.replace(llm_model="claude-haiku-5-5")
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        plan = plan_reviews(conn, s, now_dt=NOW, today=TODAY, model_name=s.llm_model, sale_id=sale_id)
        with db.write_tx(conn):
            for part, kind, _payload, h in plan.triggered[:6]:
                conn.execute(
                    """INSERT INTO llm_assessments (vehicle_id, kind, llm_input_hash, prompt_version, llm_model,
                           adjustment, verdict, status, tokens_in, tokens_out, created_at)
                       VALUES (?, ?, ?, 'p3-2026.11', 'claude-haiku-5-5', 0, 'ok', 'ok', 100, 900, ?)""",
                    (part.facts.vehicle_id, kind, h, "2026-10-13T08:00:00Z"))
        est = estimate_llm_cost(conn, s, sale_id=sale_id, now=NOW)
    assert est["cached"] == 6 and est["to_send"] == 2 and est["measured_output"] == 900
    haiku = next(m for m in est["models"] if m["model"] == "claude-haiku-5-5")
    luna = next(m for m in est["models"] if m["model"] == "gpt-6-luna")
    assert haiku["measured"] and not luna["measured"] and luna["effort"] == "none" and haiku["effort"] == "low"
    assert 0 < haiku["run_typical"] < haiku["run_worst"] and haiku["month_worst"] > haiku["week_worst"]
    assert haiku["batch_month_worst"] == pytest.approx(haiku["month_worst"] / 2)
    assert any("measured on 6 stored reviews" in line for line in format_estimate(est))


def test_payloads_and_hashes_match_a_scoring_run(settings):
    with db.open_db(settings.db_path) as conn:
        sale_id = seed(conn)
        plan = plan_reviews(conn, settings, now_dt=NOW, today=TODAY, model_name=settings.llm_model, sale_id=sale_id)
    for part, _kind, payload, h in plan.triggered:
        assert payload == llm_payload(part) and h == payload_hash(payload, settings.llm_model)


def test_a_sale_that_is_over_sends_nothing_but_all_still_prices_it(settings):
    later = NOW.replace(month=12)
    with db.open_db(settings.db_path) as conn:
        sale_id = seed(conn)
        now_est = estimate_llm_cost(conn, settings, sale_id=sale_id, now=later)
        whole = estimate_llm_cost(conn, settings, sale_id=sale_id, now=later, all_cars=True)
    assert now_est["items"] == 0 and any("No car would be sent now" in line for line in format_estimate(now_est))
    assert whole["items"] == 8 and whole["models"][0]["run_typical"] > 0
