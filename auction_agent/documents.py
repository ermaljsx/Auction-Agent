"""In-memory document pipeline: fetch → validate → hash → extract → redact.

PDFs are never written to disk (owner decision #2), so there is nothing to purge. The
single exception is a bounded quarantine for PDFs that fail to parse, kept at most
``quarantine_ttl_days`` for debugging and swept by ``sweep_quarantine``.
"""
from __future__ import annotations

import hashlib
import io
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pdfplumber

from .ct_parser import PARSER_VERSION, parse_controle_technique
from .http_client import DocumentTooLargeError, PoliteClient
from .config import Settings
from .privacy import redact


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
    doc.text = redact(text)
    if doc_type == "ct":
        doc.parsed = parse_controle_technique(doc.text)
        doc.parser_version = PARSER_VERSION
        doc.parse_confidence = doc.parsed["parse_confidence"]
    doc.parse_status = "parsed"
    return doc


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
    qdir = settings.quarantine_dir
    qdir.mkdir(parents=True, exist_ok=True)
    existing = sorted(qdir.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
    if len(existing) >= settings.quarantine_max_files:
        return None
    path = qdir / f"{sha256}.pdf"
    path.write_bytes(data)
    return path


def sweep_quarantine(settings: Settings, now: Optional[float] = None) -> int:
    qdir = settings.quarantine_dir
    if not qdir.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - settings.quarantine_ttl_days * 86400
    removed = 0
    for path in qdir.glob("*.pdf"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
