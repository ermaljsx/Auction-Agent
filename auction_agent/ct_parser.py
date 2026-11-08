"""Parser for French Contrôle Technique (CT) procès-verbal text, version 2.1.

Version 2.1 is written against the real Alcopa PVs (see tests/fixtures/ct for synthetic
copies of each layout). Two families of forms exist, each with a voluntary variant:

- ``A``: the labelled two-column PV. A header row ("NATURE DU CONTRÔLE (3) DATE DU
  CONTRÔLE N° DU PROCÈS-VERBAL") carries its values on the next line, and the result sits
  on the line after "(7) RÉSULTAT DU CONTRÔLE". ``A-vol`` is the voluntary form.
- ``B``: the unlabelled pre-printed PV. A "ZA<8 digits>" form number, then the
  nature/date/PV-number line and the result line. ``C`` is the voluntary "CV" form.
- ``generic``: anything else (labelled "Résultat : ..." fields, one column).

What changed versus ct-2.0:

- The PDF text layer merges the two columns line by line, so text from the other column
  (the validity date, "Contre-visite", centre or vehicle fields) can come before a defect
  code or a section header on the same line. Both are now searched anywhere in the line.
  A code has the strict 5-part shape ``<fn>.<n>.<n>.<letter>.<digit>`` and is printed
  with a trailing dot (``4.1.2.a.2.``), which ct-2.0 rejected: it found 0 defects. A
  suffix other than 1/2/3 (L-category PVs print 6 and 7) takes the header's severity and
  flags the reading as uncertain; code-shaped text that is not read (glued, a stray
  space) is flagged too, and a ".3" one is kept as a critical defect.
- Severity comes from the section header and the code suffix. When they disagree the
  more severe one wins, so a critical code is always reported as critical, and confidence
  drops. A code outside any section keeps its suffix severity instead of being dropped.
- Sections no longer end on left-column labels (NATURE, DATE, SIGNATURE...). Codes
  identify themselves; only label continuation stops at right-column end markers.
- Wrapped labels take a continuation line only after known form labels, dates,
  agreement and phone numbers and upper-case-only fragments (names, addresses, make and
  model, owner block) are removed. Long official headings that wrap are completed from
  their known wording, which also drops the centre name or town printed before the rest.
- A result phrase cut by a line break is still read, and a stated critical result wins
  over a field that says otherwise. A PV whose next page is announced but missing is
  flagged uncertain.
- Inspection date, nature and PV layout come from the "<nature> DD/MM/YYYY <PV n°>" line;
  the odometer from the "<km> AVANT ARRIERE" / "<km> G D G D" rows; the validity date
  ("CT : <date>" sticker) and the nature of the next control cross-check the result.
- The odometer history is read as a block, so a "date :" / "km" pair split across two
  lines (or across the page break) is no longer lost, and the current reading joins the
  rollback check. Dates of the vehicle line merged into the block are not readings.

Every result carries a ``parse_confidence``; scoring treats < 0.8 as "CT non lu".
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from collections import Counter
from datetime import date
from typing import Any, Optional

PARSER_VERSION = "ct-2.1"

RESULT_FAVORABLE = "FAVORABLE"
RESULT_MAJOR = "DEFAVORABLE_MAJEURE"
RESULT_CRITICAL = "DEFAVORABLE_CRITIQUE"
RESULT_UNKNOWN = "UNKNOWN"

SEVERITIES = ("minor", "major", "critical")
_SEV_RANK = {"minor": 1, "major": 2, "critical": 3}
_RESULT_RANK = {RESULT_UNKNOWN: 0, RESULT_FAVORABLE: 1, RESULT_MAJOR: 2, RESULT_CRITICAL: 3}
_SUFFIX_SEV = {"1": "minor", "2": "major", "3": "critical"}

SYSTEMS = {
    "0": "identification",
    "1": "freinage",
    "2": "direction",
    "3": "visibilite",
    "4": "eclairage",
    "5": "liaisons_sol",
    "6": "structure",
    "7": "equipements",
    "8": "nuisances",
    "9": "supplementaires",
    "12": "vehicule_ecole",  # driving-school vehicles ("CT Véhicule école")
}
CRITICAL_SYSTEMS = {"freinage", "direction", "structure"}

# --------------------------------------------------------------------------- folding

_FOLD_MAP = {
    **{ord(c): "-" for c in "‐‑‒–—―−"},  # same dashes as privacy.redact
    ord("’"): "'", ord("‘"): "'", ord("ʼ"): "'",
    ord(" "): " ", ord(" "): " ",
    # zero-width characters and soft hyphens, which PDF text layers can put inside a code
    **{ord(c): None for c in "​‌‍⁠﻿­"},
}


def fold(text: str) -> str:
    """Upper-case, strip accents and normalise dashes, quotes and spaces, keeping line breaks.

    Dashes are mapped like ``privacy.redact`` does, so the parser reads the original PDF
    text and the stored (redacted) text the same way.
    """
    decomposed = unicodedata.normalize("NFKD", text.translate(_FOLD_MAP))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).upper()


_CHAR_FOLD: dict[str, str] = {}


def _fold_with_map(line: str) -> tuple[str, list[int]]:
    """Fold one line and map every folded position back to the original one.

    Folding can change lengths ("ß" → "SS", NFD accents vanish), so labels are cut from
    the original text through this map. The map has one extra entry for the end.
    """
    parts: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(line):
        folded = _CHAR_FOLD.get(ch)
        if folded is None:
            folded = fold(ch)
            if len(_CHAR_FOLD) < 4096:
                _CHAR_FOLD[ch] = folded
        parts.append(folded)
        index.extend([i] * len(folded))
    index.append(len(line))
    return "".join(parts), index


# --------------------------------------------------------------------------- patterns (folded text)

# Real codes: <fn>.<n>.<n>.<letter>.<digit>, optionally followed by a dot. The suffix is
# 1/2/3 (minor/major/critical) on the car forms; other digits exist (L-category PVs print
# 6 and 7) and then the section header gives the severity. The left boundary keeps
# "12.2.2.b.2" from being read as "2.2.2.b.2" (a direction defect); letters may touch the
# code on either side, since the PDF text layer sometimes drops the space ("RÉALISÉ1.1.16.c.3.",
# "1.1.16.c.3.CYLINDRES").
_CODE_RE = re.compile(
    r"(?<![\d.])(?P<code>(?P<fn>\d{1,2})\.(?P<l2>\d{1,2})\.(?P<l3>\d{1,2})\.(?P<letter>[A-Z])\.(?P<suffix>\d))"
    r"(?!\d)(?!\.\d)\.?"
)
# Backstop: anything code-shaped, without boundaries and with stray spaces ("2.1.3.b .3.").
# A hit the strict pattern did not extract is reported, and a ".3" hit becomes a critical defect.
_LOOSE_CODE_RE = re.compile(r"(\d{1,2}) ?[.,] ?(\d{1,2}) ?[.,] ?(\d{1,2}) ?[.,] ?([A-Z]) ?[.,] ?(\d)(?!\d)")
# Shorter codes (e.g. 5.2.3, 1.1.13.a) are only trusted at line start and followed by a
# word, as in ct-2.0 (a dotted number in a measurement table is not a code).
_LEGACY_CODE_RE = re.compile(
    r"\s*(?P<code>(?P<fn>\d{1,2})\.\d{1,2}\.\d{1,2}(?:\.\d{1,2})?(?:\.[A-Z])?(?:\.(?P<suffix>[123]))?)"
    r"\.?(?![\d.])(?=[\s:\-)]*[^\W\d_])"
)
# A section header, anywhere in the line, but never the result phrase
# ("Défavorable pour défaillances majeures").
_HEADER_RE = re.compile(
    r"(?<![A-Z])(?<!POUR )(?<!POUR LES )DEFAILLANCES?(?:\(S\))?\s+(?P<sev>MINEURE|MAJEURE|CRITIQUE)(?:S|\(S\))?"
    r"(?![A-Z])"
)
# What may follow a real section header: nothing, a count, or "néant". No two optional
# whitespace runs touch, so a long run of spaces cannot make this backtrack.
_HEADER_REST_OK = re.compile(r"\s*(?:[:\-.]\s*)?(?:(?:\(?\d+\)?|NEANT|AUCUNE?|SANS OBJET)\s*(?:[:\-.]\s*)?)?$")
# Voluntary forms print a legend inside the header: "(Soumis à contre-visite en cas de ...".
_HEADER_LEGEND_RE = re.compile(r"\s*:?\s*\((?:SOUMIS\s+A\s+|PAS\s+DE\s+)?CONTRE[- ]?VISITE\b")
_NONE_RE = re.compile(r"\b(NEANT|AUCUNE?|SANS OBJET)\b")

# "<nature> DD/MM/YYYY <PV number>": the only place the inspection date is printed.
_HEAD_RE = re.compile(
    r"\s*(?P<nat>[A-Z][A-Z0-9 '\-]*?)\s+(?P<d>\d{2})/(?P<m>\d{2})/(?P<y>\d{4})\s+(?P<pv>[A-Z]?\d{6,10})\s*$"
)
_HEAD_NATURE_RE = re.compile(r"CONTROLE|\bCT\b|VISITE|VOLONTAIRE")

# A "Résultat" field: "Résultat (du contrôle) : <value>", the label alone on its line, or
# the layout-A header row "(7) RÉSULTAT DU CONTRÔLE ...". "résultat favorable du test",
# "Légende des résultats" and other prose are not fields.
_RESULT_FIELD_RE = re.compile(
    r"(?<![A-Z])RESULTAT(?:\s+(?:DU|DE|LA|CONTROLE|TECHNIQUE|PERIODIQUE|VOLONTAIRE|GLOBAL|FINAL|VISITE))*"
    r"(?:\s*[:\-]\s*(?P<value>.*)|\s*$)"
)
_RESULT_ROW_RE = re.compile(r"\(7\)\s*RESULTAT\s+DU\s+CONTROLE\b")
_RESULT_LINE_RE = re.compile(
    r"\s*(?:(?P<fav>FAVORABLE)|DEFAVORABLE\s+POUR\s+(?:LES\s+)?DEFAILLANCES?(?:\(S\))?\s+(?P<sev>MAJEURE|CRITIQUE)"
    r"(?:S|\(S\))?)(?![A-Z])(?P<rest>.*)$"
)
_RESULT_REST_OK = re.compile(
    r"\s*[.:]?\s*(?:SUITE\s+DU\s+PROCES[- ]?VERBAL\s*|DEFAILLANCES?\s+(?:MINEURE|MAJEURE|CRITIQUE)S?\s*:.*)?$"
)
_PHRASES = {
    RESULT_CRITICAL: re.compile(r"DEFAVORABLE\s+POUR\s+(?:LES\s+)?DEFAILLANCES?\s+CRITIQUES?"),
    RESULT_MAJOR: re.compile(r"DEFAVORABLE\s+POUR\s+(?:LES\s+)?DEFAILLANCES?\s+MAJEURES?"),
}
# Tear-off slip printed on every layout-A PV, favourable ones included: never a result.
_SLIP_RE = re.compile(r"INFORMATION\s+SUR\s+LE\s+CONTROLE\s+TECHNIQUE\s+DEFAVORABLE")

_DATE_LABEL_RE = re.compile(
    r"DATE\s+(?:DU\s+CONTROLE|DE\s+(?:LA\s+)?VISITE|D'?INSPECTION|DU\s+PASSAGE)\s*[:\-]?\s*"
    r"(\d{2})[/.\-](\d{2})[/.\-](\d{4})"
)
_KM_LABEL_RE = re.compile(
    r"KILOMETRAGE(?:\s+(?:RELEVE|AU\s+COMPTEUR|LU|ACTUEL))?\s*[:\-]?\s*"
    r"(\d{1,3}(?:[ .]\d{3})+|\d{1,7})\s*KM\b"
)
# The current odometer has no unit: "<km> AVANT ARRIERE" (A) or "<km> G D G D" (B).
_KM_ROW_RE = re.compile(r"\s*(\d{1,7})\s+(?:AVANT\s+ARRIERE|G\s+D\s+G\s+D)\b")
# Form B without a brake bench (L-category vehicles): the km column sits in front of the
# first measurement line, "22162 Opacité des fumées ...".
_KM_MEASURE_RE = re.compile(r"\s*(\d{1,7})\s+(?:OPACITE|CO\s+(?:AU\s+)?RALENTI|NIVEAU\s+SONORE|TENEUR\s+EN|LAMBDA)\b")
# Form C: "<VIN> <energy>" then "<km> <model>".
_CV_ENERGY_RE = re.compile(  # the energy: GO, GPL, ES/GP...
    r"\s*(?:\[VIN\]|(?=(?:[A-Z]*\d){3})[A-HJ-NPR-Z0-9]{17})(?:\s+\S+)?\s*$")
_CV_KM_RE = re.compile(r"\s*(\d{1,7})\s+[A-Z0-9]")
_CV_END_RE = re.compile(r"\s*(?:\[IMMAT\]|[A-Z0-9][A-Z0-9 -]{3,9})\s+\(F\)\s+\d{2}/\d{2}/\d{4}")

_STICKER_RE = re.compile(r"(?<![A-Z])CT\s*:\s*(\d{2})/(\d{2})/(\d{4})")
_VALIDITY_LABEL_RE = re.compile(r"(?:VALIDITE|AVANT\s+LE|JUSQU.AU)[^\n\d]{0,25}?(\d{2})/(\d{2})/(\d{4})")
_LIMIT_LABEL_RE = re.compile(r"LIMITE\s+DE\s+VALIDITE")
_LEADING_DATE_RE = re.compile(r"\s*(\d{2})/(\d{2})/(\d{4})(?!\s*:)")
_NEXT_LABEL_RE = re.compile(r"NATURE\s+DU\s+PROCHAIN\s+CONTROLE")
_NEXT_VALUE_RE = re.compile(r"\s*(?:[:\-]\s*)?(CONTRE[- ]?VISITE|CONTROLE\s+TECHNIQUE\s+PERIODIQUE)\b")

_ODO_PAIR_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})[^\n\d]{0,25}?(\d{1,3}(?:[ .]\d{3})+|\d{4,7})\s*KM\b")
# A date with one of these labels is not an odometer reading date, nor is the second date
# of the vehicle line "<plate> (F) <registration date> <first use>".
_NOT_ODO_DATE_RE = re.compile(
    r"(?:(?:IMMATRICULATION|MISE EN CIRCULATION|PROCHAIN|VALIDITE|AVANT LE|JUSQU)[^\n]{0,15}"
    r"|\(F\)(?:\s+\d{2}/\d{2}/\d{4})?\s*)$"
)
_HISTORY_START_RE = re.compile(r"KILOMETRAGES?\s+RELEVES?\s+LORS\s+DES\s+PRECEDENTS|HISTORIQUE\s+DES\s+RELEVES")
_HISTORY_KEEP_RE = re.compile(r"\d{2}/\d{2}/\d{4}|\d\s*KM\b|DEPUIS\s+LE")
_HISTORY_STOP_RE = re.compile(r"AFIN\s+DE\s+FACILITER|VOTRE\s+VEHICULE|KILOMETRAGE\s+RELEVE\s+MESURES|AVANT\s+ARRIERE")
# Inside the history block: each "date :" goes with the first "<n> km" after it.
_HISTORY_PAIR_RE = re.compile(
    r"(?<!\d)(\d{2})/(\d{2})/(\d{4})\s*:\s*(?:(?!\d{2}/\d{2}/\d{4})[^/])*?"
    r"(?<![\d.])(\d{1,3}(?:[ .]\d{3})+|\d{1,7})\s*KM\b"
)
_COHERENCE_NOT_DONE_RE = re.compile(r"CONTROLE\s+DE\s+COHERENCE\s+DU\s+KILOMETRAGE[\s\S]{0,200}?NON\s+REALISE")

_OBD_INTRO_RE = re.compile(r"CODE\(S\)\s+DEFA")
_OBD_CODE_RE = re.compile(r"\bP[0-9A-F]{4}\b")

# Lines that belong to the page frame: skipped without touching the section or the label.
_FURNITURE_RE = re.compile(
    r"\s*(?:ZA\d{8}|N\d{2}\.\d{2}|V\d{6,12}|PROCES[- ]VERBAL\s+AUTO|DE\s+CONTROLE\s+(?:TECHNIQUE|VOLONTAIRE)\s+SECURITE"
    r"|D.UN\s+VEHICULE\s+AUTOMOBILE|EXEMPLAIRE\s+CONSERVE\s+PAR\s+LE\s+CENTRE|N.\s*D.\s*IMPRIME\b.*"
    r"|NATURE\s+DU\s+CONTROLE\s+\(3\).*|TYPE\s+DE\s+CONTROLE\s+VOLONTAIRE.*|\(7\)\s*RESULTAT\s+DU\s+CONTROLE.*"
    r"|FONCTIONS\s+CONTROLEES.*|ATTENTION,?\s+CE\s+PROCES[- ]?VERBAL\s+CONTIENT.*)\s*$"
)
_GLYPH_RE = re.compile(r"\s*Ò.*Ó\s*$")  # mangled barcode font: "Ò....Ó"
# ct-2.0 page furniture: it ends the current label (not the section).
_PAGE_FURNITURE_RE = re.compile(r"\s*(?:PAGE\s+\d+(?:\s*/\s*\d+)?|PROCES[- ]VERBAL\b.*|-?\s*\d+\s*/\s*\d+\s*-?)\s*$")
_SUITE_RE = re.compile(r"SUITE\s+DU\s+PROCES[- ]?VERBAL")
_PAUSE_RE = re.compile(r"ATTENTION,?\s+IL\s+EXISTE\s+UNE\s+SUITE")
_PAGES_RE = re.compile(r"CONTIENT\s+(\d{1,2})\s+PAGES")

# Right-column end markers: label continuation stops (the section does not).
_STOP_ANY_RE = re.compile(
    r"KILOMETRAGES?\s+RELEVES?\s+LORS\s+DES\s+PRECEDENTS|KILOMETRAGE\s+RELEVE\s+MESURES"
    r"|CONTROLE\s+DES\s+EMISSIONS\s+A\s+L.ECHAPPEMENT\s+REALISE|CONTROLE\s+DE\s+COHERENCE\s+DU\s+KILOMETRAGE"
    r"|AFIN\s+DE\s+FACILITER\s+LES\s+CAMPAGNES|VOTRE\s+VEHICULE\s+EST\s+CONCERNE|VERIFICATION\s+NON\s+REALISEE"
    r"|ETAT\s+D.UN\s+OU\s+DE\s+PLUSIEURS\s+PNEUMATIQUES|INFORMATION\s+SUR\s+LE\s+CONTROLE\s+TECHNIQUE"
    r"|HISTORIQUE\s+DES\s+RELEVES|RIPAGE\s*\(|DISSYMETRIE\s+SUSPENSION|FORCES?\s+VERTICALES|FORCES?\s+DE\s+FREINAGE"
    r"|TAUX\s+D.EFFICACITE|DESEQUILIBRE\s*\(|OPACITE\s+DES\s+FUMEES|FEUX\s+DE\s+CROISEMENT\s+\(-"
    r"|NIVEAU\s+SONORE\s*\(|CO\s+(?:AU\s+)?RALENTI\s*\("
)
# The measurement block title of form B ("Emissions à l'échappement", capitalised, alone).
_MEASURE_TITLE_RE = re.compile(r"\s*(?:CT\s*:\s*\d{2}/\d{2}/\d{4}\s+)?[EÉ]missions à l.échappement\s*$")
_STOP_LINE_RE = re.compile(r"\s*(?:\d{1,7}\s+)?(?:AVANT\s+ARRIERE|G\s+D\s+G\s+D)\s*$")
_STOP_START_RE = re.compile(
    r"\s*(?:PROCHAIN|CENTRE\b|VALIDITE|RESULTAT|OBSERVATIONS?\b|COMMENTAIRES?\b|LES\s+DEFAILLANCES|CONTROLEUR"
    r"|LEGENDE|SIGNATURE\s+DU|KILOMETRAGE\b|N.\s*D.?IDENTIFICATION"
    r"|CONTRE[- ]?VISITE\s+(?:A\s+EFFECTUER|A\s+REALISER|AVANT|OBLIGATOIRE|SOUS)"
    r"|DATE\s+(?:DU|DE\s+(?:LA\s+)?(?:VISITE|VALIDITE|PROCHAIN)))"
)
# Left-column fields merged at the start of a line. They are generic form labels (never
# a centre's own name or address), so they are safe to strip before reading the rest.
_LEFT_PREFIX_RES = [re.compile(p) for p in (
    r"\(8\)\s*LIMITE\s+DE\s+VALIDITE\s+DU\s+CONTROLE\s+REALISE",
    r"NATURE\s+DU\s+PROCHAIN\s+CONTROLE",
    r"IDENTIFICATION\s+DU\s+(?:CENTRE\s+DE\s+CONTROLE|VEHICULE)",
    r"N.\s*D.\s*AGREMENT(?:\s+DU\s+CENTRE)?\s*:(?:\s*[A-Z]?\d{2,3}[A-Z]\d{3,4}\b)?",
    r"\(9\)\s*RAISON\s+SOCIALE\s*:",
    r"\(3\)\s*COORDONNEES\s*:",
    r"\(9\)\s*IDENTITE\s+DU\s+CONTROLEUR",
    r"SIGNATURE\s*:",
    r"DATE\s+DE\s+1\s*(?:ERE|RE|ER)\s+MISE(?:\s+EN\s+CIRCULATION)?",
    r"\(2\)\s*IMMATRICULATION\s+ET\s+PAYS(?:\s+DATE\s+D.IMMATRICULATION)?(?:\s+EN\s+CIRCULATION)?",
    r"\(1\)\s*N.\s*DANS\s+LA\s+SERIE\s+DU\s+TYPE(?:\s*\(VIN\))?(?:\s*\(5\)\s*CATEGORIE\s+INTERNATIONALE)?(?:\s+GENRE)?",
    r"MARQUE\s+DESIGNATION\s+COMMERCIALE",
    r"TYPE\s*/\s*CNIT\s+ENERGIE",
    r"DOCUMENT\(S\)\s+PRESENTE\(S\)",
    r"(?:(?:PHOTO)?COPIE\s+DU\s+)?CERTIFICAT\s+D.IMMATRICULATION(?:\s+(?:PRESENTEE?|ORIGINAL"
    r"|VISEE?\s+PAR\s+UN(?:\s+COMMISSAIRE-PRISEUR)?))?|CARTE\s+GRISE",
    r"(?:COMMISSAIRE-PRISEUR\s+)?OU\s+UN\s+HUISSIER\s+DE\s+JUSTICE",
    r"CONTRE[- ]?VISITE(?=\s|$)",
    r"CONTROLE\s+TECHNIQUE\s+PERIODIQUE",
    r"NON\s+RENSEIGNES?(?=\s|$)",
    r"\d{2}/\d{2}/\d{4}(?!\s*:)",
    r"[A-Z]?\d{2,3}[A-Z]\d{3,4}\b",  # centre / controller agreement number
    r"TEL\s*:\s*(?:\+33\s?|0)\d(?:[ .\-]?\d{2}){4}(?!\d)",  # a phone number, not the date after it
    r"(?:\[IMMAT\]|[A-Z0-9][A-Z0-9 -]{3,9})\s+\(F\)(?:\s+\d{2}/\d{2}/\d{4}){0,2}",
    # "<VIN> <category> <genre>": "[VIN] M1 VP", "[VIN] L6e-BP QM"
    r"(?:\[VIN\]|[A-HJ-NPR-Z0-9]{17})(?:\s+[A-Z]\d[A-Z]?(?:-[A-Z]{1,3})?\s+[A-Z]{2,4})?(?=\s|$)",
    # a street ("12 RUE DES <NAME>"), e.g. the centre address merged before a wrapped heading
    r"\d{1,4}(?:\s*(?:BIS|TER))?\s+(?:RUE|AVENUE|AV|BD|BOULEVARD|CHEMIN|ROUTE|RTE|IMPASSE|ALLEE|PLACE|QUAI|COURS"
    r"|ZA|ZI|ZAC|RN|RD)\.?\s+(?:(?:DE\s+LA|DE\s+L'|DU|DES|DE|D'|LA|LE|LES|L')\s*)?[A-Z][A-Z'\-]*",
)]
# The "FONCTIONS CONTRÔLÉES" column of the voluntary form A-vol (fixed form text).
_AVOL_PREFIX_RES = [re.compile(p) for p in (
    r"IDENTIFICATION\s+DU\s+VEHICULE,\s*EQUIPEMENTS(?=\s|$)",
    r"DE\s+FREINAGE,\s*DIRECTION,\s*VISIBILITE,\s*FEUX,",
    r"DISPOSITIFS\s+REFLECHISSANTS\s+ET(?=\s|$)",
    r"EQUIPEMENTS\s+ELECTRIQUES,\s*ESSIEUX,",
    r"ROUES,\s*PNEUS,\s*SUSPENSION,\s*CHASSIS\s+ET(?=\s|$)",
    r"ACCESSOIRES\s+DU\s+CHASSIS,\s*AUTRE\s+MATERIEL(?=\s|$)",
)]
_LOC = r"(?:AVG|AVD|ARG|ARD|AV|AR|G|D|C)"
_LOC2 = r"(?:AVG|AVD|ARG|ARD|AV|AR)"
# A location list at the end of a line, after another column's name or address:
# "(AVG, AVD)", "AVG, AVD, ARG", "ARD". A bare single letter is too ambiguous ("CLASSE C").
_LOCATION_TAIL_RE = re.compile(
    rf"(?:^|\s)(?P<loc>\(\s*{_LOC}(?:\s*,\s*{_LOC})*\s*\)|{_LOC}(?:\s*,\s*{_LOC})+|{_LOC2})\s*$"
)
_LOCATION_END_RE = re.compile(rf"\(\s*{_LOC}(?:\s*,\s*{_LOC})*\s*\)\s*$")
# Long official headings that the forms wrap over two or three lines (folded). When a label
# stops inside one of them, the next line is searched for the rest of the heading, which
# also drops whatever the other column printed in front of it (a centre name, a town).
_WRAPPED_HEADINGS = (
    "NUMERO D'IDENTIFICATION, DE CHASSIS OU DE SERIE DU VEHICULE",
    "DISPOSITIF DE FREINAGE ASSISTE MAITRE-CYLINDRE (SYSTEMES HYDRAULIQUES)",
    *(f"{kind} (FEUX DE POSITION AVANT, ARRIERE ET LATERAUX, FEUX DE GABARIT, FEUX D'ENCOMBREMENT ET FEUX DE JOUR)"
      for kind in ("ETAT ET FONCTIONNEMENT", "COMMUTATION", "CONFORMITE AVEC LES EXIGENCES")),
    *(f"{kind} (INDICATEURS DE DIRECTION ET FEUX DE SIGNAL DE DETRESSE)"
      for kind in ("ETAT ET FONCTIONNEMENT", "COMMUTATION", "CONFORMITE AVEC LES EXIGENCES")),
    *(f"{kind} (FEUX DE BROUILLARD AVANT ET ARRIERE)"
      for kind in ("ETAT ET FONCTIONNEMENT", "COMMUTATION", "CONFORMITE AVEC LES EXIGENCES")),
    *(f"{kind} (DISPOSITIF D'ECLAIRAGE DE LA PLAQUE D'IMMATRICULATION ARRIERE)"
      for kind in ("ETAT ET FONCTIONNEMENT", "CONFORMITE AVEC LES EXIGENCES")),
    *(f"{kind} (CATADIOPTRES, MARQUAGE DE VISIBILITE REFLECHISSANT ET PLAQUES REFLECHISSANTES ARRIERE)"
      for kind in ("ETAT", "CONFORMITE AVEC LES EXIGENCES")),
    *(f"{kind} (TEMOINS OBLIGATOIRES POUR LE SYSTEME D'ECLAIRAGE)"
      for kind in ("ETAT ET FONCTIONNEMENT", "CONFORMITE AVEC LES EXIGENCES")),
    "TUBES DE POUSSEE, JAMBES DE FORCE, TRIANGLES ET BRAS DE SUSPENSION",
    "PARE-CHOCS, PROTECTION LATERALE ET DISPOSITIFS ANTI-ENCASTREMENT ARRIERE",
    "AUTRES EQUIPEMENTS ET AMENAGEMENTS INTERIEURS ET EXTERIEURS",
    "EQUIPEMENTS DE REDUCTION DES EMISSIONS A L'ECHAPPEMENT POUR MOTEUR A ALLUMAGE COMMANDE",
    "EQUIPEMENTS DE REDUCTION DES EMISSIONS A L'ECHAPPEMENT POUR MOTEUR A ALLUMAGE PAR COMPRESSION",
)
_LOCATION_ONLY_RE = re.compile(r"\s*:?\s*\(?\s*(?:AVG|AVD|ARG|ARD|AV|AR|G|D|C)(?:\s*,\s*(?:AVG|AVD|ARG|ARD|AV|AR|G|D|C))*\s*\)?\s*$")
_LABEL_PHONE_RE = re.compile(r"(?:TEL\s*:?\s*)?(?<!\d)(?:\+33\s?|0)[1-9](?:[\s.\-]?\d{2}){4}(?!\d)", re.IGNORECASE)
_LABEL_AGREMENT_RE = re.compile(r"\b[A-Z]?\d{2,3}[A-Z]\d{3,4}\b")

_OBD_ONLY_RE = re.compile(r"\s*P[0-9A-F]{4}(?:\s*[,;]?\s*P[0-9A-F]{4})*\s*[,;.]?\s*$")
_SPACE_RUN_RE = re.compile(r"[^\S\n]{2,}")


# --------------------------------------------------------------------------- small helpers

def _severity_from_header(word: str) -> str:
    if word.startswith("MINEURE"):
        return "minor"
    if word.startswith("MAJEURE"):
        return "major"
    return "critical"


def _to_int(token: str) -> int:
    return int(re.sub(r"[ .]", "", token))


def _date(d: str, m: str, y: str) -> Optional[str]:
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def _nature_from(scope: str) -> Optional[str]:
    if "VOLONTAIRE" in scope:
        return "volontaire"
    if re.search(r"CONTRE[- ]?VISITE", scope):
        return "contre_visite"
    if "PERIODIQUE" in scope:
        return "periodique"
    if re.search(r"VEHICULE\s+ECOLE|AUTO[- ]?ECOLE", scope):
        return "vehicule_ecole"
    return None


def _head_match(line: str) -> Optional[re.Match]:
    m = _HEAD_RE.match(line)
    if m and _HEAD_NATURE_RE.search(m.group("nat")):
        return m
    return None


def _strict_result(line: str) -> Optional[str]:
    """The result when ``line`` is a result line and nothing else."""
    m = _RESULT_LINE_RE.match(line)
    if not m or not _RESULT_REST_OK.match(m.group("rest")):
        return None
    if m.group("fav"):
        return RESULT_FAVORABLE
    return RESULT_CRITICAL if m.group("sev") == "CRITIQUE" else RESULT_MAJOR


def _next_nonempty(lines: list[str], i: int) -> Optional[int]:
    for j in range(i + 1, len(lines)):
        if lines[j].strip():
            return j
    return None


def _skip_left_prefixes(line: str, extra: tuple = ()) -> int:
    """Index after the left-column field labels/values that start ``line``."""
    pos = 0
    while True:
        while pos < len(line) and line[pos] == " ":
            pos += 1
        for rx in (*extra, *_LEFT_PREFIX_RES):
            m = rx.match(line, pos)
            if m and m.end() > pos:
                pos = m.end()
                break
        else:
            return pos


def _heading_rest_at(label: str, line: str) -> Optional[int]:
    """Where ``line`` continues a known wrapped heading that ``label`` stops inside.

    ``label`` is the defect text so far (original case), ``line`` the folded next line.
    Returns the position in ``line`` where the rest of the heading starts (skipping the
    other column's text in front of it), or None.
    """
    if ":" in label or len(label) < 8:
        return None  # the heading is complete (the description has started), or too short
    head = re.sub(r"\s+", " ", fold(label)).strip()
    rests = []
    for phrase in _WRAPPED_HEADINGS:
        if phrase.startswith(head) and len(phrase) > len(head):
            rest = phrase[len(head):]
            if not (head.endswith("'") or rest.startswith(" ")):
                continue  # the label stops inside a word that is not this heading's
            rests.append(rest.strip())
    if not rests:
        return None
    starts = [0] + [m.end() for m in re.finditer(r" ", line)]
    for q in starts:
        seg = line[q:].rstrip()
        for rest in rests:
            if seg.startswith(rest) and (len(seg) == len(rest) or seg[len(rest)] in " :,"):
                return q  # the whole rest of the heading, then the description
            if len(seg) >= 8 and rest.startswith(seg) and rest[len(seg)] in " ,":
                return q  # part of the rest: the heading wraps again
    return None


def _scrub_label(label: str) -> str:
    label = _LABEL_PHONE_RE.sub("", label)
    label = _LABEL_AGREMENT_RE.sub("", label)
    return re.sub(r"\s{2,}", " ", label).strip()


def _code_key(m: re.Match) -> tuple:
    try:
        return (int(m.group("fn")), int(m.group("l2")), int(m.group("l3")), m.group("letter"))
    except (IndexError, TypeError, ValueError):
        return ()


def _empty_summary() -> dict[str, Any]:
    return {
        "parser_version": PARSER_VERSION,
        "layout": "generic",
        "result": RESULT_UNKNOWN,
        "result_source": "none",
        "nature": "unknown",
        "inspection_date": None,
        "inspection_km": None,
        "validity_date": None,
        "next_control": None,
        "defects": [],
        "counts": {s: 0 for s in SEVERITIES},
        "odometer": [],
        "odometer_history": "absent",
        "odometer_inconsistent": False,
        "suffix_disagreements": 0,
        "unsectioned_codes": 0,
        "result_conflict": False,
        "warnings": [],
        "parse_confidence": 0.0,
    }


# --------------------------------------------------------------------------- line events

def _line_events(line: str) -> list[tuple]:
    """Section headers and defect codes in one folded line, in reading order.

    Header events are ``("header", start, end, severity_or_None)``; code events are
    ``("code", start, end, match)``.
    """
    events: list[tuple] = []
    codes = list(_CODE_RE.finditer(line))
    for m in _HEADER_RE.finditer(line):
        rest = line[m.end():]
        stripped = rest.lstrip(" :-")
        inline = _CODE_RE.match(stripped) or _LEGACY_CODE_RE.match(stripped)
        legend = _HEADER_LEGEND_RE.match(rest)
        if not (_HEADER_REST_OK.match(rest) or legend or inline):
            continue  # a legend sentence such as "Défaillances majeures : contre-visite ..."
        severity: Optional[str] = _severity_from_header(m.group("sev"))
        if _NONE_RE.search(rest) and not inline and not legend:
            severity = None  # "Défaillances critiques : néant"
        events.append(("header", m.start(), m.end(), severity))
        if inline and not any(c.start() >= m.end() for c in codes):
            offset = m.end() + (len(rest) - len(stripped))
            legacy = _LEGACY_CODE_RE.match(line, offset)
            if legacy:
                events.append(("code", legacy.start("code"), legacy.end(), legacy))
    events.extend(("code", c.start(), c.end(), c) for c in codes)
    if not events:
        legacy = _LEGACY_CODE_RE.match(line)
        if legacy:
            events.append(("code", legacy.start("code"), legacy.end(), legacy))
    events.sort(key=lambda e: e[1])
    return events


def _detect_layout(lines: list[str], head_idx: Optional[int]) -> str:
    top = "\n".join(lines[:8])
    first = next((line.strip() for line in lines if line.strip()), "")
    if re.search(r"N.\s*D.\s*IMPRIME\s+CV\b", top):
        return "C"
    if re.search(r"DE\s+CONTROLE\s+VOLONTAIRE\s+SECURITE|TYPE\s+DE\s+CONTROLE\s+VOLONTAIRE", top):
        return "A-vol"
    if re.search(r"NATURE\s+DU\s+CONTROLE\s+\(3\)", top) or (
        "PROCES-VERBAL AUTO" in top and "DE CONTROLE TECHNIQUE SECURITE" in top
    ):
        return "A"
    if re.fullmatch(r"ZA\d{8}", first) and head_idx is not None and head_idx <= 3:
        return "B"
    return "generic"


# --------------------------------------------------------------------------- result

# A result phrase cut by a line break: "Défavorable pour" / "défaillances critiques".
_PHRASE_WRAP_RE = re.compile(r"DEFAVORABLE(?:\s+(?:POUR|AVEC))?(?:\s+LES)?(?:\s+DEFAILLANCES?(?:\(S\))?)?\s*[:\-(]?\s*$")


def _with_wrap(lines: list[str], i: int) -> str:
    """``lines[i]``, joined with the next non-empty line when it ends inside a result phrase."""
    line = lines[i]
    if _PHRASE_WRAP_RE.search(line):
        j = _next_nonempty(lines, i)
        if j is not None:
            return line + " " + lines[j].strip()
    return line


def _value_result(value: str) -> tuple[Optional[str], bool]:
    """(result, is_legend) read from a field value: DEFAVORABLE plus a severity word."""
    value = value[:120]
    has_fav = re.search(r"\bFAVORABLE\b", value) is not None
    has_def = "DEFAVORABLE" in value
    if (has_fav and has_def) or ("MAJEURE" in value and "CRITIQUE" in value):
        return None, True  # a legend listing every possible result, not the result itself
    if has_def:
        if "CRITIQUE" in value:
            return RESULT_CRITICAL, False
        if "MAJEURE" in value:
            return RESULT_MAJOR, False
        return None, False
    if has_fav:
        return RESULT_FAVORABLE, False
    return None, False


def _result_from_field(lines: list[str]) -> tuple[Optional[str], Optional[int]]:
    """Result written in (or right after) a "Résultat" field, and the line it was read on."""
    for i, line in enumerate(lines):
        row = _RESULT_ROW_RE.search(line)
        m = None if row else _RESULT_FIELD_RE.search(line)
        if not (row or m):
            continue
        value = (m.group("value") or "") if m else ""
        if value.strip():
            result, legend = _value_result(_with_wrap(lines, i)[m.start("value"):])
            if legend:
                continue
            if result:
                return result, i
            if "DEFAVORABLE" in value:
                return None, None  # unfavourable but severity unreadable: decide from defects
        # The label is a header row (layout A) or stands alone: the result is on the next
        # line. Only that one line is read: a wider window would reach the section headers.
        j = _next_nonempty(lines, i)
        if j is None:
            continue
        result = _strict_result(lines[j])
        if result:
            return result, j
        nxt, legend = _value_result(_with_wrap(lines, j))
        if nxt in (RESULT_MAJOR, RESULT_CRITICAL) and not legend:
            return nxt, j  # "Défavorable (défaillances critiques)", "DÉFAVORABLE CRITIQUE"...
    return None, None


def _phrase_results(lines: list[str]) -> set[str]:
    """Results stated by a "Défavorable pour défaillances ..." phrase, legend lines excluded."""
    found: set[str] = set()
    for i in range(len(lines)):
        line = _with_wrap(lines, i)
        hits = {r for r, rx in _PHRASES.items() if rx.search(line)}
        if not hits:
            continue
        has_fav = re.search(r"\bFAVORABLE\b", line) is not None
        if len(hits) > 1 or has_fav:
            continue  # a legend line
        found |= hits
    return found


def _validity_date(lines: list[str], layout: str, result_idx: Optional[int]) -> Optional[str]:
    if layout in ("A", "B"):  # the windscreen sticker "CT : <date>" of the known forms
        for line in lines:
            m = _STICKER_RE.search(line)
            if m:
                iso = _date(*m.groups())
                if iso:
                    return iso
    for i, line in enumerate(lines):
        if _LIMIT_LABEL_RE.search(line):
            for nxt in lines[i + 1:i + 4]:
                m = _LEADING_DATE_RE.match(nxt)
                if m:
                    return _date(*m.groups())
            break
    if layout == "B" and result_idx is not None:
        for nxt in lines[result_idx + 1:result_idx + 7]:
            m = _LEADING_DATE_RE.match(nxt)
            if m:
                return _date(*m.groups())
    if layout == "generic":
        for line in lines:
            m = _VALIDITY_LABEL_RE.search(line)
            if m:
                return _date(*m.groups())
    return None


def _next_control(lines: list[str], layout: str, result_idx: Optional[int]) -> Optional[str]:
    def value(line: str) -> Optional[str]:
        if _head_match(line):
            return None
        m = _NEXT_VALUE_RE.match(line)
        if not m:
            return None
        return "contre_visite" if "VISITE" in m.group(1) else "periodique"

    for i, line in enumerate(lines):
        m = _NEXT_LABEL_RE.search(line)
        if m:
            for candidate in [line[m.end():]] + lines[i + 1:i + 4]:
                found = value(candidate)
                if found:
                    return found
            break
    if layout == "B" and result_idx is not None:
        for line in lines[result_idx + 1:]:
            found = value(line)
            if found:
                return found
    return None


def _implied_by_validity(insp: Optional[str], validity: Optional[str]) -> Optional[str]:
    """The result the validity period encodes (same day / ~2 months / 1-2 years)."""
    if not insp or not validity:
        return None
    delta = (date.fromisoformat(validity) - date.fromisoformat(insp)).days
    if delta < 0 or delta > 800:
        return None
    if delta <= 1:
        return RESULT_CRITICAL
    if delta <= 75:
        return RESULT_MAJOR
    if delta >= 300:
        return RESULT_FAVORABLE
    return None


# --------------------------------------------------------------------------- odometer

def _history_pairs(lines: list[str]) -> tuple[list[tuple[str, int]], set[int]]:
    """Pairs from the "Kilométrages relevés..." block, joined across wrapped lines.

    Also returns the indices of the lines read as part of the block. A block cut by the
    end of page 1 ("Attention il existe une suite") resumes after "Suite du procès-verbal".
    """
    pairs: list[tuple[str, int]] = []
    used: set[int] = set()  # lines of the blocks that use the "date : km" form
    for i, line in enumerate(lines):
        m = _HISTORY_START_RE.search(line)
        if not m:
            continue
        block = {i}
        parts = [line[m.end():]]
        misses = taken = 0
        j = i + 1
        while j < len(lines) and taken < 9:
            nxt = lines[j]
            if _HISTORY_START_RE.search(nxt):
                break
            if _PAUSE_RE.search(nxt):
                k = next((k for k in range(j + 1, len(lines)) if _SUITE_RE.search(lines[k])), None)
                if k is None:
                    break
                j, misses = k + 1, 0
                continue
            cleaned = nxt[_skip_left_prefixes(nxt):]
            if _HISTORY_STOP_RE.search(cleaned):
                break
            taken += 1
            if _HISTORY_KEEP_RE.search(cleaned):
                parts.append(cleaned)
                block.add(j)
                misses = 0
            else:
                misses += 1
                if misses > 2:
                    break
            j += 1
        found = False
        for p in _HISTORY_PAIR_RE.finditer(" ".join(parts)):
            iso = _date(*p.groups()[:3])
            km = _to_int(p.group(4))
            if iso and km < 2_000_000:
                pairs.append((iso, km))
                found = True
        if found:
            used |= block
    return pairs, used


# --------------------------------------------------------------------------- main entry point

def parse_controle_technique(text: Optional[str]) -> dict[str, Any]:
    """Return a structured summary of a CT report. Never raises on odd input."""
    summary = _empty_summary()
    if not text or len(text.strip()) < 40:
        return summary
    try:
        _parse_into(summary, text)
    except Exception as exc:  # defensive: an odd text must never break an ingest or a rescore
        summary = _empty_summary()
        summary["warnings"] = [f"parse_error:{exc.__class__.__name__}"]
    return summary


def _parse_into(summary: dict[str, Any], text: str) -> None:
    # Runs of spaces carry no meaning here (the PDF text layer already collapses them),
    # and collapsing them keeps every pattern linear on odd input.
    original_lines = [_SPACE_RUN_RE.sub(" ", line) for line in text.splitlines()]
    folded_maps = [_fold_with_map(line) for line in original_lines]
    lines = [f for f, _ in folded_maps]
    folded = "\n".join(lines)
    warnings: list[str] = summary["warnings"]

    head_idx: Optional[int] = None
    head: Optional[re.Match] = None
    for i, line in enumerate(lines[:15]):
        head = _head_match(line)
        if head:
            head_idx = i
            break
    layout = _detect_layout(lines, head_idx)
    summary["layout"] = layout

    # --- nature and inspection date: the "<nature> date PV" line, else labelled fields
    nature = _nature_from(head.group("nat")) if head else None
    if head and nature is None:
        nature = "other"
    if nature is None:
        label = re.search(r"NATURE\s+DU\s+CONTROLE\s*[:\-]?\s*([^\n]{0,80})", folded)
        if label:
            nature = _nature_from(label.group(1))
    if nature is None:
        if "VOLONTAIRE" in folded:
            nature = "volontaire"
        elif "PERIODIQUE" in folded:
            nature = "periodique"
    summary["nature"] = nature or "unknown"

    if head:
        summary["inspection_date"] = _date(head.group("d"), head.group("m"), head.group("y"))
    if summary["inspection_date"] is None:
        m = _DATE_LABEL_RE.search(folded)
        if m:
            summary["inspection_date"] = _date(*m.groups())

    # --- inspection km
    km: Optional[int] = None
    for line in lines:
        m = _KM_ROW_RE.match(line)
        if m:
            km = int(m.group(1))
            break
    if km is None and layout == "C":
        for i, line in enumerate(lines[:-1]):
            if _CV_ENERGY_RE.match(line):
                m = _CV_KM_RE.match(lines[i + 1])
                if m:
                    km = int(m.group(1))
                    break
    if km is None and layout == "B":
        for line in lines:
            m = _KM_MEASURE_RE.match(line)
            if m:
                km = int(m.group(1))
                break
    if km is None:
        m = _KM_LABEL_RE.search(folded)
        if m:
            km = _to_int(m.group(1))
    if km is not None and 0 < km < 2_000_000:
        summary["inspection_km"] = km

    # --- defects, by section
    events_by_line = [_line_events(line) for line in lines]
    has_headers = any(e[0] == "header" for events in events_by_line for e in events)
    current: Optional[str] = None
    last_defect: Optional[dict[str, Any]] = None
    paused: Optional[dict[str, Any]] = None
    last_key: tuple = ()
    skipped_codes = 0
    order_breaks = 0
    other_suffix = 0  # 5-part codes whose suffix is not 1/2/3: severity from the header only
    legacy_codes = 0  # shorter codes, read at line start only
    obd_target: Optional[dict[str, Any]] = None
    obd_lines = 0
    extra_prefixes = tuple(_AVOL_PREFIX_RES) if layout == "A-vol" else ()

    def add_defect(code: re.Match, label: str) -> Optional[dict[str, Any]]:
        nonlocal skipped_codes, last_key, order_breaks, other_suffix, legacy_codes
        suffix = code.group("suffix") or ""
        suffix_sev = _SUFFIX_SEV.get(suffix)
        if code.re is _CODE_RE and not suffix_sev:
            other_suffix += 1
        elif code.re is not _CODE_RE:
            legacy_codes += 1
        if current:
            severity = current
            if suffix_sev and suffix_sev != current:
                summary["suffix_disagreements"] += 1
                if _SEV_RANK[suffix_sev] > _SEV_RANK[current]:
                    severity = suffix_sev  # never report a code below its own severity
        elif suffix_sev:
            severity = suffix_sev
            if has_headers:
                summary["unsectioned_codes"] += 1
        else:
            if has_headers:
                skipped_codes += 1
            return None
        key = _code_key(code)
        if key and last_key and key < last_key:
            order_breaks += 1
        last_key = key
        defect = {
            "code": code.group("code").lower(),
            "severity": severity,
            "system": SYSTEMS.get(code.group("fn"), "autre"),
            "label": label.lstrip(" -:.)\t").rstrip()[:300],
        }
        summary["defects"].append(defect)
        summary["counts"][severity] += 1
        return defect

    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        orig, fmap = original_lines[idx], folded_maps[idx][1]
        events = events_by_line[idx]
        if events:
            for n, event in enumerate(events):
                if event[0] == "header":
                    current = event[3]
                    last_defect = None
                    last_key = ()
                    continue
                end = len(line) if n + 1 == len(events) else events[n + 1][1]
                label = orig[fmap[event[2]]:fmap[end]]
                last_defect = add_defect(event[3], label)
                if last_defect is not None and layout in ("B", "C") \
                        and _LOCATION_END_RE.search(last_defect["label"]):
                    last_defect = None  # complete on its own line: "... (AVD, AVG)"
            obd_target = None
            if _PAUSE_RE.search(line):
                paused, last_defect = last_defect, None
            continue
        if _GLYPH_RE.match(unicodedata.normalize("NFC", orig)) or _FURNITURE_RE.match(line) \
                or _head_match(line) or _strict_result(line):
            if _SUITE_RE.search(line) and paused is not None:
                last_defect, paused = paused, None  # page 2 continues the last page-1 label
            continue
        if _PAGE_FURNITURE_RE.match(line):
            last_defect = None
            continue
        if _PAUSE_RE.search(line):
            paused, last_defect = (last_defect or paused), None
            continue
        if layout == "C" and _CV_END_RE.match(line):
            last_defect = None  # the vehicle and holder block follows the defects
            continue
        if _OBD_INTRO_RE.search(line) or ("ANTIPOLLUTION" in line and _OBD_CODE_RE.search(line)):
            obd = _OBD_CODE_RE.findall(line)
            target = next((d for d in reversed(summary["defects"]) if d["code"].startswith("8.2.")), None)
            if target is not None:
                target.setdefault("obd_codes", [])
                target["obd_codes"].extend(c for c in obd if c not in target["obd_codes"])
                if not target["obd_codes"]:
                    del target["obd_codes"]
            obd_target, obd_lines = target, 0
            continue
        if obd_target is not None:
            # P-codes wrapped onto the next line(s), maybe behind another column's label.
            rest = line[_skip_left_prefixes(line, extra_prefixes):]
            if not rest.strip() and obd_lines < 2:
                obd_lines += 1
                continue
            if _OBD_ONLY_RE.match(rest):
                obd_target.setdefault("obd_codes", [])
                obd_target["obd_codes"].extend(c for c in _OBD_CODE_RE.findall(rest)
                                               if c not in obd_target["obd_codes"])
                obd_lines = 0
                continue
            obd_target = None
        if last_defect is None:
            continue
        if _STOP_ANY_RE.search(line) or _STOP_LINE_RE.match(line) or _STOP_START_RE.match(line) \
                or _MEASURE_TITLE_RE.match(unicodedata.normalize("NFC", orig)):
            last_defect = None
            continue
        q = _heading_rest_at(last_defect["label"], line)
        if q is not None:  # the rest of a wrapped official heading, behind the other column
            piece = orig[fmap[q]:].strip()
            glue = "" if last_defect["label"].endswith(("'", "’")) else " "
            last_defect["label"] = (last_defect["label"] + glue + piece).strip()[:300]
            continue
        pos = _skip_left_prefixes(line, extra_prefixes)
        rest = line[pos:].strip()
        if not rest:
            continue
        if _STOP_START_RE.match(rest):
            last_defect = None
            continue
        start = pos + (len(line[pos:]) - len(line[pos:].lstrip()))
        piece = orig[fmap[start]:].strip()
        if not _LOCATION_ONLY_RE.match(rest):
            if not any(ch.islower() for ch in piece):
                # Upper-case only: a name, an address, make and model, a CNIT... but keep a
                # location list printed after it, and the catalogue token "OBD".
                if rest != "OBD":
                    tail = _LOCATION_TAIL_RE.search(rest) if ":" in last_defect["label"] else None
                    if not tail:
                        continue
                    piece = orig[fmap[start + tail.start("loc")]:].strip()
            else:
                words = piece.split(" ")
                k = next((n for n, w in enumerate(words) if any(ch.islower() for ch in w)), 0)
                if k and ":" not in " ".join(words[:k]):
                    piece = " ".join(words[k:])  # "949 RUE DES ... châssis ou à l'essieu" → the label part
        if len(last_defect["label"]) < 300:
            last_defect["label"] = (last_defect["label"] + " " + piece).strip()[:300]
        if layout in ("B", "C") and _LOCATION_END_RE.search(last_defect["label"]):
            last_defect = None  # these forms end every defect with "(<location>)"

    # Backstop: code-shaped text the strict pattern did not extract (glued to a word, a
    # stray space inside). A ".3" code is critical whatever else could be read.
    extracted = Counter(d["code"] for d in summary["defects"])
    not_read = 0
    for idx, line in enumerate(lines):
        for m in _LOOSE_CODE_RE.finditer(line):
            code = ".".join((m.group(1), m.group(2), m.group(3), m.group(4).lower(), m.group(5)))
            if extracted[code] > 0:
                extracted[code] -= 1
                continue
            not_read += 1
            if m.group(5) == "3":
                orig, fmap = original_lines[idx], folded_maps[idx][1]
                summary["defects"].append({
                    "code": code, "severity": "critical", "system": SYSTEMS.get(m.group(1), "autre"),
                    "label": orig[fmap[m.end()]:].lstrip(" -:.)\t").strip()[:300],
                })
                summary["counts"]["critical"] += 1

    for defect in summary["defects"]:
        defect["label"] = _scrub_label(defect["label"])[:200]
    counts = summary["counts"]

    # --- overall result
    field_result, field_idx = _result_from_field(lines)
    position_result, position_idx = None, None
    if head_idx is not None:
        j = _next_nonempty(lines, head_idx)
        if j is not None:
            position_result = _strict_result(_with_wrap(lines, j))
            position_idx = j if position_result else None
    conflict = False
    confidence = 1.0
    result, source = RESULT_UNKNOWN, "none"
    phrases = _phrase_results(lines)
    if field_result and position_result and field_result != position_result:
        conflict = True
        warnings.append("result_lines_disagree")
        field_result = max(field_result, position_result, key=_RESULT_RANK.__getitem__)
    if field_result or position_result:
        result, source = (field_result, "field") if field_result else (position_result, "position")
        if RESULT_CRITICAL in phrases and result != RESULT_CRITICAL:
            # "Défavorable pour défaillances critiques" printed elsewhere: the red result
            # wins over a field that may be prose ("résultat favorable du test ..."). A
            # major phrase does not override: severity legends print it on favourable PVs.
            conflict = True
            warnings.append("result_phrase_disagrees")
            result, source = RESULT_CRITICAL, "phrase"
    else:
        body = "\n".join(line for line in lines if not _SLIP_RE.search(line))
        favourable_only = re.search(r"\bFAVORABLE\b", body) is not None and "DEFAVORABLE" not in body
        if len(phrases) == 1:
            result, source, confidence = next(iter(phrases)), "phrase", 0.8
        elif counts["critical"]:
            result, source, confidence = RESULT_CRITICAL, "defects", 0.7
        elif counts["major"]:
            result, source, confidence = RESULT_MAJOR, "defects", 0.7
        elif favourable_only:
            result, source, confidence = RESULT_FAVORABLE, "phrase", 0.8
        else:
            confidence = 0.3

    # --- cross-checks: validity period and the nature of the next control
    result_idx = position_idx if position_idx is not None else field_idx
    summary["validity_date"] = _validity_date(lines, layout, result_idx)
    if summary["validity_date"] and summary["inspection_date"] and \
            summary["validity_date"] < summary["inspection_date"]:
        summary["validity_date"] = None
    summary["next_control"] = _next_control(lines, layout, result_idx)
    implied = None
    if summary["nature"] in ("periodique", "vehicule_ecole"):
        implied = _implied_by_validity(summary["inspection_date"], summary["validity_date"])
    known_form = layout in ("A", "B")  # validity semantics checked on every real PV of these forms
    if implied:
        if result == RESULT_UNKNOWN and known_form:
            result, source, confidence = implied, "validity", 0.6
        elif result != RESULT_UNKNOWN and implied != result:
            conflict = True
            warnings.append("validity_disagrees")
            if implied == RESULT_CRITICAL and known_form:
                result, source = RESULT_CRITICAL, "validity"  # a same-day validity is a critical PV
    nxt = summary["next_control"]
    if result != RESULT_UNKNOWN and nxt and summary["nature"] in ("periodique", "vehicule_ecole"):
        if (nxt == "contre_visite") != (result in (RESULT_MAJOR, RESULT_CRITICAL)):
            conflict = True
            warnings.append("next_control_disagrees")

    # Defects more severe than the stated result raise it (and lower confidence).
    if counts["critical"] and result != RESULT_CRITICAL:
        if result != RESULT_UNKNOWN:
            conflict = True
            warnings.append("critical_defect_above_result")
        result, source = RESULT_CRITICAL, "defects"
    elif counts["major"] and result == RESULT_FAVORABLE:
        conflict = True
        warnings.append("major_defect_on_favourable")
        result, source = RESULT_MAJOR, "defects"
    summary["result"], summary["result_source"] = result, source

    # --- confidence
    confidence -= 0.1 * summary["suffix_disagreements"]
    if conflict:
        confidence = min(confidence, 0.5)
    if summary["defects"] and not has_headers:
        confidence = min(confidence, 0.7)
    if summary["unsectioned_codes"]:
        confidence = min(confidence, 0.7)
        warnings.append("codes_outside_sections")
    if skipped_codes:
        confidence = min(confidence, 0.6)  # defect codes with no readable severity
        warnings.append("codes_without_severity")
    if (result == RESULT_MAJOR and not counts["major"]) or (result == RESULT_CRITICAL and not counts["critical"]):
        confidence = min(confidence, 0.6)  # the stated result is not backed by defects
    if summary["nature"] == "volontaire":
        confidence = min(confidence, 0.6)
    if other_suffix:
        confidence = min(confidence, 0.7)  # a code scheme no real PV has been checked against
        warnings.append("unknown_code_scheme")
    if legacy_codes:
        confidence = min(confidence, 0.7)
        warnings.append("legacy_codes")
    if not_read:
        confidence = min(confidence, 0.7)  # code-shaped text that could not be read as a code
        warnings.append("codes_not_read")
    last_pause = max((i for i, line in enumerate(lines) if _PAUSE_RE.search(line)), default=None)
    pages = max((int(m.group(1)) for m in _PAGES_RE.finditer(folded)), default=1)
    has_suite = any(_SUITE_RE.search(line) for line in lines)
    if (last_pause is not None and not any(_SUITE_RE.search(line) for line in lines[last_pause:])) \
            or (pages > 1 and not has_suite):
        confidence = min(confidence, 0.7)  # defects and the odometer history may be missing
        warnings.append("pages_missing")
    if summary["inspection_km"] is None and summary["nature"] in ("periodique", "contre_visite", "vehicule_ecole"):
        warnings.append("km_missing")  # informative: the result does not depend on it
    if order_breaks:
        warnings.append("codes_out_of_order")
    summary["result_conflict"] = conflict

    # --- odometer history (and the current reading, for the rollback check)
    block_pairs, block_lines = _history_pairs(lines)
    pairs = set(block_pairs)
    line_starts = [0]
    for line in lines[:-1]:
        line_starts.append(line_starts[-1] + len(line) + 1)
    for m in _ODO_PAIR_RE.finditer(folded):
        if bisect.bisect_right(line_starts, m.start()) - 1 in block_lines:
            continue  # the block reader paired this line already ("date :" then km)
        if _NOT_ODO_DATE_RE.search(folded[max(0, m.start() - 40):m.start()]):
            continue
        d, mth, y, km_token = m.groups()
        iso = _date(d, mth, y)
        if iso:
            pairs.add((iso, _to_int(km_token)))
    history = sorted(pairs)
    summary["odometer"] = [{"date": d, "km": k} for d, k in history]
    if history:
        summary["odometer_history"] = "present"
    elif _COHERENCE_NOT_DONE_RE.search(folded):
        summary["odometer_history"] = "coherence_non_realisee"
    elif re.search(r"(?m)^\s*NON\s+RENSEIGNES?\s*$", folded):
        summary["odometer_history"] = "non_renseigne"
    series = set(history)
    if summary["inspection_date"] and summary["inspection_km"]:
        series.add((summary["inspection_date"], summary["inspection_km"]))
    ordered = sorted(series)
    summary["odometer_inconsistent"] = any(later[1] < earlier[1] for earlier, later in zip(ordered, ordered[1:]))
    if any(d["code"].startswith("7.11.1.") for d in summary["defects"]):
        summary["odometer_inconsistent"] = True  # the CT itself flagged the odometer

    summary["parse_confidence"] = round(max(0.0, min(1.0, confidence)), 2)
