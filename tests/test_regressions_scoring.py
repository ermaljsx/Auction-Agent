"""Regression tests for the Phase 1 review findings on scoring, the LLM layer and the report."""
import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from auction_agent import db
from auction_agent.report import generate_report
from auction_agent.scoring.heuristics import assess, finalize, match_engines, model_family
from auction_agent.scoring.llm import GeminiClient, LLMTransientError
from auction_agent.scoring.service import llm_payload, rescore_all
from tests.test_scoring import FAV, MAJ, TODAY, facts, fee, score
from tests.test_service_report import NOW, FakeClient, llm_settings, seed


# ------------------------------------------------------------------ comment lexicon (scoring-2, scoring-6)

@pytest.mark.parametrize("comment", ["Boîte de vitesses HS", "BVA HS, prévoir remplacement", "Moteur H.S.",
                                     "moteur hors service"])
def test_broken_engine_or_gearbox_spellings_are_red(settings, comment):
    _, r = score(facts(comments=comment), settings, V=100)
    assert r["tier"] == "red"


@pytest.mark.parametrize("comment", ["Véhicule non VGE", "pas VGE", "aucun voyant allumé, aucun bruit, sans choc",
                                     "Sellerie cuir chocolat", "Pare-chocs couleur carrosserie", "pas de fumée"])
def test_negated_or_unrelated_words_are_not_penalised(settings, comment):
    part, r = score(facts(comments=comment), settings, V=80)
    assert not any(g["code"] == "comment_dealbreaker" for g in r["gates"])
    assert not any(x["code"].startswith("comment_") for x in part.reasons)


# ------------------------------------------------------------------ young car with voluntary CT (scoring-3)

def test_young_car_with_voluntary_ct_is_not_penalised(settings):
    volontaire = dict(FAV, nature="volontaire", parse_confidence=0.6)
    _, with_ct = score(facts(year=2024, first_registration="2024-02-01", ct=volontaire), settings, V=80)
    _, without = score(facts(year=2024, first_registration="2024-02-01", ct=None, ct_doc="missing"), settings, V=80)
    assert with_ct["confidence"] == without["confidence"] == "full"
    assert with_ct["max_tier"] == "green"


def test_young_car_voluntary_ct_with_critical_defect_still_red(settings):
    bad = dict(MAJ, nature="volontaire", parse_confidence=0.6,
               defects=[{"code": "1.1.1.a.3", "severity": "critical", "system": "freinage", "label": "x"}])
    _, r = score(facts(year=2024, first_registration="2024-02-01", ct=bad), settings, V=100)
    assert r["tier"] == "red"


# ------------------------------------------------------------------ cohorts and KB (scoring-4, scoring-9)

def test_model_family_keeps_prefix_models_apart():
    assert model_family("CLASSE A 180") == "CLASSE A" and model_family("CLASSE E 220") == "CLASSE E"
    assert model_family("SERIE 1") != model_family("SERIE 5")
    assert model_family("C-ELYSEE") == "C-ELYSEE" and model_family("CLIO IV") == "CLIO"


def test_engine_kb_spellings(settings):
    citroen = facts(brand="CITROËN", model="C3", trim="C3 PURETECH 110 S&S", fuel="petrol", engine_cc=1199)
    assert match_engines(citroen)
    opel = facts(brand="OPEL", model="CORSA", trim="CORSA 1.2 TURBO 100", fuel="petrol", engine_cc=1199, year=2021)
    assert match_engines(opel)
    dv5 = facts(brand="PEUGEOT", model="208", trim="208 BLUEHDI 100 S&S", fuel="diesel", engine_cc=1499, year=2020)
    assert [e["id"] for e in match_engines(dv5)] == ["stellantis_bluehdi_15_dv5"]
    dv6 = replace(dv5, engine_cc=None, year=2017)  # 1.6 BlueHDi 100 without cc: not claimed
    assert match_engines(dv6) == []
    twingo = facts(brand="RENAULT", model="TWINGO", trim="TWINGO II 1.2 TCE 100", fuel="petrol", engine_cc=None, year=2012)
    assert match_engines(twingo) == []


# ------------------------------------------------------------------ explanations and dates (scoring-10, 11)

def test_every_gate_is_explained_even_below_its_cap(settings):
    odo = dict(FAV, odometer_inconsistent=True)
    _, r = score(facts(ct=odo, mileage_km=260_000, year=2008, first_registration="2008-01-01"), settings, V=20)
    assert any(x["code"] == "odometer" for x in r["reasons"])


def test_ct_exactly_six_months_old_needs_redo(settings):
    ct = dict(FAV, inspection_date="2026-04-10")
    sale = date(2026, 9, 19)  # ref date = sale + 21 days = 2026-10-10, exactly 6 months
    part = assess(facts(ct=ct, sale_date=sale), settings, TODAY, fee)
    assert "ct_redo" in part.flags


