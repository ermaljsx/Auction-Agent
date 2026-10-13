"""Extracts text and key structured insights from downloaded PDF documents."""
import re
from pathlib import Path
from typing import Any, Optional, Union

import pdfplumber
from rich.console import Console

console = Console()


def extract_text_from_pdf(pdf_path: Union[str, Path]) -> Optional[str]:
    """
    Extract all readable text from a PDF file.
    Returns the concatenated text or None if extraction fails.
    """
    path = Path(pdf_path)
    if not path.exists() or path.stat().st_size == 0:
        return None

    try:
        pages_text = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)

        full_text = "\n\n".join(pages_text).strip()
        return full_text if full_text else None
    except Exception as e:
        console.print(f"[yellow]Failed to extract text from {path}: {e}[/yellow]")
        return None


def parse_controle_technique(text: str) -> dict[str, Any]:
    """
    Extract structured data from French Contrôle Technique text.
    Identifies:
    - Overall result: FAVORABLE / DÉFAVORABLE POUR DÉFAILLANCES MAJEURES / CRITIQUES
    - Critical defects (défaillances critiques)
    - Major defects (défaillances majeures)
    - Minor defects (défaillances mineures)
    - Date of inspection
    - Mileage at inspection
    """
    summary: dict[str, Any] = {
        "result": "UNKNOWN",
        "is_favorable": None,
        "defects_critical": [],
        "defects_major": [],
        "defects_minor": [],
        "inspection_date": None,
        "inspection_mileage": None,
    }

    if not text:
        return summary

    text_upper = text.upper()

    # Determine overall result
    if "FAVORABLE" in text_upper and "DEFAVORABLE" not in text_upper and "DÉFAVORABLE" not in text_upper:
        summary["result"] = "FAVORABLE"
        summary["is_favorable"] = True
    elif "CRITIQUE" in text_upper:
        summary["result"] = "DEFAVORABLE_CRITIQUE"
        summary["is_favorable"] = False
    elif "MAJEURE" in text_upper or "DEFAVORABLE" in text_upper or "DÉFAVORABLE" in text_upper:
        summary["result"] = "DEFAVORABLE_MAJEURE"
        summary["is_favorable"] = False

    # Extract inspection date (DD/MM/YYYY)
    date_matches = re.findall(r"\b(\d{2}/\d{2}/\d{4})\b", text)
    if date_matches:
        summary["inspection_date"] = date_matches[0]

    # Extract mileage at inspection
    km_match = re.search(r"(\d[\d\s]{3,})\s*(?:km|KM)", text)
    if km_match:
        cleaned = re.sub(r"[^\d]", "", km_match.group(1))
        if cleaned:
            summary["inspection_mileage"] = int(cleaned)

    # Extract defect lines
    lines = text.split("\n")
    current_category = None

    for line in lines:
        line_clean = line.strip()
        line_upper = line_clean.upper()

        # Check section headers
        if "DÉFAILLANCE CRITIQUE" in line_upper or "DEFAILLANCE CRITIQUE" in line_upper:
            current_category = "critical"
            continue
        elif "DÉFAILLANCE MAJEURE" in line_upper or "DEFAILLANCE MAJEURE" in line_upper:
            current_category = "major"
            continue
        elif "DÉFAILLANCE MINEURE" in line_upper or "DEFAILLANCE MINEURE" in line_upper:
            current_category = "minor"
            continue

        # Look for defect codes (e.g. 1.1.1.a.1, 5.2.3, etc.)
        is_defect_line = bool(re.match(r"^\d\.\d", line_clean)) or (
            current_category and len(line_clean) > 5 and not line_clean.startswith("---")
        )

        if is_defect_line and current_category:
            if current_category == "critical":
                summary["defects_critical"].append(line_clean)
            elif current_category == "major":
                summary["defects_major"].append(line_clean)
            elif current_category == "minor":
                summary["defects_minor"].append(line_clean)

    return summary
