"""Battery-certificate parser (bat-1.0), the certificate-link redaction and the stored re-read."""
import json

import pytest

from auction_agent import db, documents
from auction_agent.battery_parser import PARSER_VERSION, parse_battery_certificate
from auction_agent.documents import process_document_bytes, reparse_stale_battery_documents, reparse_stale_ct_documents
from auction_agent.privacy import redact, redact_battery_text, redact_document_links
from auction_agent.scoring.service import rescore_all
from tests.conftest import FIXTURES, read_fixture
from tests.test_service_report import NOW, TODAY, seed

LINK_PARTS = ("http", "certificate.example.test", ".pdf", "www.", "c0000000000")


def bat(name: str) -> str:
    return read_fixture("battery", name)


# ------------------------------------------------------------------ parser


def test_reads_the_moba_certificate():
    out = parse_battery_certificate(bat("moba_phev.txt"))
    assert out["parser_version"] == PARSER_VERSION == "bat-1.0"
    assert out["provider"] == "moba"
    assert out["soh_pct"] == 96.25 and out["soh_source"] == "certificate"
    assert (out["capacity_kwh"], out["capacity_new_kwh"]) == (10.88, 11.3)
    assert out["capacity_ratio_pct"] == pytest.approx(96.28, abs=0.01)
    assert out["diagnostic_date"] == "2026-09-14" and out["diagnostic_km"] == 64210
    assert out["km_declared"] is False
    assert out["warranty_hint"] == {"soh_min_pct": 70, "years": 8, "km": 160000}
    assert out["warnings"] == [] and out["parse_confidence"] == 1.0


def test_reads_an_ev_certificate_and_shifted_footnotes():
    ev = parse_battery_certificate(bat("moba_ev.txt"))
    assert ev["soh_pct"] == 88.5 and ev["parse_confidence"] == 1.0 and ev["diagnostic_km"] == 61000
    assert ev["diagnostic_date"] == "2026-10-02"
    declared = parse_battery_certificate(bat("moba_declared_km.txt"))
    assert declared["soh_pct"] == 94.75 and declared["parse_confidence"] == 1.0
    assert declared["km_declared"] is True and declared["diagnostic_km"] == 98300
    assert declared["warranty_hint"] == {"soh_min_pct": 70, "years": 6, "km": 100000}


def test_percentile_and_warranty_lines_are_never_the_soh():
    out = parse_battery_certificate(bat("moba_no_soh.txt"))
    # No SoH line: the certificate's own fallback, usable over new capacity.
    assert out["soh_pct"] == 88.0 and out["soh_source"] == "capacity_ratio"
    assert out["parse_confidence"] == 0.85
    without_capacity = "\n".join(line for line in bat("moba_no_soh.txt").splitlines() if "kWh" not in line)
    nothing = parse_battery_certificate(without_capacity)
    assert nothing["soh_pct"] is None and nothing["parse_confidence"] == 0.0  # not 50 nor 70
    assert nothing["warnings"] == ["soh_not_found"]


def test_soh_far_from_the_capacity_ratio_lowers_confidence():
    out = parse_battery_certificate(bat("moba_mismatch.txt"))
    assert out["soh_pct"] == 97.0 and out["parse_confidence"] == 0.6
    assert "soh_capacity_mismatch" in out["warnings"]


def test_soh_without_capacity_line_and_out_of_range_values():
    text = "\n".join(line for line in bat("moba_phev.txt").splitlines() if "kWh" not in line)
    out = parse_battery_certificate(text)
    assert out["soh_pct"] == 96.25 and out["parse_confidence"] == 0.9
    absurd = bat("moba_phev.txt").replace("96,25%", "12%").replace("10,88 kWh", "1,2 kWh")
    out = parse_battery_certificate(absurd)
    assert out["soh_pct"] is None and "soh_out_of_range" in out["warnings"] and out["parse_confidence"] == 0.0


@pytest.mark.parametrize("text", [None, "", "   ", "\x00\x01 ÿ‮" * 40, "%" * 500, "Facture garage\n12%\n",
                                  "Certificat batterie\n" + "99%\n" * 3000])
def test_never_raises(text):
    out = parse_battery_certificate(text)
    assert out["parser_version"] == PARSER_VERSION and isinstance(out["warnings"], list)
    assert 0.0 <= out["parse_confidence"] <= 1.0


