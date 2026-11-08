"""Every car the site lists gets an AI verdict: the shared selection rule, the review plan,
its priority and margin, and the batch splitting."""
import pytest

from auction_agent import db
from auction_agent.report import build_report_context, pick_rows
from auction_agent.scoring import service
from auction_agent.scoring.service import _chunks, plan_reviews, rescore_all, site_picks
from auction_agent.snapshot import PUBLIC_CARD_LIMIT, build_snapshot
from tests.test_scoring import TODAY
from tests.test_service_report import NOW, FakeClient, llm_settings, seed


def row(score, tier="yellow", confidence="full", price=10_000.0):
    return {"tier": tier, "confidence": confidence, "final_score": score, "effective_price": price}


# ------------------------------------------------------------------ site_picks

def test_public_card_limit_lives_in_the_service_and_stays_importable_from_snapshot():
    assert PUBLIC_CARD_LIMIT == service.PUBLIC_CARD_LIMIT == 40


def test_red_cars_are_never_listed_and_rest_excludes_the_top():
    rows = [(1, row(90)), (2, row(95, tier="red")), (3, row(80)), (4, row(70)), (5, row(60))]
    picks = site_picks(rows, upcoming={1, 2, 3, 4, 5}, top_n=2)
    assert picks.top == [1, 3] and picks.rest == [4, 5] and not picks.provisional
    assert picks.listed == [1, 3, 4, 5]


def test_top_needs_complete_data_and_a_sale_this_week():
    rows = [(1, row(90, confidence="partial")), (2, row(85)), (3, row(80))]
    picks = site_picks(rows, upcoming={1, 3}, top_n=5)
    assert picks.top == [3] and picks.rest == [1, 2]  # 2 is not this week: listed, not a top pick


def test_provisional_top_when_nothing_is_complete():
    rows = [(1, row(70, confidence="partial")), (2, row(90, confidence="partial")), (3, row(99))]
    picks = site_picks(rows, upcoming={1, 2}, top_n=1)
    assert picks.top == [2] and picks.provisional and picks.rest == [3, 1]


def test_limit_is_respected_and_ties_follow_pick_key_then_id():
    rows = [(7, row(80, price=None)), (5, row(80, price=9_000.0)), (6, row(80, price=9_000.0)), (4, row(50))]
    picks = site_picks(rows, upcoming=set(), top_n=3, limit=3)
    assert picks.top == [] and picks.rest == [5, 6, 7]  # cheaper first, unknown price last, then id


# ------------------------------------------------------------------ every listed car is reviewed

@pytest.fixture
def three_listed(monkeypatch):
    """8 similar cars, top 2 + 3 more listed, no green car (so only the list triggers verdicts)."""
    monkeypatch.setattr(service, "PUBLIC_CARD_LIMIT", 3)


def _s(settings, **kw):
    return llm_settings(settings).replace(top_n=2, green_min=99, yellow_min=40, **kw)


def test_every_listed_car_gets_a_verdict_and_unlisted_cars_are_not_sent(settings, three_listed):
    s = _s(settings)
    client = FakeClient()
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        plan = plan_reviews(conn, s, now_dt=NOW, today=TODAY, model_name=client.model, sale_id=sale_id)
        listed = plan.picks.listed
        assert len(plan.picks.top) == 2 and len(listed) == 5 and plan.listed_count == 5
        assert all(kind == "verdict" for _, kind, _, _ in plan.triggered)
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=client, sale_id=sale_id)
        sent = [int(p["key"]) for batch in client.calls for p in batch]
        assert sent == listed and out["llm_triggered"] == 5 and out["llm_listed"] == 5
        snap = build_snapshot(conn, s, sale_id=sale_id, now=NOW, public_limit=3)
        shown = snap["top"] + snap["cards"]
        assert len(shown) == 5 and all(c["verdict"] for c in shown)
        assert snap["llm"]["coverage"] == {"listed": 5, "with_verdict": 5}
        reviewed = {r[0] for r in conn.execute("SELECT vehicle_id FROM llm_assessments WHERE status = 'ok'")}
        assert reviewed == set(listed)


def _reverse_page_order(conn):
    """The newest car becomes the cheapest (best scored): page order is no longer id order."""
    with db.write_tx(conn):
        conn.execute("UPDATE listings SET starting_price = 20000 - 1000 * vehicle_id")


def test_the_budget_goes_to_the_top_picks_first(settings, three_listed):
    s = _s(settings)
    client = FakeClient()
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        _reverse_page_order(conn)
        plan = plan_reviews(conn, s, now_dt=NOW, today=TODAY, model_name=client.model, sale_id=sale_id)
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=client, sale_id=sale_id, llm_limit=2)
    assert plan.picks.top != sorted(plan.pool)[: len(plan.picks.top)], "page order must differ from id order"
    assert [int(p["key"]) for batch in client.calls for p in batch] == plan.picks.top
    assert any("budget" in n for n in out["notes"])


def test_priority_follows_page_order_when_it_differs_from_id_order(settings):
    s = llm_settings(settings).replace(top_n=1)
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        _reverse_page_order(conn)
        plan = plan_reviews(conn, s, now_dt=NOW, today=TODAY, model_name="m", sale_id=sale_id)
    order = [t[0].facts.vehicle_id for t in plan.triggered]
    listed = [v for v in plan.picks.listed if v in set(order)]
    assert listed != sorted(listed), "page order must differ from id order"
    assert order[: len(listed)] == listed


