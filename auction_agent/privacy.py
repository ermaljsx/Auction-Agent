"""Personal-data minimisation: identity hashing and text redaction.

VINs and licence plates can identify a person (the CNIL treats plates as personal
data), so they are never stored in clear text, never displayed and never sent to an
LLM. A keyed HMAC is kept only to recognise the same car when it is relisted. That is
pseudonymisation, not anonymisation, and it is deleted together with the vehicle.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import unicodedata
from pathlib import Path
from typing import Optional

_VIN_CHARS = "A-HJ-NPR-Z0-9"  # a VIN never contains I, O or Q
_SIV_L = "A-HJ-NP-TV-Z"  # SIV plate letters exclude I, O and U
_UNITS = r"(?:KM|KW|CV|CH|MM|CM|KG|NM|HP|PS|CC)"

# VIN: 17 chars with at least 3 digits and 3 letters. The upper-case pass also catches
# VINs glued to lower-case labels ("identificationVF3...") or to a following word.
_VIN_UPPER = re.compile(
    rf"(?<![{_VIN_CHARS}])(?=(?:[A-Z]*\d){{3}})(?=(?:\d*[A-Z]){{3}})[{_VIN_CHARS}]{{17}}"
    rf"(?:(?![{_VIN_CHARS}])|(?=[A-Z][a-z]))"
)
_VIN_ANYCASE = re.compile(
    rf"(?<![A-Za-z0-9])(?=(?:[A-Za-z]*\d){{3}})(?=(?:\d*[A-Za-z]){{3}})[{_VIN_CHARS}]{{17}}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# VIN written in groups: "VF3 ABCDE FGH123456" (upper case, at most 3 spaces).
_VIN_SPACED = re.compile(rf"(?<![A-Za-z0-9])[{_VIN_CHARS}](?:[ ]?[{_VIN_CHARS}]){{16}}(?![A-Za-z0-9])")
# VIN in groups joined by one kind of punctuation, any case: "VF3-ABCDE-FGH123456",
# "VF3.ABC.DE1.234.56789", "vf3_abcde_fgh123456" (17 VIN characters, checked in redact).
_VIN_PUNCT = re.compile(
    rf"(?<![A-Za-z0-9._-])[{_VIN_CHARS}]+([-._])[{_VIN_CHARS}]+(?:\1[{_VIN_CHARS}]+){{0,7}}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# SIV plates (2009+): AB-123-CD, ab-123-cd, AB123CD, AB 123 CD.
_SIV_DASH = re.compile(rf"(?<![A-Za-z0-9])[{_SIV_L}]{{2}}-\d{{3}}-[{_SIV_L}]{{2}}(?![A-Za-z0-9])", re.IGNORECASE)
_SIV_GLUED = re.compile(rf"(?<![A-Za-z0-9])[{_SIV_L}]{{2}}\d{{3}}[{_SIV_L}]{{2}}(?![A-Za-z0-9])", re.IGNORECASE)
_SIV_UPPER = re.compile(rf"(?<![A-Z0-9])[{_SIV_L}]{{2}}[- ]?\d{{3}}[- ]?(?!{_UNITS}\b)[{_SIV_L}]{{2}}(?![A-Z0-9])")
# Any case when the same separator joins both parts: "ab 123 cd", "AB.123.CD", "AB_123_CD".
_SIV_SEP = re.compile(
    rf"(?<![A-Za-z0-9])[{_SIV_L}]{{2}}([ ._])\d{{3}}\1(?!{_UNITS}\b)[{_SIV_L}]{{2}}(?![A-Za-z0-9])", re.IGNORECASE
)
# FNI plates (pre-2009): 1234 AB 75 / 123 ABC 2A / 12 AB 971. Units and dates excluded,
# so "98 765 KM 12/03/2026" is left alone.
_FNI_RE = re.compile(
    rf"(?<![A-Za-z0-9.,])\d{{1,4}}[- ]?(?!{_UNITS}\b)[A-Z]{{1,3}}[- ]?(?:\d{{2}}|2A|2B|97\d)(?![A-Za-z0-9/.,:])"
)
# "Label : value" fields that may carry an owner's identity (PV layout unverified).
# Only the value is removed, up to a comma, semicolon, column gap or end of line.
_OWNER_FIELD_RE = re.compile(
    r"\b(propri[eé]taire|titulaire|nom|adresse|demeurant)\b([^:\n]{0,20}):[ \t]*[^,;\n]*?(?=[ \t]{2,}|[,;\n]|$)",
    re.IGNORECASE | re.MULTILINE,
)
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
# Amounts, thousands included: "1 250 €", "6 900 HT", "2 k€", "3 K€", "2 k euros".
_MONEY_RE = re.compile(r"\d[\d   .,]*\s*(?:(?:k\s?)?(?:€|EUR\b|euros?\b)(?:\s*(?:HT|TTC)\b)?|(?:HT|TTC)\b)", re.IGNORECASE)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# French phone numbers: "06 12 34 56 78", "+33 6…", "+33 (0)6…", "0033 6…".
_PHONE_RE = re.compile(r"(?<![\d+])(?:(?:\+|00)\s?33\s?(?:\(0\)\s?)?|0)[1-9](?:[\s.\-]?\d{2}){4}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# The voluntary-inspection "CV" form prints the registration holder's name and postal
# address without any label, after the "<VIN> <energy>" and "<km> <model>" lines and
# before the function list "0 - IDENTIFICATION DU VÉHICULE". The defect column runs down
# beside it, so a holder line can carry a defect on its right: only the part before the
# defect is personal data, and the defect (code, header) must survive for the re-parse.
_CV_FORM_RE = re.compile(r"^\s*N[°o]\s*d.imprim[ée]\s+CV\b", re.IGNORECASE | re.MULTILINE)
_CV_VIN_LINE = re.compile(  # "<VIN> <energy>", the energy being GO, GPL, ES/GP... or missing
    rf"^\s*(?:\[VIN\]|(?=(?:[A-Z]*\d){{3}})[{_VIN_CHARS}]{{17}})(?:\s+\S+)?\s*$")
_CV_KM_MODEL_LINE = re.compile(r"^\s*(?:\d{1,7}|non\s+relev\S*)\s+\S", re.IGNORECASE)
_CV_FUNCTIONS_LINE = re.compile(r"^\s*0\s*-\s*IDENTIFICATION\b", re.IGNORECASE)
_CV_PLATE_LINE = re.compile(r"\(F\)\s+\d{2}/\d{2}/\d{4}")  # the top of the vehicle block
# Where the defect column starts on a merged line: "<n> - <code>", a code, a section header.
_CV_DEFECT_START = re.compile(
    r"(?:(?<!\d)\d{1,2}\s*-\s*)?(?<![\d.])\d{1,2} ?[.,] ?\d{1,2} ?[.,] ?\d{1,2} ?[.,] ?[A-Za-z] ?[.,] ?\d(?!\d)"
    r"|D[ÉE]FAILLANCES?\b",
    re.IGNORECASE,
)
_PERSONAL = "[DONNEES PERSONNELLES]"
_log = logging.getLogger(__name__)


def _redact_holder_line(line: str) -> str:
    cr = "\r" if line.endswith("\r") else ""
    body = line[:-1] if cr else line
    if not body.strip() or body.strip() == _PERSONAL:
        return line
    m = _CV_DEFECT_START.search(body)
    if m is None:
        return _PERSONAL + cr
    if not body[:m.start()].strip():
        return line  # only defect text on this line
    return f"{_PERSONAL} {body[m.start():]}{cr}"


def _redact_cv_holder(text: str) -> str:
    if not _CV_FORM_RE.search(text):
        return text
    lines = text.split("\n")
    end = next((j for j, line in enumerate(lines) if _CV_FUNCTIONS_LINE.match(line)), None)
    window = range(max(0, end - 12), end) if end is not None else range(len(lines))
    vin = next((i for i in reversed(window) if _CV_VIN_LINE.match(lines[i])), None)
    if vin is not None:
        start = vin + 1
        if start < len(lines) and _CV_KM_MODEL_LINE.match(lines[start]):
            start += 1  # the km and the model are kept: the parser reads the km there
        stop = end if end is not None else min(len(lines), start + 4)
    elif end is not None:
        # The form is recognised but its vehicle lines are not: fail closed, redacting the
        # lines just above the function list, up to the plate line.
        start = end
        while start > 0 and end - start < 6 and not _CV_PLATE_LINE.search(lines[start - 1]):
            start -= 1
        stop = end
        _log.warning("CV form: vehicle lines not recognised, %d line(s) above the function list redacted",
                     stop - start)
    else:
        _log.warning("CV form: holder block not found, nothing redacted")
        return text
    if vin is not None and end is None:
        _log.warning("CV form: function list not found, %d line(s) after the vehicle lines redacted", stop - start)
    for j in range(start, stop):
        lines[j] = _redact_holder_line(lines[j])
    return "\n".join(lines)


def _punct_vin(m: re.Match[str]) -> str:
    """``_VIN_PUNCT`` match → "[VIN]" when its characters make a VIN (17, 3+ digits and letters)."""
    chars = re.sub(r"[-._]", "", m.group(0))
    digits = sum(c.isdigit() for c in chars)
    return "[VIN]" if len(chars) == 17 and digits >= 3 and len(chars) - digits >= 3 else m.group(0)


def redact(text: Optional[str]) -> Optional[str]:
    """Replace VINs, plates and owner fields with placeholders (stored text only)."""
    if not text:
        return text
    out = unicodedata.normalize("NFKC", text).translate(_DASHES)
    out = _OWNER_FIELD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}: [DONNEES PERSONNELLES]", out)
    for rx in (_VIN_UPPER, _VIN_ANYCASE):
        out = rx.sub("[VIN]", out)
    out = _VIN_SPACED.sub(
        lambda m: "[VIN]" if (m.group(0).count(" ") <= 3 and sum(c.isdigit() for c in m.group(0)) >= 3
                              and m.group(0).isupper()) else m.group(0),
        out,
    )
    out = _VIN_PUNCT.sub(_punct_vin, out)
    for rx in (_SIV_DASH, _SIV_GLUED, _SIV_UPPER, _SIV_SEP):
        out = rx.sub("[IMMAT]", out)
    out = _FNI_RE.sub("[IMMAT]", out)
    return _redact_cv_holder(out)


def scrub_for_llm(text: Optional[str]) -> Optional[str]:
    """Redact, then also remove links, amounts, phone numbers and e-mail addresses."""
    out = redact(text)
    if not out:
        return out
    out = _URL_RE.sub("[LIEN]", out)
    out = _MONEY_RE.sub("[MONTANT]", out)
    out = _PHONE_RE.sub("[TEL]", out)
    return _EMAIL_RE.sub("[EMAIL]", out)


# A token ending in ".pdf": the end of a document link that the PDF text layer wrapped onto
# the next line ("https://certificate.<provider>/certificate <model>" + "s/<token>.pdf").
_PDF_TOKEN_RE = re.compile(r"(?<!\S)\S*\.pdf\b", re.IGNORECASE)


def redact_document_links(text: Optional[str]) -> Optional[str]:
    """Replace links and wrapped ``….pdf`` link tails with ``[LIEN]``. Idempotent.

    A battery certificate prints the address of its own up-to-date edition, and that PDF
    shows the VIN and the plate unredacted: the link must not survive in stored text.
    """
    if not text:
        return text
    return _PDF_TOKEN_RE.sub("[LIEN]", _URL_RE.sub("[LIEN]", text))


def redact_battery_text(text: Optional[str]) -> Optional[str]:
    """What is stored for a battery certificate: ``redact`` plus the document links removed."""
    return redact_document_links(redact(text))


def normalise_identifier(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Z0-9]", "", value.upper())
    return cleaned or None


MIN_KEY_BYTES = 32


def _decode_key(raw: str, source: str) -> bytes:
    raw = raw.strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = raw.encode("utf-8")
    if len(key) < MIN_KEY_BYTES:
        raise RuntimeError(
            f"The identity key from {source} is too short ({len(key)} bytes, need {MIN_KEY_BYTES}). "
            "Restore the original key, or delete it only if you also start a new database."
        )
    return key


def load_identity_key(key_path: Path) -> bytes:
    """Return the HMAC key from IDENTITY_HMAC_KEY (hex) or a generated, git-ignored file.

    The key must stay stable: changing it makes every stored hash useless for
    recognising relisted cars. The file is created atomically (O_EXCL), so two first
    runs cannot end up with different keys.
    """
    env = os.environ.get("IDENTITY_HMAC_KEY")
    if env:
        return _decode_key(env, "IDENTITY_HMAC_KEY")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        key = secrets.token_bytes(MIN_KEY_BYTES)
        try:
            fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process created it first: read theirs below
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(key.hex())
            return key
    return _decode_key(key_path.read_text(encoding="utf-8"), str(key_path))


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:12]


def identity_hmac(key: bytes, value: Optional[str]) -> Optional[str]:
    normalised = normalise_identifier(value)
    if not normalised:
        return None
    return hmac.new(key, normalised.encode("ascii"), hashlib.sha256).hexdigest()


def check_identity_key_ready(key_path: Path) -> tuple[bytes, str]:
    """Load (or create) the identity key and return it with its short id."""
    key = load_identity_key(key_path)
    return key, key_id(key)


def find_vin(text: Optional[str]) -> Optional[str]:
    """First VIN-shaped token in a text, or None (used to hash, never to store)."""
    if not text:
        return None
    m = _VIN_UPPER.search(unicodedata.normalize("NFKC", text).upper())
    return m.group(0) if m else None
