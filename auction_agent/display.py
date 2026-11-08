"""Readable case for car titles and trims, which arrive in capitals from the auction site.

"MINI HATCH 5 PORTES F55 LCI" -> "MINI Hatch 5 portes F55 LCI";
"PURETECH 130CH S&S BVM6 ALLURE" -> "PureTech 130 ch S&S BVM6 Allure".

Rules, word by word (a word is split again on "-", "+" and "/"):
1. a known spelling wins (brands, engine families, a few accented model names, "ch");
2. a word with a digit stays as written, except "130CH" -> "130 ch" and "4X2" -> "4x2";
3. a Roman numeral, a word of three letters or fewer, or a known acronym stays in capitals;
4. any other word gets a capital and lower case.
Search on the site still uses the raw text; this is display only (snapshot ``*_display``).
"""
from __future__ import annotations

import re
from typing import Optional

# Exact spellings, keyed by the capitalised word (accents as stored: the site's data has none).
KNOWN = {
    # Brands
    "CITROEN": "Citroën", "MERCEDES-BENZ": "Mercedes-Benz", "KIA": "Kia", "SKODA": "Škoda", "SEAT": "Seat",
    "MINI": "MINI", "DS": "DS", "BMW": "BMW", "MG": "MG", "BYD": "BYD", "CUPRA": "Cupra",
    # Engines and gearboxes as their makers write them
    "TCE": "TCe", "PURETECH": "PureTech", "BLUEHDI": "BlueHDi", "HDI": "HDi", "CRDI": "CRDi", "DCI": "dCi",
    "GDI": "GDi", "TGDI": "T-GDi", "ECOBLUE": "EcoBlue", "ECOBOOST": "EcoBoost", "MULTIJET": "MultiJet",
    "SKYACTIV": "Skyactiv", "TRONIC": "tronic", "E-EAT8": "e-EAT8", "XDRIVE": "xDrive", "SDRIVE": "sDrive",
    "TIPTRONIC": "Tiptronic", "BLUEMOTION": "BlueMotion", "VTI": "VTi", "THP": "THP", "TSI": "TSI",
    "TFSI": "TFSI", "TDI": "TDI", "MHEV": "MHEV", "PHEV": "PHEV", "HEV": "HEV", "MIVEC": "MIVEC",
    "CDTI": "CDTi", "TDCI": "TDCi", "VTEC": "VTEC", "EDC": "EDC", "DSG": "DSG",
    # Model names that lost their accents
    "MEGANE": "Mégane", "SCENIC": "Scénic", "ZOE": "Zoé", "ELYSEE": "Élysée", "SPACETOURER": "SpaceTourer",
    "SERIE": "Série",
    "DS7": "DS 7", "DS3": "DS 3", "DS4": "DS 4", "DS9": "DS 9",
    # Ordinary words of three letters or fewer that are not acronyms
    "YOU": "You", "AIR": "Air", "ONE": "One", "NEW": "New", "PRO": "Pro", "MAX": "Max", "VAN": "Van",
    "ART": "Art", "ZEN": "Zen", "TOP": "Top", "FUN": "Fun", "RED": "Red", "ANS": "ans", "DUE": "Due",
    "RIO": "Rio", "EOS": "Eos", "KA": "Ka", "SKY": "Sky", "CUP": "Cup", "LUX": "Lux", "DUO": "Duo",
    # Units and small words
    "CH": "ch", "PORTES": "portes", "PLACES": "places",
}
_UNIT = re.compile(r"^(\d+)(CH)$")
_DRIVE = re.compile(r"^(\d)X(\d)$")
_BMW = re.compile(r"^(\d{3})([DEI])$")  # "320E" -> "320e", "520D" -> "520d"
_ROMAN = re.compile(r"^[IVX]+$")
_EDGES = re.compile(r"^([^\w&]*)(.*?)([^\w&]*)$", re.S)


def _word(part: str) -> str:
    m = _EDGES.match(part)
    head, core, tail = (m.group(1), m.group(2), m.group(3)) if m else ("", part, "")
    if not core:
        return part
    upper = core.upper()
    if upper in KNOWN:
        return head + KNOWN[upper] + tail
    if core != upper:  # already in mixed case: the data chose it
        return part
    unit = _UNIT.match(upper)
    if unit:
        return f"{head}{unit.group(1)} ch{tail}"
    drive = _DRIVE.match(upper)
    if drive:
        return f"{head}{drive.group(1)}x{drive.group(2)}{tail}"
    bmw = _BMW.match(upper)
    if bmw:
        return f"{head}{bmw.group(1)}{bmw.group(2).lower()}{tail}"
    letters = sum(1 for c in core if c.isalpha())
    if any(c.isdigit() for c in core) or _ROMAN.match(upper) or letters <= 3:
        return part
    return head + core[0] + core[1:].lower() + tail


def display_case(text: Optional[str]) -> Optional[str]:
    """Readable case for a title or trim; None stays None."""
    if text is None:
        return None
    out = []
    for token in re.split(r"(\s+)", str(text)):
        if not token or token.isspace():
            out.append(token)
            continue
        upper = token.upper()
        if upper in KNOWN:  # whole tokens first: "MERCEDES-BENZ", "E-EAT8"
            out.append(KNOWN[upper])
            continue
        out.append("".join(p if p in "-+/" else _word(p) for p in re.split(r"([-+/])", token)))
    return "".join(out)


def display_title(title: Optional[str]) -> Optional[str]:
    """A card title (brand + model) in readable case, without a brand the model repeats:
    "DS DS7 CROSSBACK" -> "DS 7 Crossback"."""
    if title is None:
        return None
    words = str(title).split()
    if len(words) > 1 and words[0].isalpha() and words[1].upper().startswith(words[0].upper()) \
            and words[1][len(words[0]):].isdigit():
        words = words[1:]
    return display_case(" ".join(words))