def test_the_list_margin_grows_with_the_adjust_candidates(settings, three_listed):
    s = llm_settings(settings).replace(top_n=2)
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        base = plan_reviews(conn, s, now_dt=NOW, today=TODAY, model_name="m", sale_id=sale_id)
        best = max(r["base_score"] for r in base.results.values())
        # Put the green boundary just above the best base score: the cars near it are adjust candidates.
        near = s.replace(green_min=min(100, int(best) + 3), yellow_min=40)
        plan = plan_reviews(conn, near, now_dt=NOW, today=TODAY, model_name="m", sale_id=sale_id)
    adjust = [t for t in plan.triggered if t[1] == "adjust"]
    assert adjust, "the seed must produce adjust candidates"
    assert len(plan.picks.rest) == min(8 - len(plan.picks.top), 3 + len(adjust))
    assert plan.listed_count == len(plan.picks.top) + min(len(plan.picks.rest), 3)


def test_a_top_pick_the_ai_lowers_is_replaced_by_a_reviewed_car(settings, monkeypatch):
    """The next complete car replaces a dropped top pick even when incomplete cars fill the
    rest of the list before it: it must have a verdict too."""
    monkeypatch.setattr(service, "PUBLIC_CARD_LIMIT", 1)
    s = llm_settings(settings).replace(top_n=1, yellow_min=30)
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        with db.write_tx(conn):
            # Cars 2 and 3 are incomplete (capped below green) but outrank cars 4-8, which are
            # complete with a few points less (minor comment penalties).
            conn.execute("UPDATE vehicles SET comments = 'Kilométrage non garanti' WHERE id IN (2, 3)")
            conn.execute("UPDATE vehicles SET comments = 'Bruit, fumée, voyant' WHERE id > 3")
        base = plan_reviews(conn, s, now_dt=NOW, today=TODAY, model_name="m", sale_id=sale_id).results
        # Car 1 is the only adjust candidate: the green boundary is just above its base score.
        s = s.replace(green_min=base[1]["base_score"] + 3)
        assert all(base[v]["base_score"] <= s.green_min - 6 for v in range(4, 9))
        mileage = {r[0]: r[1] for r in conn.execute("SELECT id, mileage_km FROM vehicles")}
        plan = plan_reviews(conn, s, now_dt=NOW, today=TODAY, model_name="m", sale_id=sale_id)
        assert [t[0].facts.vehicle_id for t in plan.triggered if t[1] == "adjust"] == [1]
        assert plan.picks.top == [1] and plan.picks.rest == [2, 3]  # car 4 sits beyond the list
        assert 4 in {t[0].facts.vehicle_id for t in plan.triggered if t[1] == "verdict"}
        rescore_all(conn, s, today=TODAY, now=NOW, client=FakeClient(adjustment=-15), sale_id=sale_id)
        snap = build_snapshot(conn, s, sale_id=sale_id, now=NOW, public_limit=1)
    assert [c["mileage"] for c in snap["top"]] == [mileage[4]]  # car 1 dropped out, car 4 replaced it
    assert snap["llm"]["coverage"]["listed"] == snap["llm"]["coverage"]["with_verdict"] == 2


def test_priority_is_page_order_then_adjust_then_green(settings):
    s = llm_settings(settings).replace(top_n=1)
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        plan = plan_reviews(conn, s, now_dt=NOW, today=TODAY, model_name="m", sale_id=sale_id)
    order = [t[0].facts.vehicle_id for t in plan.triggered]
    listed = [v for v in plan.picks.listed if v in set(order)]
    assert order[: len(listed)] == listed


def test_cars_of_another_sale_are_not_listed_for_this_one(settings):
    s = llm_settings(settings).replace(green_min=99, yellow_min=40)
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        plan = plan_reviews(conn, s, now_dt=NOW, today=TODAY, model_name="m", sale_id=sale_id + 1000)
    assert plan.pool == set() and plan.picks.listed == [] and plan.triggered == []


def test_snapshot_report_and_review_plan_list_the_same_cars(settings):
    s = llm_settings(settings).replace(top_n=3)
    with db.open_db(s.db_path) as conn:
        sale_id = seed(conn)
        rescore_all(conn, s, today=TODAY, now=NOW, client=FakeClient(adjustment=0), sale_id=sale_id)
        plan = plan_reviews(conn, s, now_dt=NOW, today=TODAY, model_name="m", sale_id=sale_id)
        ctx = build_report_context(conn, s, sale_id=sale_id, now=NOW)
        snap = build_snapshot(conn, s, sale_id=sale_id, now=NOW)
        mileage = {r[0]: r[1] for r in conn.execute("SELECT id, mileage_km FROM vehicles")}
    assert ctx["picks"] == site_picks(pick_rows(ctx["cards"]), ctx["upcoming"], s.top_n)
    assert ctx["picks"].top == plan.picks.top and ctx["picks"].rest == plan.picks.rest[:PUBLIC_CARD_LIMIT]
    assert [c["mileage"] for c in snap["top"]] == [mileage[v] for v in plan.picks.top]
    assert [c["mileage"] for c in snap["cards"]] == [mileage[v] for v in ctx["picks"].rest]


# ------------------------------------------------------------------ batches

def test_batches_split_at_the_car_count_or_the_character_budget():
    items = [(None, "verdict", {"key": str(i), "comments": "x" * 90}, "h") for i in range(7)]
    assert [len(c) for c in _chunks(items, 3, max_chars=10_000)] == [3, 3, 1]
    # About 115 characters per payload: a 250-character budget fits two.
    assert [len(c) for c in _chunks(items, 20, max_chars=250)] == [2, 2, 2, 1]
    # A single payload above the budget still goes, alone.
    assert [len(c) for c in _chunks(items[:2], 20, max_chars=10)] == [1, 1]
    assert _chunks([], 20) == []
