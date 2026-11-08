"""What the scoring run reads from the database: damage list, battery certificate, warranty label."""
import json

from auction_agent import db
from auction_agent.scoring.service import load_facts, rescore_all
from tests.test_scoring import LIKE_ARKANA
from tests.test_service_report import NOW, NOW_S, TODAY, seed


def vehicle_ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM vehicles ORDER BY id")]


def set_vehicle(conn, vid, **cols):
    with db.write_tx(conn):
        for key, value in cols.items():
            conn.execute(f"UPDATE vehicles SET {key} = ? WHERE id = ?", (value, vid))


def set_battery(conn, vid, parsed, *, status="parsed", fetch="ok"):
    with db.write_tx(conn):
        db.upsert_document(conn, vid, {"doc_type": "battery", "source_url": "u", "fetch_status": fetch,
                                       "parse_status": status, "parsed": parsed,
                                       "parser_version": "bat-1.0", "text": "x"}, NOW_S)


def reasons(conn, vid):
    return json.loads(conn.execute("SELECT reasons_json FROM assessments WHERE vehicle_id = ?",
                                   (vid,)).fetchone()[0])


def test_load_facts_reads_damages_and_ignores_unreadable_json(settings):
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=4)
        listed, empty, broken, unread = vehicle_ids(conn)
        set_vehicle(conn, listed, damages_json=json.dumps(LIKE_ARKANA + [{"zone": 3}, "x"], ensure_ascii=False))
        set_vehicle(conn, empty, damages_json="[]")
        set_vehicle(conn, broken, damages_json="{not json")
        facts = {f.vehicle_id: f for f in load_facts(conn, NOW_S)}
        assert facts[listed].damages == LIKE_ARKANA  # only {zone, kind} string pairs
        assert facts[empty].damages == [] and facts[broken].damages is None and facts[unread].damages is None
        rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        damage = next(r for r in reasons(conn, listed) if r["code"] == "body_damage")
        assert damage["text"].startswith("Défauts esthétiques sur 8 éléments de carrosserie")
        assert not any(r["code"] == "body_damage" for r in reasons(conn, empty))


def test_load_facts_keeps_only_a_sure_certificate_reading(settings):
    reading = {"parser_version": "bat-1.0", "provider": "moba", "soh_pct": 88.0, "soh_source": "certificate",
               "capacity_kwh": 40.7, "capacity_new_kwh": 46.3, "capacity_ratio_pct": 87.9,
               "diagnostic_date": "2026-10-02", "diagnostic_km": 61000, "km_declared": False,
               "warranty_hint": {"soh_min_pct": 70, "years": 8, "km": 160000}, "warnings": [],
               "parse_confidence": 1.0}
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=4)
        sure, unsure, failed, none = vehicle_ids(conn)
        for vid in (sure, unsure, failed):
            set_vehicle(conn, vid, fuel="electric", battery_soh_pct=95.0)
        set_battery(conn, sure, reading)
        set_battery(conn, unsure, dict(reading, parse_confidence=0.6))
        set_battery(conn, failed, reading, fetch="error")
        facts = {f.vehicle_id: f for f in load_facts(conn, NOW_S)}
        cert = facts[sure].battery_cert
        assert set(cert) == {"soh_pct", "soh_source", "capacity_kwh", "capacity_new_kwh", "diagnostic_date",
                             "diagnostic_km", "km_declared", "warranty_hint", "parse_confidence"}
        # 95 on the listing, 88 on the certificate: the lower value, flagged.
        assert (facts[sure].battery_soh_pct, facts[sure].battery_soh_source) == (88.0, "lowest")
        for vid in (unsure, failed):
            assert facts[vid].battery_cert is None
            assert (facts[vid].battery_soh_pct, facts[vid].battery_soh_source) == (95.0, "listing")
        assert (facts[none].battery_cert, facts[none].battery_soh_source) == (None, None)
        rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        flags = json.loads(conn.execute("SELECT flags_json FROM assessments WHERE vehicle_id = ?",
                                        (sure,)).fetchone()[0])
        assert "battery_soh_mismatch" in flags


def test_bare_warranty_badge_reads_as_a_mechanical_warranty(settings):
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=2)
        badge, label = vehicle_ids(conn)
        set_vehicle(conn, badge, warranty="Garantie")
        set_vehicle(conn, label, warranty="Garantie Sérénité de 3 mois incluse")
        rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        text = {vid: next(r["text"] for r in reasons(conn, vid) if r["code"] == "warranty") for vid in (badge, label)}
        assert text == {badge: "Garantie mécanique incluse (formule non précisée)",
                        label: "Garantie Sérénité de 3 mois incluse"}
        assert conn.execute("SELECT DISTINCT heuristic_version FROM assessments").fetchone()[0] == "h1-2026.11b"
