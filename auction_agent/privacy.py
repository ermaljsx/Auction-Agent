"""Personal-data minimisation: identity hashing and text redaction.

VINs and licence plates can identify a person (the CNIL treats plates as personal
data), so they are never stored in clear text, never displayed and never sent to an
LLM. A keyed HMAC is kept only to recognise the same car when it is relisted. That is
pseudonymisation, not anonymisation, and it is deleted together with the vehicle.
"""
from __future__ import annotations

import hashlib
import hmac
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
# SIV plates (2009+): AB-123-CD, ab-123-cd, AB123CD, AB 123 CD.
_SIV_DASH = re.compile(rf"(?<![A-Za-z0-9])[{_SIV_L}]{{2}}-\d{{3}}-[{_SIV_L}]{{2}}(?![A-Za-z0-9])", re.IGNORECASE)
_SIV_GLUED = re.compile(rf"(?<![A-Za-z0-9])[{_SIV_L}]{{2}}\d{{3}}[{_SIV_L}]{{2}}(?![A-Za-z0-9])", re.IGNORECASE)
_SIV_UPPER = re.compile(rf"(?<![A-Z0-9])[{_SIV_L}]{{2}}[- ]?\d{{3}}[- ]?(?!{_UNITS}\b)[{_SIV_L}]{{2}}(?![A-Z0-9])")
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
_MONEY_RE = re.compile(r"\d[\d   .,]*\s*(?:€|EUR\b|euros?\b)(?:\s*(?:HT|TTC)\b)?", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+33\s?|0)[1-9](?:[\s.\-]?\d{2}){4}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


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
    for rx in (_SIV_DASH, _SIV_GLUED, _SIV_UPPER):
        out = rx.sub("[IMMAT]", out)
    out = _FNI_RE.sub("[IMMAT]", out)
    return out


def scrub_for_llm(text: Optional[str]) -> Optional[str]:
    """Redact, then also remove amounts, phone numbers and e-mail addresses."""
    out = redact(text)
    if not out:
        return out
    out = _MONEY_RE.sub("[MONTANT]", out)
    out = _PHONE_RE.sub("[TEL]", out)
    return _EMAIL_RE.sub("[EMAIL]", out)


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