def test_unknown_layout_and_internal_errors(monkeypatch):
    from auction_agent import battery_parser
    assert parse_battery_certificate("Facture garage, vidange et filtres")["warnings"] == ["unknown_layout"]

    def bug(summary, text):
        raise IndexError("simulated parser bug")

    monkeypatch.setattr(battery_parser, "_parse_into", bug)
    out = parse_battery_certificate(bat("moba_phev.txt"))
    assert out["warnings"] == ["parse_error:IndexError"] and out["soh_pct"] is None


@pytest.mark.parametrize("name", ["moba_phev.txt", "moba_ev.txt", "moba_declared_km.txt", "moba_no_soh.txt",
                                  "moba_mismatch.txt"])
def test_same_reading_from_the_stored_text(name):
    text = bat(name)
    assert parse_battery_certificate(redact_battery_text(text)) == parse_battery_certificate(text)


def test_summary_holds_no_text_identifier_or_link():
    dump = json.dumps(parse_battery_certificate(bat("moba_phev.txt")), ensure_ascii=False)
    for word in ("VIN", "IMMAT", "Peugeot", "SIRET", "Ville-Exemple", *LINK_PARTS):
        assert word not in dump


# ------------------------------------------------------------------ link redaction


@pytest.mark.parametrize("name", ["moba_phev.txt", "moba_declared_km.txt"])
def test_certificate_link_and_wrapped_pdf_tail_are_removed(name):
    stored = redact_battery_text(bat(name))
    for part in LINK_PARTS:
        assert part not in stored
    assert "[LIEN]" in stored and "[VIN]" in stored and "[IMMAT]" in stored  # placeholders kept
    assert "Moba" in stored  # the provider can still be recognised
    assert redact_battery_text(stored) == stored  # idempotent
    assert redact_document_links(stored) == stored


def test_link_redaction_only_touches_links():
    assert redact_document_links("Tension du pack 300,5 V\nKilométrage : 64 210 km") == \
        "Tension du pack 300,5 V\nKilométrage : 64 210 km"
    assert redact_document_links("voir rapport.PDF ici") == "voir [LIEN] ici"
    assert redact_document_links(None) is None and redact_document_links("") == ""


# ------------------------------------------------------------------ ingest and stored re-read


def test_ingest_parses_a_battery_certificate_and_stores_it_without_the_link(settings, monkeypatch):
    pdf = (FIXTURES / "pdf" / "ct_min.pdf").read_bytes()
    monkeypatch.setattr(documents, "extract_pdf_text", lambda data, pages: bat("moba_phev.txt"))
    out = process_document_bytes("battery", "https://example.test/getDocument/moba/x", pdf, settings)
    assert out.parse_status == "parsed" and out.parser_version == PARSER_VERSION
    assert out.parsed["soh_pct"] == 96.25 and out.parse_confidence == 1.0
    assert not any(part in out.text for part in LINK_PARTS)


def test_ingest_never_stores_a_battery_parser_error_as_a_reading(settings, monkeypatch):
    from auction_agent import battery_parser
    pdf = (FIXTURES / "pdf" / "ct_min.pdf").read_bytes()
    monkeypatch.setattr(documents, "extract_pdf_text", lambda data, pages: bat("moba_phev.txt"))

    def bug(summary, text):
        raise IndexError("simulated parser bug")

    monkeypatch.setattr(battery_parser, "_parse_into", bug)
    out = process_document_bytes("battery", "https://example.test/getDocument/moba/x", pdf, settings)
    assert out.parse_status == "failed" and out.parsed is None and out.parser_version is None
    assert out.text and "c0000000000" not in out.text


def vehicle_ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM vehicles ORDER BY id")]


def add_battery(conn, vid, *, text, version=None, parsed=None, confidence=None, fuel="plugin_hybrid", soh=None):
    with db.write_tx(conn):
        conn.execute("UPDATE vehicles SET fuel = ?, battery_soh_pct = ? WHERE id = ?", (fuel, soh, vid))
        db.upsert_document(conn, vid, {"doc_type": "battery", "source_url": "https://example.test/getDocument/moba/x",
                                       "fetch_status": "ok", "parse_status": "parsed", "text": text,
                                       "parser_version": version, "parse_confidence": confidence,
                                       "parsed": parsed}, "2026-10-13T08:00:00Z")


def battery_doc(conn, vid):
    return conn.execute("SELECT * FROM documents WHERE vehicle_id = ? AND doc_type = 'battery'", (vid,)).fetchone()


