"""CT documents read by an older parser are re-read from their stored (redacted) text."""
import json

from auction_agent import db, documents
from auction_agent.ct_parser import PARSER_VERSION
from auction_agent.documents import reparse_stale_ct_documents
from auction_agent.privacy import redact
from auction_agent.scoring.service import rescore_all
from tests.conftest import read_fixture
from tests.test_service_report import NOW, TODAY, seed

# What ct-2.0 stored for a real layout-A PV: the phrase, no defect, no date.
STALE = {"parser_version": "ct-2.0", "result": "DEFAVORABLE_MAJEURE", "result_source": "phrase",
         "nature": "unknown", "inspection_date": None, "inspection_km": None, "defects": [],
         "counts": {"minor": 0, "major": 0, "critical": 0}, "odometer": [], "odometer_inconsistent": False,
         "suffix_disagreements": 0, "parse_confidence": 0.6}


def vehicle_ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM vehicles ORDER BY id")]


def set_doc(conn, vid, *, text, version, parsed=None, confidence=0.6):
    with db.write_tx(conn):
        conn.execute(
            """UPDATE documents SET text = ?, parser_version = ?, parsed_json = ?, parse_confidence = ?
               WHERE vehicle_id = ? AND doc_type = 'ct'""",
            (text, version, json.dumps(parsed or STALE), confidence, vid),
        )


def doc(conn, vid):
    return conn.execute("SELECT * FROM documents WHERE vehicle_id = ? AND doc_type = 'ct'", (vid,)).fetchone()


def test_stale_versions_are_reparsed_and_others_left_alone(settings):
    stored = redact(read_fixture("ct", "alcopa_a_majeure.txt"))
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=4)
        stale, older, current, no_text = vehicle_ids(conn)
        set_doc(conn, stale, text=stored, version="ct-2.0")
        set_doc(conn, older, text=stored, version=None)  # parsed before versions were recorded
        sentinel = dict(STALE, parser_version=PARSER_VERSION, sentinel=True)
        set_doc(conn, current, text=stored, version=PARSER_VERSION, parsed=sentinel)
        set_doc(conn, no_text, text=None, version="ct-2.0")

        assert reparse_stale_ct_documents(conn) == {"reparsed": 2, "failed": 0, "postponed": 0}
        for vid in (stale, older):
            row = doc(conn, vid)
            parsed = json.loads(row["parsed_json"])
            assert row["parser_version"] == PARSER_VERSION == parsed["parser_version"]
            assert row["parse_confidence"] == parsed["parse_confidence"] >= 0.8
            assert parsed["counts"] == {"minor": 4, "major": 4, "critical": 0}
            assert parsed["inspection_date"] == "2026-09-14" and parsed["inspection_km"] == 112480
            assert row["text"] == stored and row["parse_status"] == "parsed"
        assert json.loads(doc(conn, current)["parsed_json"]) == sentinel  # current version untouched
        untouched = doc(conn, no_text)
        assert untouched["parser_version"] == "ct-2.0" and json.loads(untouched["parsed_json"]) == STALE
        # Idempotent, and cheap when nothing is stale.
        assert reparse_stale_ct_documents(conn) == {"reparsed": 0, "failed": 0, "postponed": 0}


def test_reparsed_labels_and_text_are_redacted(settings):
    # A row stored before a redaction rule existed: a plate in a defect line, and the
    # unlabelled holder block of the voluntary "CV" form.
    plate_text = read_fixture("ct", "alcopa_b_2pages.txt").replace(
        "8.4.1.a.2. PERTES DE LIQUIDES : Fuite excessive de liquide autre",
        "0.1.1.d.2. PLAQUES D'IMMATRICULATION : Plaque AB-123-CD non conforme",
    )
    cv_text = read_fixture("ct", "alcopa_cv_volontaire.txt").replace("VF7TESTAB00000002", "[VIN]")
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=2)
        first, second = vehicle_ids(conn)
        set_doc(conn, first, text=plate_text, version="ct-2.0")
        set_doc(conn, second, text=cv_text, version="ct-2.0")
        assert reparse_stale_ct_documents(conn)["reparsed"] == 2
        row = doc(conn, first)
        plate = next(d for d in json.loads(row["parsed_json"])["defects"] if d["code"] == "0.1.1.d.2")
        assert "AB-123-CD" not in plate["label"] and "[IMMAT]" in plate["label"]
        assert "AB-123-CD" not in row["text"]
        row = doc(conn, second)
        assert "DUPONT-ESSAI" not in row["text"] and "HAMEAU-FICTIF" not in row["text"]
        assert json.loads(row["parsed_json"])["inspection_km"] == 64210


