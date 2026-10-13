"""Scrapes vehicle listings from Alcopa Auction sale pages."""
import re
import time
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup
from rich.console import Console

from config import (
    BASE_URL,
    MAX_RETRIES,
    REQUEST_DELAY_SECONDS,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

console = Console()


def get_session() -> requests.Session:
    """Create a configured requests session."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    return session


def clean_int(text: Optional[str]) -> Optional[int]:
    """Extract first integer from text (e.g. '200 166 km' -> 200166)."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    return int(cleaned) if cleaned else None


def clean_float(text: Optional[str]) -> Optional[float]:
    """Extract float from price string (e.g. '10 200 €' -> 10200.0)."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_vehicle_card(card: BeautifulSoup) -> Optional[dict[str, Any]]:
    """Extract data from a single vehicle card on the listing page."""
    try:
        data: dict[str, Any] = {}

        # Brand and Model from header link
        header_link = card.select_one(".card-title a")
        if not header_link:
            return None

        title_text = header_link.get_text(strip=True)
        if "|" in title_text:
            parts = [p.strip() for p in title_text.split("|", 1)]
            data["brand"] = parts[0]
            data["model"] = parts[1]
        else:
            data["brand"] = None
            data["model"] = title_text

        # Detail URL
        href = header_link.get("href", "")
        data["detail_url"] = urljoin(BASE_URL, href)

        # Image URL
        img = card.select_one("img.img-fluid")
        data["image_url"] = img.get("src") if img else None

        # Card text section
        card_text = card.select_one(".card-body .card-text")
        if card_text:
            # Trim description
            trim_p = card_text.select_one("p.mb-2")
            data["trim"] = trim_p.get_text(strip=True) if trim_p else None

            # Tech details paragraph (fuel, year, km, gearbox)
            tech_p = card_text.select_one("p.mb-1")
            if tech_p:
                tech_text = tech_p.get_text(separator="\n", strip=True)
                lines = [line.strip() for line in tech_text.split("\n") if line.strip()]

                # First line is usually fuel code: EE, ES, GO, EL, etc.
                if lines and len(lines[0]) <= 3:
                    data["fuel_type"] = lines[0]

                # Year from "1ère mise : YYYY"
                year_match = re.search(r"1ère mise\s*:\s*(\d{4})", tech_text)
                if year_match:
                    data["year"] = int(year_match.group(1))

                # Mileage from "NNN NNN km"
                km_match = re.search(r"([\d\s]+)\s*km", tech_text, re.IGNORECASE)
                if km_match:
                    data["mileage_km"] = clean_int(km_match.group(1))

                # Gearbox
                if "Automatique" in tech_text:
                    data["gearbox"] = "AUTOMATIQUE"
                elif "Manuelle" in tech_text:
                    data["gearbox"] = "MANUELLE"
                else:
                    data["gearbox"] = None

        # Card footer (lot, price, location, warranty, etc.)
        footer = card.select_one(".card-footer")
        if footer:
            footer_text = footer.get_text(separator=" ", strip=True)

            # Lot number
            lot_match = re.search(r"Lot\s*n°?\s*(\d+)", footer_text, re.IGNORECASE)
            if lot_match:
                data["lot_number"] = int(lot_match.group(1))

            # Storage location
            loc_el = footer.select_one("p[title='Lieu de stockage']")
            if loc_el:
                data["storage_location"] = loc_el.get_text(strip=True).replace("Beauvais", "Beauvais").strip()

            # Starting price (Mise à prix)
            map_match = re.search(r"Mise à prix\s*:\s*([\d\s]+)\s*€", footer_text)
            if map_match:
                data["starting_price"] = clean_float(map_match.group(1))

            # Current bid (Enchère courante)
            bid_match = re.search(r"Enchère courante\s*:\s*([\d\s]+)\s*€", footer_text)
            if bid_match:
                data["current_bid"] = clean_float(bid_match.group(1))

            # Warranty badge
            if "Garantie" in footer_text:
                data["warranty"] = "Garantie Essentielle"

            # Battery certificate
            bat_match = re.search(r"Certificat batterie\s*(\d+%)", footer_text)
            if bat_match:
                data["battery_health"] = bat_match.group(1)

        return data
    except Exception as e:
        console.print(f"[yellow]Error parsing vehicle card: {e}[/yellow]")
        return None


def fetch_page(url: str, session: requests.Session) -> Optional[BeautifulSoup]:
    """Fetch and parse a webpage with retries."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                console.print(f"[red]Failed to fetch {url}: {e}[/red]")
                return None
            time.sleep(2 ** attempt)
    return None


def extract_sale_metadata(soup: BeautifulSoup, sale_url: str) -> dict[str, Any]:
    """Extract sale room, date, title, and lot count from listing page."""
    metadata: dict[str, Any] = {
        "sale_url": sale_url,
        "sale_room": "Beauvais",
        "sale_date": "",
        "sale_title": "",
        "total_lots": 0,
    }

    # Sale title e.g. "Vente du 19 octobre 2026 à Beauvais : Véhicules Utilitaires..."
    title_el = soup.select_one("h5.text-prim")
    if title_el:
        metadata["sale_title"] = title_el.get_text(strip=True)

    # Date from title or text
    date_match = re.search(r"(\d{1,2}\s+[a-zéû]+\s+\d{4})", metadata["sale_title"], re.IGNORECASE)
    if date_match:
        metadata["sale_date"] = date_match.group(1)

    # Sale room
    room_match = re.search(r"à\s+([A-Za-zÀ-ÿ\-]+)", metadata["sale_title"])
    if room_match:
        metadata["sale_room"] = room_match.group(1)

    # Total lots
    lots_match = re.search(r"Nombre de lots\s*:\s*<b>(\d+)</b>", str(soup))
    if lots_match:
        metadata["total_lots"] = int(lots_match.group(1))
    else:
        # Fallback to search count
        count_el = soup.select_one(".founded_items .nb_items")
        if count_el:
            metadata["total_lots"] = clean_int(count_el.get_text()) or 0

    return metadata


def has_next_page(soup: BeautifulSoup, current_page: int) -> bool:
    """Check if there is a next page of results."""
    # Look for button with name="page" and value="suivant" or value > current_page
    next_btn = soup.select_one("button[name='page'][value='suivant']")
    if next_btn:
        return True

    # Check numbered buttons
    page_buttons = soup.select("button[name='page']")
    for btn in page_buttons:
        val = btn.get("value")
        if val and val.isdigit() and int(val) > current_page:
            return True

    return False


def scrape_all_listings(
    sale_url: str,
    max_pages: Optional[int] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Crawl all paginated pages of a sale.
    Returns (sale_metadata, list_of_vehicle_summaries).
    """
    session = get_session()
    all_vehicles: list[dict[str, Any]] = []
    sale_metadata: dict[str, Any] = {}

    current_page = 1
    full_url = urljoin(BASE_URL, sale_url)

    console.print(f"[bold blue]Starting crawl of sale: {full_url}[/bold blue]")

    while True:
        # Construct paginated URL
        parsed = urlparse(full_url)
        query = parse_qs(parsed.query)
        query["page"] = [str(current_page)]
        page_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(query, doseq=True)}"

        console.print(f"Fetching page {current_page}...")
        soup = fetch_page(page_url, session)
        if not soup:
            console.print(f"[red]Failed to load page {current_page}, stopping.[/red]")
            break

        # Extract sale metadata on first page
        if current_page == 1:
            sale_metadata = extract_sale_metadata(soup, sale_url)
            console.print(
                f"[green]Sale: {sale_metadata['sale_title']} "
                f"({sale_metadata['total_lots']} lots total)[/green]"
            )

        # Parse all vehicle cards on this page
        # Cards are inside turbo-frame#search-results or directly in .col-12.col-lg-6
        cards = soup.select(".col-12.col-lg-6.align-items-stretch")
        if not cards:
            # Fallback selector
            cards = soup.select(".card.h-100")

        page_vehicles = 0
        for card in cards:
            v = parse_vehicle_card(card)
            if v and v.get("detail_url"):
                all_vehicles.append(v)
                page_vehicles += 1

        console.print(f"  Found {page_vehicles} vehicles on page {current_page}")

        # Check for next page
        if max_pages and current_page >= max_pages:
            console.print(f"Reached page limit ({max_pages}), stopping.")
            break

        if not has_next_page(soup, current_page) or page_vehicles == 0:
            console.print("No more pages found.")
            break

        current_page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    console.print(
        f"[bold green]Listing crawl complete: {len(all_vehicles)} vehicles found "
        f"across {current_page} page(s)[/bold green]"
    )
    return sale_metadata, all_vehicles
