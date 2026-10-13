"""Scrapes full specifications and document links from vehicle detail pages."""
import re
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from rich.console import Console

from config import (
    BASE_URL,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

console = Console()

# French field label -> database field mapping
SPEC_FIELD_MAP = {
    "marque": "brand",
    "modèle": "model",
    "modele": "model",
    "finition": "trim",
    "immatriculation": "registration",
    "énergie": "fuel_type",
    "energie": "fuel_type",
    "mise en circulation": "first_registration_date",
    "kilométrage": "mileage_km",
    "kilometrage": "mileage_km",
    "numéro de série": "vin",
    "numero de serie": "vin",
    "couleur": "color",
    "tva récupérable": "vat_recoverable",
    "tva recuperable": "vat_recoverable",
    "type": "vehicle_type",
    "carrosserie": "body_type",
    "lieu de stockage": "storage_location",
    "co2": "co2",
    "cylindrée moteur": "engine_cc",
    "cylindree moteur": "engine_cc",
    "boite de vitesse": "gearbox",
    "boîte de vitesse": "gearbox",
}


def clean_int(val: Any) -> Optional[int]:
    """Clean integer value from string."""
    if val is None:
        return None
    cleaned = re.sub(r"[^\d]", "", str(val))
    return int(cleaned) if cleaned else None


def clean_float(val: Any) -> Optional[float]:
    """Clean float value from string."""
    if val is None:
        return None
    cleaned = re.sub(r"[^\d.,]", "", str(val)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def scrape_vehicle_detail(
    detail_url: str,
    session: requests.Session,
) -> dict[str, Any]:
    """
    Scrape all available details from a vehicle page:
    - Caractéristiques table (specs)
    - Downloadable document URLs (CT, maintenance, battery)
    - Full-size photo URLs
    - Comments section
    - Real-time price/bid
    """
    result: dict[str, Any] = {
        "detail_url": detail_url,
        "documents": [],
        "photos": [],
        "comments": None,
    }

    try:
        resp = session.get(detail_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        console.print(f"[red]Failed to fetch detail page {detail_url}: {e}[/red]")
        return result

    # 1. Parse specs table ("Caractéristiques")
    specs_table = soup.select_one("table.table-striped")
    if specs_table:
        for row in specs_table.select("tr"):
            th = row.select_one("th")
            td = row.select_one("td")
            if not th or not td:
                continue

            raw_label = th.get_text(strip=True).lower()
            raw_value = td.get_text(strip=True)

            field_name = SPEC_FIELD_MAP.get(raw_label)
            if not field_name:
                continue

            # Type conversions
            if field_name == "mileage_km":
                result[field_name] = clean_int(raw_value)
            elif field_name == "co2":
                result[field_name] = clean_int(raw_value)
            elif field_name == "engine_cc":
                result[field_name] = clean_int(raw_value)
            elif field_name == "first_registration_date":
                result[field_name] = raw_value
                # Also extract 4-digit year
                year_match = re.search(r"\b(19\d{2}|20\d{2})\b", raw_value)
                if year_match:
                    result["year"] = int(year_match.group(1))
            else:
                result[field_name] = raw_value

    # 2. Extract document download links
    # Pattern: /getDocument/{type}/{base64_id}
    doc_links = soup.select("a[href*='/getDocument/']")
    seen_types = set()
    for a in doc_links:
        href = a.get("href", "")
        full_doc_url = urljoin(BASE_URL, href)

        doc_type = None
        label = a.get_text(strip=True)

        if "/getDocument/ct/" in href:
            doc_type = "ct"
            label = "Contrôle technique"
        elif "/getDocument/mh/" in href:
            doc_type = "mh"
            label = "Historique d'entretien"
        elif "/getDocument/moba/" in href:
            doc_type = "moba"
            label = "Certificat batterie"

        if doc_type and doc_type not in seen_types:
            seen_types.add(doc_type)
            result["documents"].append({
                "type": doc_type,
                "label": label,
                "url": full_doc_url,
            })

    # 3. Extract photo URLs
    # Full size pictures are in links with data-gallery="pictures"
    gallery_links = soup.select("a[data-gallery='pictures'][href*='photos.static']")
    seen_photos = set()
    for a in gallery_links:
        photo_url = a.get("href")
        if photo_url and photo_url not in seen_photos:
            seen_photos.add(photo_url)
            result["photos"].append(photo_url)

    # Fallback: grab the main image
    if not result["photos"]:
        main_img = soup.select_one("img[itemprop='image']")
        if main_img and main_img.get("src"):
            result["photos"].append(main_img.get("src"))

    # 4. Extract comments section
    comments_header = soup.find(lambda tag: tag.name in ["h3", "h4"] and "Commentaires" in tag.get_text())
    if comments_header:
        parent_div = comments_header.parent
        if parent_div:
            # Get text excluding the header itself
            comments_text = parent_div.get_text(separator=" ", strip=True)
            comments_text = comments_text.replace("Commentaires", "").strip()
            if comments_text:
                result["comments"] = comments_text

    # 5. Extract battery health if present
    bat_div = soup.select_one(".progress-value")
    if bat_div:
        result["battery_health"] = bat_div.get_text(strip=True)

    # 6. Extract price and lot number from the page
    # Bid form often contains data-current-price attribute
    bid_form = soup.select_one("form[data-current-price]")
    if bid_form:
        cur_price = bid_form.get("data-current-price")
        if cur_price:
            result["starting_price"] = clean_float(cur_price)

    # Price displayed on page
    price_span = soup.select_one(".text-nowrap")
    if price_span and "starting_price" not in result:
        result["starting_price"] = clean_float(price_span.get_text())

    # Lot number
    lot_el = soup.find(lambda tag: tag.name in ["h3", "h4", "p"] and "Lot" in tag.get_text())
    if lot_el:
        lot_match = re.search(r"Lot\s*(\d+)", lot_el.get_text())
        if lot_match:
            result["lot_number"] = int(lot_match.group(1))

    return result
