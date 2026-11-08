import os
import time

from auction_agent.documents import (
    is_pdf,
    process_document_bytes,
    quarantine,
    sweep_quarantine,
)
from auction_agent.privacy import identity_hmac, redact
from tests.conftest import FIXTURES

PDF = (FIXTURES / "pdf" / "ct_min.pdf").read_bytes()


def test_pdf_is_processed_in_memory_and_redacted(settings, tmp_path):
    doc = process_document_bytes("ct", "https://example.test/getDocument/ct/x", PDF, settings)
    assert doc.fetch_status == "ok" and doc.parse_status == "parsed"
    assert len(doc.sha256) == 64 and doc.size_bytes == len(PDF)
    assert "ZAC5JAC51PJK11650" not in doc.text and "GP-755-SL" not in doc.text
    assert "[VIN]" in doc.text and "[IMMAT]" in doc.text
    assert doc.parsed["result"] == "DEFAVORABLE_MAJEURE"
    assert doc.parsed["counts"]["major"] == 2
    # Nothing was written to disk.
    assert not settings.quarantine_dir.exists()


def test_non_pdf_bytes_are_rejected(settings):
    html = b"<!DOCTYPE html><html><body>Connexion requise</body></html>" * 5
    doc = process_document_bytes("ct", "u", html, settings)
    assert doc.fetch_status == "not_pdf" and doc.text is None
    assert is_pdf(b"\n\n%PDF-1.7 ...") is True


def test_truncated_pdf_is_quarantined_with_cap(settings):
    broken = PDF[:400]
    doc = process_document_bytes("ct", "u", broken, settings)
    assert doc.parse_status == "failed"
    files = list(settings.quarantine_dir.glob("*.pdf"))
    assert len(files) == 1
    small = settings.replace(quarantine_max_files=1)
    assert quarantine(b"%PDF-other", "f" * 64, small) is None


def test_quarantine_sweep_removes_old_files(settings):
    path = quarantine(b"%PDF-1.4 x", "a" * 64, settings)
    old = time.time() - 8 * 86400
    os.utime(path, (old, old))
    assert sweep_quarantine(settings) == 1
    assert not path.exists()


def test_redaction_patterns():
    text = "VIN VF1CND10544572749 plaque BE-680-YB ancienne 1234 AB 75\nPropriétaire : Jean Dupont\n1.1.13.a.2 Plaquettes"
    out = redact(text)
    for secret in ("VF1CND10544572749", "BE-680-YB", "1234 AB 75", "Jean Dupont"):
        assert secret not in out
    assert "1.1.13.a.2 Plaquettes" in out


def test_identity_hmac_normalises_and_is_keyed():
    k1, k2 = b"k" * 32, b"j" * 32
    assert identity_hmac(k1, "ab-123-cd") == identity_hmac(k1, "AB123CD")
    assert identity_hmac(k1, "AB123CD") != identity_hmac(k2, "AB123CD")
    assert identity_hmac(k1, None) is None
