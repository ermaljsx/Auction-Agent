"""AI cost: the price table, the rate cards, the cost stored per batch and the run totals."""
import pytest

from auction_agent import db
from auction_agent.scoring.llm import LLMBatchError, ReviewBatch
from auction_agent.scoring.pricing import PRICES, cost_usd, price_for
from auction_agent.scoring.service import rescore_all
from tests.test_scoring import TODAY
from tests.test_service_report import NOW, FakeClient, llm_settings, seed


def test_haiku_standard_rate_card():
    # $0.10 / $0.50 per 1M tokens while the prompt is 100K tokens or fewer.
    assert cost_usd("claude-haiku-5-5", 50_000, 1_000_000) == pytest.approx(0.005 + 0.50)
    # A 1M-token prompt is above the threshold: the whole request is on the $0.50 / $2.50 card.
    assert cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.50 + 2.50)


def test_haiku_long_prompt_rate_card_applies_above_100k_input_tokens():
    assert cost_usd("claude-haiku-5-5", 150_000, 10_000) == pytest.approx(0.075 + 0.025)
    assert cost_usd("claude-haiku-5-5", 100_000, 10_000) == pytest.approx(0.010 + 0.005)  # exactly 100K: standard


def test_gpt_6_luna_and_dated_ids():
    assert cost_usd("gpt-6-luna", 1_000_000, 0) == pytest.approx(0.10)
    assert price_for("gpt-6-luna-2026-09-22") is PRICES["gpt-6-luna"]
    assert price_for("gpt-6-luna-pro") is None  # another model, another price: never guessed
    assert cost_usd("fake-model", 100, 20) is None
    assert cost_usd("claude-haiku-5-5", None, 20) is None
    assert {p.provider for p in PRICES.values()} == {"anthropic", "openai"}
    assert all(p.source for p in PRICES.values())


class HaikuClient(FakeClient):
    model = "claude-haiku-5-5"
    provider = "anthropic"

    def review(self, payloads):
        batch = super().review(payloads)
        return ReviewBatch(batch.reviews, tokens_in=10_000, tokens_out=2_000, model="claude-haiku-5-5")


class HaikuFailsFirst(HaikuClient):
    def review(self, payloads):
        if not self.calls:
            self.calls.append(payloads)
            exc = LLMBatchError("The answer was cut off (max_tokens reached).")
            exc.tokens_in, exc.tokens_out = 5_000, 3_000
            raise exc
        return super().review(payloads)


def test_cost_is_stored_on_the_first_row_of_each_batch_and_summed(settings):
    s = llm_settings(settings).replace(llm_model="claude-haiku-5-5", llm_batch_size=3)
    client = HaikuFailsFirst()
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=client)
        rows = conn.execute("SELECT status, tokens_in, cost_usd FROM llm_assessments ORDER BY id").fetchall()
        stats = db.get_stats(conn)
    per_ok_batch = cost_usd("claude-haiku-5-5", 10_000, 2_000)
    failed_batch = cost_usd("claude-haiku-5-5", 5_000, 3_000)
    priced = [r for r in rows if r["cost_usd"] is not None]
    assert all(r["tokens_in"] is not None for r in priced)  # cost sits with the batch totals, once
    assert sum(1 for r in priced if r["status"] == "failed") == 1
    ok_batches = out["llm_calls"] - 1
    assert out["llm_cost_usd"] == pytest.approx(failed_batch + ok_batches * per_ok_batch)
    assert out["llm_tokens_in"] == 5_000 + ok_batches * 10_000 and out["llm_tokens_out"] == 3_000 + ok_batches * 2_000
    assert out["llm_provider"] == "anthropic" and out["llm_model"] == "claude-haiku-5-5"
    note = next(n for n in out["notes"] if n.startswith("AI review: "))
    assert f"{out['llm_calls']} call(s)" in note and "$" in note and "(claude-haiku-5-5)" in note
    assert stats["llm_cost_usd"] == pytest.approx(round(out["llm_cost_usd"], 4))


class HaikuMangledKeys(HaikuClient):
    """A billed, valid answer whose keys match no car ("car 3" instead of "3")."""

    def review(self, payloads):
        self.calls.append(payloads)
        return ReviewBatch({}, tokens_in=6_000, tokens_out=3_000, model="claude-haiku-5-5")


def test_a_billed_batch_with_no_usable_review_still_stores_its_cost(settings):
    s = llm_settings(settings).replace(llm_model="claude-haiku-5-5", llm_batch_size=3)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=HaikuMangledKeys())
        rows = conn.execute("SELECT status, tokens_in, tokens_out, cost_usd FROM llm_assessments").fetchall()
        stats = db.get_stats(conn)
    assert out["llm_calls"] >= 2 and all(r["status"] == "failed" for r in rows)
    assert out["llm_cost_usd"] == pytest.approx(out["llm_calls"] * cost_usd("claude-haiku-5-5", 6_000, 3_000))
    assert sum(r["tokens_in"] or 0 for r in rows) == out["llm_tokens_in"] == out["llm_calls"] * 6_000
    assert sum(r["tokens_out"] or 0 for r in rows) == out["llm_tokens_out"]
    assert sum(r["cost_usd"] or 0 for r in rows) == pytest.approx(out["llm_cost_usd"])
    assert sum(1 for r in rows if r["cost_usd"] is not None) == out["llm_calls"]  # once per batch
    assert stats["llm_cost_usd"] == pytest.approx(round(out["llm_cost_usd"], 4))


def test_unknown_price_is_reported_not_guessed(settings):
    s = llm_settings(settings)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=FakeClient())
        assert conn.execute("SELECT count(*) FROM llm_assessments WHERE cost_usd IS NOT NULL").fetchone()[0] == 0
    assert out["llm_cost_usd"] is None
    assert any("no price known for model fake-model: cost not computed" in n for n in out["notes"])


def test_no_call_costs_nothing(settings):
    with db.open_db(settings.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        assert db.get_stats(conn)["llm_cost_usd"] == 0
    assert out["llm_calls"] == 0 and out["llm_cost_usd"] == 0 and not any(n.startswith("AI review") for n in out["notes"])


def test_stats_command_shows_the_total_cost(settings, monkeypatch, tmp_path, capsys):
    from auction_agent import cli
    monkeypatch.setenv("AUCTION_AGENT_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["stats"]) == 0
    assert "llm_cost_usd" in capsys.readouterr().out