def test_odd_text_and_parser_errors_never_raise(settings, monkeypatch):
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=3)
        odd, broken, good = vehicle_ids(conn)
        set_doc(conn, odd, text="\x00\x01 ÿ‮" * 30 + "\n" * 50 + "1.1.1.1.1.1.1" * 40, version="ct-2.0")
        set_doc(conn, broken, text="BROKEN " * 20, version="ct-2.0")
        set_doc(conn, good, text=read_fixture("ct", "alcopa_b_critique.txt"), version="ct-2.0")
        real = documents.parse_ct

        def flaky(text):
            if text.startswith("BROKEN"):
                raise RuntimeError("boom")
            return real(text)

        monkeypatch.setattr(documents, "parse_ct", flaky)
        assert reparse_stale_ct_documents(conn) == {"reparsed": 2, "failed": 1, "postponed": 0}
        assert json.loads(doc(conn, odd)["parsed_json"])["result"] == "UNKNOWN"
        assert doc(conn, broken)["parser_version"] == "ct-2.0"  # previous reading kept
        assert json.loads(doc(conn, good)["parsed_json"])["result"] == "DEFAVORABLE_CRITIQUE"


def test_a_row_changed_while_parsing_is_not_overwritten(settings, monkeypatch):
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=1)
        (vid,) = vehicle_ids(conn)
        set_doc(conn, vid, text=read_fixture("ct", "alcopa_b_critique.txt"), version="ct-2.0")
        real = documents.parse_ct

        def crawl_meanwhile(text):
            with db.write_tx(conn):  # a crawl re-fetches the PV between read and write
                conn.execute("UPDATE documents SET text = 'refetched', parser_version = ? WHERE vehicle_id = ?",
                             (PARSER_VERSION, vid))
            return real(text)

        monkeypatch.setattr(documents, "parse_ct", crawl_meanwhile)
        assert reparse_stale_ct_documents(conn) == {"reparsed": 0, "failed": 0, "postponed": 0}
        assert doc(conn, vid)["text"] == "refetched"


def test_rescore_rereads_stale_reports_before_scoring(settings):
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=3)
        vid = vehicle_ids(conn)[0]
        set_doc(conn, vid, text=redact(read_fixture("ct", "alcopa_a_majeure.txt")), version="ct-2.0")
        out = rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        assert any(f"Re-read 1 CT report(s) with parser {PARSER_VERSION}" in n for n in out["notes"])
        flags = json.loads(conn.execute("SELECT flags_json FROM assessments WHERE vehicle_id = ?",
                                        (vid,)).fetchone()[0])
        assert "ct_non_lu" not in flags  # the re-read PV is no longer "illisible ou incertain"
        reasons = json.loads(conn.execute("SELECT reasons_json FROM assessments WHERE vehicle_id = ?",
                                          (vid,)).fetchone()[0])
        assert any(r["code"] == "ct_major" for r in reasons)
        again = rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        assert not any("Re-read" in n for n in again["notes"])


# ------------------------------------------------------------------ review fixes

CRITICAL_STALE = dict(STALE, result="DEFAVORABLE_CRITIQUE")


def assessment(conn, vid):
    row = conn.execute("SELECT tier, gates_json FROM assessments WHERE vehicle_id = ?", (vid,)).fetchone()
    return row["tier"], [g["code"] for g in json.loads(row["gates_json"])]


