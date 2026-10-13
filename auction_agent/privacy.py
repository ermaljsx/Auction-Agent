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
from pathlib import Path
from typing import Optional

# VIN: 17 chars, no I/O/Q. Require at least one digit and one letter to avoid
# swallowing long numeric references (PV numbers, phone numbers).
_VIN_RE = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}\b")
# SIV plates (2009+): AB-123-CD; letters exclude I, O, U.
_SIV_RE = re.compile(r"\b[A-HJ-NP-TV-Z]{2}[- ]?\d{3}[- ]?[A-HJ-NP-TV-Z]{2}\b")
# FNI plates (pre-2009): 1234 AB 75 / 123 ABC 2A / 12 AB 971.
_FNI_RE = re.compile(r"\b\d{1,4}[- ]?[A-Z]{1,3}[- ]?(?:\d{2}|2A|2B|97\d)\b")
# Lines that may carry an owner's identity on inspection reports (layout unverified).
_OWNER_LINE_RE = re.compile(
    r"^\s*(propri[eé]taire|titulaire|nom\b|adresse|demeurant)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)


def redact(text: Optional[str]) -> Optional[str]:
    """Replace VINs, plates and owner lines with placeholders."""
    if not text:
        return text
    out = _OWNER_LINE_RE.sub("[DONNEES PERSONNELLES]", text)
    out = _VIN_RE.sub("[VIN]", out)
    out = _SIV_RE.sub("[IMMAT]", out)
    out = _FNI_RE.sub("[IMMAT]", out)
    return out


def normalise_identifier(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Z0-9]", "", value.upper())
    return cleaned or None


def load_identity_key(key_path: Path) -> bytes:
    """Return the HMAC key from IDENTITY_HMAC_KEY or a generated, git-ignored file.

    The key must stay stable: changing it makes every stored hash useless for
    recognising relisted cars.
    """
    env = os.environ.get("IDENTITY_HMAC_KEY")
    if env:
        return env.encode("utf-8")
    if key_path.exists():
        return bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    key_path.write_text(key.hex(), encoding="utf-8")
    return key


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:12]


def identity_hmac(key: bytes, value: Optional[str]) -> Optional[str]:
    normalised = normalise_identifier(value)
    if not normalised:
        return None
    return hmac.new(key, normalised.encode("ascii"), hashlib.sha256).hexdigest()