def test_stored_certificates_are_reparsed_once_and_lose_their_link(settings):
    # What the crawl stored before bat-1.0: redacted text with the link, no reading.
    stored = redact(bat("moba_phev.txt"))
    assert "certificate.example.test" in stored
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=3)
        first, newer, ct_only = vehicle_ids(conn)
        add_battery(conn, first, text=stored)
        sentinel = {"parser_version": "bat-9.0", "sentinel": True}
        add_battery(conn, newer, text=stored, version="bat-9.0", parsed=sentinel, confidence=1.0)
        assert reparse_stale_ct_documents(conn)["reparsed"] == 0  # the CT re-read ignores battery rows
        assert reparse_stale_battery_documents(conn) == {"reparsed": 1, "failed": 0, "postponed": 0}
        row = battery_doc(conn, first)
        assert row["parser_version"] == PARSER_VERSION and row["parse_confidence"] == 1.0
        assert json.loads(row["parsed_json"])["soh_pct"] == 96.25
        assert not any(part in row["text"] for part in LINK_PARTS) and "[VIN]" in row["text"]
        assert json.loads(battery_doc(conn, newer)["parsed_json"]) == sentinel  # never downgraded
        assert battery_doc(conn, ct_only) is None
        assert reparse_stale_battery_documents(conn) == {"reparsed": 0, "failed": 0, "postponed": 0}


def test_battery_reread_ignores_ct_rows_and_skips_a_row_changed_meanwhile(settings, monkeypatch):
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=1)
        (vid,) = vehicle_ids(conn)
        with db.write_tx(conn):  # a stale CT row must not be read by the battery re-read
            conn.execute("UPDATE documents SET text = 'x', parser_version = 'ct-2.0' WHERE doc_type = 'ct'")
        add_battery(conn, vid, text=redact(bat("moba_ev.txt")))
        real = documents.parse_battery

        def crawl_meanwhile(text):
            with db.write_tx(conn):
                conn.execute("UPDATE documents SET text = 'refetched', parser_version = ? "
                             "WHERE vehicle_id = ? AND doc_type = 'battery'", (PARSER_VERSION, vid))
            return real(text)

        monkeypatch.setattr(documents, "parse_battery", crawl_meanwhile)
        assert reparse_stale_battery_documents(conn) == {"reparsed": 0, "failed": 0, "postponed": 0}
        assert battery_doc(conn, vid)["text"] == "refetched"
        ct = conn.execute("SELECT parser_version FROM documents WHERE doc_type = 'ct'").fetchone()[0]
        assert ct == "ct-2.0"


def test_rescore_rereads_certificates_and_uses_them(settings):
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=3)
        vid = vehicle_ids(conn)[0]
        # An electric car whose listing shows no SoH: the certificate gives it.
        low = redact(bat("moba_ev.txt")).replace("88,5%", "72,4%").replace("40,98 kWh", "33,52 kWh")
        add_battery(conn, vid, text=low, fuel="electric", soh=None)
        out = rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        assert any(f"Re-read 1 battery certificate(s) with parser {PARSER_VERSION}." in n for n in out["notes"])
        row = conn.execute("SELECT tier, gates_json FROM assessments WHERE vehicle_id = ?", (vid,)).fetchone()
        gate = next(g for g in json.loads(row["gates_json"]) if g["code"] == "battery_soh")
        assert row["tier"] == "red" and gate["text"] == "Batterie dégradée (SoH 72 %)" and gate["src"] == "battery"
        again = rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        assert not any("battery certificate" in n for n in again["notes"])


def test_rescore_reports_a_busy_or_failing_battery_reread(settings, monkeypatch):
    import sqlite3
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=2)

        def busy(c, **kw):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("auction_agent.scoring.service.reparse_stale_battery_documents", busy)
        out = rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        assert any(n.startswith("Battery certificates not re-read this run") for n in out["notes"])
        assert out["scored"] == 2  # scoring went on with the stored readings

        monkeypatch.setattr("auction_agent.scoring.service.reparse_stale_battery_documents",
                            lambda c, **kw: {"reparsed": 1, "failed": 1, "postponed": 2})
        out = rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        assert any("Re-read 1 battery certificate(s)" in n and "2 postponed (database busy)" in n
                   for n in out["notes"])
        assert any(n.startswith("1 battery certificate(s) could not be re-read") for n in out["notes"])