def test_a_parser_bug_never_replaces_a_stored_critical_reading(settings, monkeypatch):
    from auction_agent import ct_parser
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=1)
        (vid,) = vehicle_ids(conn)
        set_doc(conn, vid, text=redact(read_fixture("ct", "alcopa_b_critique.txt")), version="ct-2.0",
                parsed=CRITICAL_STALE)

        def bug(summary, text):  # any exception inside the parser, caught by its own guard
            raise IndexError("simulated parser bug")

        monkeypatch.setattr(ct_parser, "_parse_into", bug)
        assert reparse_stale_ct_documents(conn) == {"reparsed": 0, "failed": 1, "postponed": 0}
        row = doc(conn, vid)
        assert row["parser_version"] == "ct-2.0" and json.loads(row["parsed_json"]) == CRITICAL_STALE
        out = rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        assert any("could not be re-read" in n for n in out["notes"])
        tier, gates = assessment(conn, vid)
        assert tier == "red" and "ct_critique" in gates
        # Retried at the next run, and re-read once the parser works again.
        assert reparse_stale_ct_documents(conn)["failed"] == 1
        monkeypatch.undo()
        assert reparse_stale_ct_documents(conn)["reparsed"] == 1
        assert json.loads(doc(conn, vid)["parsed_json"])["result"] == "DEFAVORABLE_CRITIQUE"


def test_a_row_read_by_a_newer_parser_is_never_downgraded(settings):
    stored = redact(read_fixture("ct", "alcopa_a_majeure.txt"))
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=3)
        newer, much_newer, older = vehicle_ids(conn)
        sentinel = dict(STALE, parser_version="ct-9.0", sentinel=True)
        set_doc(conn, newer, text=stored, version="ct-9.0", parsed=sentinel)
        set_doc(conn, much_newer, text=stored, version="ct-2.10", parsed=sentinel)  # 2.10 > 2.1
        set_doc(conn, older, text=stored, version="ct-1.9")
        assert reparse_stale_ct_documents(conn) == {"reparsed": 1, "failed": 0, "postponed": 0}
        assert json.loads(doc(conn, newer)["parsed_json"]) == sentinel
        assert json.loads(doc(conn, much_newer)["parsed_json"]) == sentinel
        assert doc(conn, older)["parser_version"] == PARSER_VERSION


def test_a_busy_database_keeps_the_written_batches_and_reports_the_rest(settings, monkeypatch):
    import sqlite3
    stored = redact(read_fixture("ct", "alcopa_a_majeure.txt"))
    with db.open_db(settings.db_path) as conn:
        seed(conn, n=3)
        for vid in vehicle_ids(conn):
            set_doc(conn, vid, text=stored, version="ct-2.0")
        real_tx, calls = db.write_tx, []

        def busy_after_first(c):
            calls.append(1)
            if len(calls) > 1:
                raise sqlite3.OperationalError("database is locked")
            return real_tx(c)

        monkeypatch.setattr(documents.db, "write_tx", busy_after_first)
        assert reparse_stale_ct_documents(conn, batch_size=1) == {"reparsed": 1, "failed": 0, "postponed": 2}
        monkeypatch.undo()
        versions = [doc(conn, vid)["parser_version"] for vid in vehicle_ids(conn)]
        assert versions == [PARSER_VERSION, "ct-2.0", "ct-2.0"] and not conn.in_transaction

        def busy_rescore(c, **kw):
            return {"reparsed": 1, "failed": 0, "postponed": 2}

        monkeypatch.setattr("auction_agent.scoring.service.reparse_stale_ct_documents", busy_rescore)
        out = rescore_all(conn, settings, today=TODAY, now=NOW, use_llm=False)
        assert any("Re-read 1 CT report(s)" in n and "2 postponed (database busy)" in n for n in out["notes"])


def test_ingest_never_stores_a_parser_error_as_a_reading(settings, monkeypatch):
    from auction_agent import ct_parser
    from auction_agent.documents import process_document_bytes
    from tests.conftest import FIXTURES
    pdf = (FIXTURES / "pdf" / "ct_min.pdf").read_bytes()

    def bug(summary, text):
        raise IndexError("simulated parser bug")

    monkeypatch.setattr(ct_parser, "_parse_into", bug)
    out = process_document_bytes("ct", "https://example.test/getDocument/ct/x", pdf, settings)
    # "failed": the ingest keeps a previous good copy, and the empty version makes the
    # next scoring run re-read the stored (redacted) text.
    assert out.parse_status == "failed" and out.parsed is None and out.parser_version is None
    assert out.text and "[VIN]" in out.text
