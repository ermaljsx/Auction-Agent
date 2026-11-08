"""In-memory document pipeline: fetch → validate → hash → extract → redact.

PDFs are never written to disk (owner decision #2), so there is nothing to purge. The
single exception is a bounded quarantine for PDFs that fail to parse, kept at most
``quarantine_ttl_days`` for debugging and swept by ``sweep_quarantine``.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import pdfplumber

from . import db
from .battery_parser import PARSER_VERSION as BATTERY_PARSER_VERSION
from .battery_parser import parse_battery_certificate
from .ct_parser import PARSER_VERSION, parse_controle_technique
from .http_client import DocumentTooLargeError, PoliteClient
from .config import Settings
from .privacy import redact, redact_battery_text


@dataclass
class ProcessedDocument:
    doc_type: str
    source_url: str
    fetch_status: str  # ok | not_fetched | not_found | not_pdf | too_large | error
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    parse_status: Optional[str] = None  # parsed | empty_text | failed
    parser_version: Optional[str] = None
    parse_confidence: Optional[float] = None
    text: Optional[str] = None
    parsed: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    def as_row(self) -> dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "source_url": self.source_url,
            "fetch_status": self.fetch_status,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "parse_status": self.parse_status,
            "parser_version": self.parser_version,
            "parse_confidence": self.parse_confidence,
            "text": self.text,
            "parsed": self.parsed,
        }


def is_pdf(data: bytes) -> bool:
    # The PDF header must appear within the first 1024 bytes (ISO 32000 tolerance).
    return b"%PDF-" in data[:1024]


def extract_pdf_text(data: bytes, max_pages: int) -> Optional[str]:
    """Extract text from the first ``max_pages`` pages. Returns None if no text layer."""
    pages = list(range(1, max_pages + 1))
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data), pages=pages) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
            page.close()
    full = "\n\n".join(parts).strip()
    # Normalise once here; the CT parser additionally folds accents for matching.
    return unicodedata.normalize("NFC", full) if full else None


def process_document_bytes(
    doc_type: str, source_url: str, data: bytes, settings: Settings
) -> ProcessedDocument:
    doc = ProcessedDocument(doc_type=doc_type, source_url=source_url, fetch_status="ok")
    doc.size_bytes = len(data)
    doc.sha256 = hashlib.sha256(data).hexdigest()
    if not is_pdf(data):
        doc.fetch_status = "not_pdf"
        return doc
    try:
        text = extract_pdf_text(data, settings.max_pdf_pages)
    except Exception as exc:  # pdfminer raises a variety of exception types
        doc.parse_status = "failed"
        doc.error = f"{exc.__class__.__name__}: {exc}"
        quarantine(data, doc.sha256, settings)
        return doc
    if not text:
        doc.parse_status = "empty_text"  # scanned/image-only: "CT non lu" downstream
        return doc
    # Parse the original text in memory (redaction could hide mileage or dates), then
    # store only redacted text and redacted defect labels. A battery certificate also loses
    # the link to its up-to-date edition, a PDF that shows the VIN and plate unredacted.
    doc.text = redact_battery_text(text) if doc_type == "battery" else redact(text)
    if doc_type == "ct":
        parsed = parse_ct(text)
        if parse_failed(parsed):
            # A parser bug, not an unreadable PV: keep any previous good copy (ingest does
            # not replace a document with a failed one), and leave the version empty so the
            # next scoring run re-reads the stored text once the parser is fixed.
            doc.parse_status = "failed"
            doc.error = ", ".join(parsed["warnings"])
            return doc
        doc.parsed = parsed
        doc.parser_version = PARSER_VERSION
        doc.parse_confidence = parsed["parse_confidence"]
    elif doc_type == "battery":
        parsed = parse_battery(text)
        if parse_failed(parsed):  # same rule as the CT: a parser bug is not a reading
            doc.parse_status = "failed"
            doc.error = ", ".join(parsed["warnings"])
            return doc
        doc.parsed = parsed
        doc.parser_version = BATTERY_PARSER_VERSION
        doc.parse_confidence = parsed["parse_confidence"]
    doc.parse_status = "parsed"
    return doc


def parse_failed(parsed: dict[str, Any]) -> bool:
    """True when the parser hit an internal error (its summary is then empty, not a reading)."""
    return any(str(w).startswith("parse_error") for w in parsed.get("warnings", []))


def _version_key(version: Optional[str], family: str = "ct") -> tuple:
    """Order parser versions of one family: "ct-2.10" > "ct-2.2" > "ct-2.0" > None or an
    unknown format (another family's version included)."""
    m = re.fullmatch(rf"{re.escape(family)}-(\d+)\.(\d+)(?:\.(\d+))?", version or "")
    return tuple(int(g or 0) for g in m.groups()) if m else ()


def parse_ct(text: Optional[str]) -> dict[str, Any]:
    """Parse a CT text into what is stored: the summary, with redacted defect labels."""
    parsed = parse_controle_technique(text)
    for defect in parsed["defects"]:
        defect["label"] = redact(defect["label"]) or ""
    return parsed


def parse_battery(text: Optional[str]) -> dict[str, Any]:
    """Parse a battery-certificate text into what is stored (the summary holds no identifier)."""
    return parse_battery_certificate(text)


def reparse_stale_ct_documents(conn: sqlite3.Connection, *, batch_size: int = 50) -> dict[str, int]:
    """Re-parse every CT document read by an older parser version (see ``_reparse_stale``)."""
    # ``parse_ct`` is looked up here, at call time, so tests can replace the module attribute.
    return _reparse_stale(conn, doc_type="ct", version=PARSER_VERSION, family="ct", parse=parse_ct,
                          clean_text=redact, batch_size=batch_size)


def reparse_stale_battery_documents(conn: sqlite3.Connection, *, batch_size: int = 50) -> dict[str, int]:
    """Re-parse every battery certificate read by an older parser (or none yet), from its
    stored text; the rewrite also removes the certificate link from that text."""
    return _reparse_stale(conn, doc_type="battery", version=BATTERY_PARSER_VERSION, family="bat",
                          parse=parse_battery, clean_text=redact_battery_text, batch_size=batch_size)


def _reparse_stale(
    conn: sqlite3.Connection,
    *,
    doc_type: str,
    version: str,
    family: str,
    parse: Callable[[Optional[str]], dict[str, Any]],
    clean_text: Callable[[Optional[str]], Optional[str]],
    batch_size: int,
) -> dict[str, int]:
    """Re-parse every ``doc_type`` document read by an older parser version, from its stored text.

    The stored text is already redacted, so it is the only input: the PDF is never kept.
    It is cleaned again first (``clean_text`` is idempotent, so this only applies rules
    added since the document was fetched). Only rows with no version or an older one of the
    same family are read: a row written by a newer parser (another checkout running against
    the same database) is never downgraded. Parsing happens outside the database lock; each batch is then
    written in one short write transaction, and a row changed meanwhile (by a crawl, say)
    is left alone. Cheap when nothing is stale: a single SELECT. Never raises on odd text.

    A document whose parse fails (an exception, or the parser's own ``parse_error``
    summary) keeps its previous parse and version, so it is retried at the next run. When
    the database is busy, the batches already written stay written and the rest wait for
    the next run (``postponed``).
    """
    current = _version_key(version, family)
    stale = [row[0] for row in conn.execute(
        """SELECT id, parser_version FROM documents WHERE doc_type = ? AND text IS NOT NULL
           AND (parser_version IS NULL OR parser_version <> ?) ORDER BY id""", (doc_type, version))
        if _version_key(row[1], family) < current]
    reparsed = failed = postponed = 0
    for start in range(0, len(stale), batch_size):
        ids = stale[start:start + batch_size]
        rows = conn.execute(
            f"SELECT id, text, parser_version FROM documents WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
        updates = []
        for row in rows:
            try:
                text = clean_text(row["text"]) or row["text"]
                parsed = parse(text)
                if parse_failed(parsed):
                    raise ValueError(parsed["warnings"])
                payload = json.dumps(parsed, ensure_ascii=False)
            except Exception:  # never let one odd document stop the rescore
                failed += 1
                continue
            updates.append((text, payload, parsed["parse_confidence"], version,
                            row["id"], row["text"], row["parser_version"]))
        if not updates:
            continue
        written = 0
        try:
            with db.write_tx(conn):
                for values in updates:
                    cur = conn.execute(
                        """UPDATE documents SET text = ?, parsed_json = ?, parse_confidence = ?,
                               parser_version = ?, parse_status = 'parsed'
                           WHERE id = ? AND text = ? AND parser_version IS ?""",
                        values,
                    )
                    written += cur.rowcount
        except sqlite3.OperationalError:  # busy: this batch is rolled back, earlier ones stay
            postponed = len(updates) + len(stale[start + batch_size:])
            break
        reparsed += written
    return {"reparsed": reparsed, "failed": failed, "postponed": postponed}


def fetch_document(
    http: PoliteClient, doc_type: str, url: str, settings: Settings, referer: Optional[str] = None
) -> ProcessedDocument:
    """Download a document into memory and process it. Raises Blocked/RateLimited errors."""
    try:
        result = http.get(url, referer=referer, max_bytes=settings.max_document_bytes)
    except DocumentTooLargeError as exc:
        return ProcessedDocument(doc_type, url, "too_large", error=str(exc))
    if result.status in (404, 410):
        return ProcessedDocument(doc_type, url, "not_found")
    if "html" in result.headers.get("content-type", "").lower() and not is_pdf(result.content):
        return ProcessedDocument(doc_type, url, "not_pdf", error="HTML page instead of a PDF")
    return process_document_bytes(doc_type, url, result.content, settings)


def quarantine(data: bytes, sha256: str, settings: Settings) -> Optional[Path]:
    """Keep an unparseable PDF briefly for debugging, within a hard file cap."""
    try:
        qdir = settings.quarantine_dir
        qdir.mkdir(parents=True, exist_ok=True)
        if len(list(qdir.glob("*.pdf"))) >= settings.quarantine_max_files:
            return None
        path = qdir / f"{sha256}.pdf"
        path.write_bytes(data)
        return path
    except OSError:
        return None  # debugging aid only: never let it break a crawl


def sweep_quarantine(settings: Settings, now: Optional[float] = None) -> int:
    qdir = settings.quarantine_dir
    if not qdir.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - settings.quarantine_ttl_days * 86400
    removed = 0
    for path in qdir.glob("*.pdf"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