def test_age_follows_injected_date_without_a_sale_date(settings):
    """conformance-12 (a): ageing really depends on `today` when no sale date is known."""
    f = facts(first_registration="2011-06-01", year=2011, sale_date=None)
    young = assess(f, settings, date(2021, 1, 1), fee)   # 9.6 years at ref date
    old = assess(f, settings, date(2027, 1, 1), fee)     # 15.6 years
    assert old.R < young.R


# ------------------------------------------------------------------ LLM layer (scoring-1/5/7/8, conformance-1/5/8)

class NetworkDownClient(FakeClient):
    def review(self, payloads):
        self.calls.append(payloads)
        raise ConnectionError("network unreachable")


def test_network_error_never_skips_the_heuristic_rebuild(settings):
    s = llm_settings(settings)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        out = rescore_all(conn, s, today=TODAY, now=NOW, client=NetworkDownClient())
        assert out["scored"] == 8 and any("unavailable" in n for n in out["notes"])
        assert conn.execute("SELECT count(*) FROM assessments").fetchone()[0] == 8


def test_gemini_client_wraps_unexpected_errors(monkeypatch):
    client = GeminiClient.__new__(GeminiClient)
    client.model, client._thinking_supported = "m", True
    from google.genai import types
    client._types = types

    class Boom:
        class models:
            @staticmethod
            def generate_content(**_kw):
                raise OSError("connection reset")

    client._client = Boom()
    with pytest.raises(LLMTransientError):
        client.review([{"key": "1"}])


def test_verdict_rows_also_store_the_adjustment(settings):
    s = llm_settings(settings)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        rescore_all(conn, s, today=TODAY, now=NOW, client=FakeClient(adjustment=-4))
        rows = conn.execute("SELECT kind, adjustment FROM llm_assessments").fetchall()
        assert rows and all(r["adjustment"] == -4 for r in rows)


def test_no_llm_ignores_cached_output(settings):
    s = llm_settings(settings)
    with db.open_db(s.db_path) as conn:
        seed(conn)
        rescore_all(conn, s, today=TODAY, now=NOW, client=FakeClient(adjustment=-4))
        rescore_all(conn, s, today=TODAY, now=NOW, use_llm=False)
        assert conn.execute("SELECT count(*) FROM assessments WHERE verdict IS NOT NULL OR llm_adjustment != 0").fetchone()[0] == 0


def test_payload_has_no_price_contact_or_identity_and_is_date_stable(settings):
    vin = "VR3UPHNEKNT084478"
    from auction_agent.privacy import redact
    comments = redact(f"VIN {vin}, mise à prix 6 900 €, tél 06 12 34 56 78, ab-123-cd")
    f1 = facts(comments=comments)
    p1 = llm_payload(assess(f1, settings, TODAY, fee))
    text = json.dumps(p1, ensure_ascii=False)
    for leaked in (vin, "€", "06 12", "ab-123-cd"):
        assert leaked not in text
    p2 = llm_payload(assess(replace(f1, sale_date=date(2027, 6, 1)), settings, date(2027, 6, 1), fee))
    assert p1 == p2  # relisting later does not change the payload (no re-paid call)


def test_top_picks_and_llm_skip_cars_whose_sale_is_over(settings):
    s = llm_settings(settings)
    client = FakeClient()
    with db.open_db(s.db_path) as conn:
        seed(conn)
        later = NOW + timedelta(days=30)  # the seeded sale ended long ago
        out = rescore_all(conn, s, today=later.date(), now=later, client=client)
        assert out["llm_triggered"] == 0 and client.calls == []
        html = generate_report(conn, s, now=later).read_text(encoding="utf-8")
    assert "Les meilleures affaires" not in html


def test_gates_are_shown_on_yellow_cards_and_sale_id_accepts_house_number(settings):
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=6)
        with db.write_tx(conn):
            conn.execute("UPDATE vehicles SET comments = 'Kilométrage non garanti' WHERE id = 1")
        rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        html = generate_report(conn, settings, now=NOW).read_text(encoding="utf-8")
        assert "Kilométrage non garanti" in html
        by_house_number = generate_report(conn, settings, sale_id=1, now=NOW).read_text(encoding="utf-8")
        assert "TOYOTA YARIS" in by_house_number


def test_final_score_matches_reasons_for_capped_car(settings):
    part, r = score(facts(ct=MAJ), settings, V=100)
    gate_reasons = [x for x in r["reasons"] if x["pillar"] == "gate"]
    assert gate_reasons and r["final_score"] <= settings.green_min - 1
    assert finalize(part, 100, "x", 1.0, 0, settings)["tier"] == r["tier"]
