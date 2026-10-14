"""Alcopa Auction (alcopa-auction.fr) adapter.

Selectors are ported from the original prototype, with the verified bugs fixed:
- mileage is parsed line by line (the old regex merged "1ère mise : 2024" and
  "35 394 km" into 202,435,394 km);
- `data-current-price` is the opening price until someone bids, so it is stored as
  ``current_price_attr``, never as the starting price; the `.text-nowrap` fallback is gone;
- lot n°0 (not yet numbered) and price "--" become NULL;
- identity is the product id at the end of the detail URL, not the lot number;
- a listing walk reports whether it is complete instead of returning partial results
  silently.

Site facts here come from third-party fixtures and search snippets (medium confidence)
and must be re-validated on pages captured with ``python -m auction_agent capture``.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..config import DEFAULT_SALE_URL
from ._parsing import (
    first_int,
    parse_eur,
    parse_fr_date,
    parse_km,
    parse_km_lines,
    parse_plain_amount,
    paris_datetime,
)
from ..privacy import find_vin, redact
from .base import AuctionAdapter, DocumentRef, LotDetail, LotStub, ParseError, SaleListing, SaleRef

BASE_URL = "https://www.alcopa-auction.fr"

_PRODUCT_PATH_RE = re.compile(r"^/(?:[a-z]{2}/)?(?P<cat>[a-z-]+)/[^/]+/[^/]+-(?P<id>\d{5,8})/?$")
_SALE_PATH_RE = re.compile(
    r"^/(?:(?P<room_kind>salle-de-vente-encheres)/(?P<room>[^/]+)|(?P<online>vente-encheres-en-ligne))/(?P<id>\d+)/?$"
)

FUEL_CODES = {
    # French registration (carte grise P.3) codes as shown on Alcopa cards.
    # Hybrid code meanings are not verified against Alcopa's own legend.
    "ES": "petrol",
    "GO": "diesel",
    "EL": "electric",
    "EE": "plugin_hybrid",
    "EH": "hybrid",
    "GH": "hybrid",
    "GL": "plugin_hybrid",
    "GP": "lpg",
    "EG": "lpg",
    "GN": "cng",
    "FE": "e85",
    "FL": "e85",
}
FUEL_WORDS = {
    "essence": "petrol", "diesel": "diesel", "gazole": "diesel", "électrique": "electric",
    "electrique": "electric", "hybride rechargeable": "plugin_hybrid", "hybride": "hybrid",
    "gpl": "lpg", "gnv": "cng", "e85": "e85", "superéthanol": "e85",
}

SPEC_FIELD_MAP = {
    "marque": "brand",
    "modèle": "model",
    "modele": "model",
    "finition": "trim",
    "énergie": "fuel",
    "energie": "fuel",
    "mise en circulation": "first_registration",
    "kilométrage": "mileage_km",
    "kilometrage": "mileage_km",
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
    "puissance": "power_hp",
}
_IDENTITY_PATTERNS = (
    ("vin", re.compile(r"s[ée]rie|\bvin\b|identification|ch[aâ]ssis|vehicle identification")),
    ("registration", re.compile(r"immatricul|plaque")),
)


def _norm_label(text: str) -> str:
    """'Immatriculation\xa0:' → 'immatriculation' (NFKC folds NBSP; trailing ':' removed)."""
    return re.sub(r"[\s:]+$", "", unicodedata.normalize("NFKC", text)).strip().lower()


def _identity_kind(label: str) -> Optional[str]:
    if label.startswith("date") or "mise en circulation" in label:
        return None  # "Date de 1ère immatriculation" is a date, not the plate
    for kind, rx in _IDENTITY_PATTERNS:
        if rx.search(label):
            return kind
    return None


def is_alcopa_url(url: str) -> bool:
    """https URL on alcopa-auction.fr or one of its sub-domains (dot boundary enforced)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "alcopa-auction.fr" or host.endswith(".alcopa-auction.fr"))


def _category(path_segment: str) -> str:
    if path_segment.startswith("voiture"):
        return "voiture"
    if path_segment.startswith("utilitaire"):
        return "utilitaire"
    if path_segment.startswith("materiel"):
        return "materiel"
    return "other"


def product_id_from_url(url: str) -> Optional[tuple[str, str]]:
    """(category, product_id) from a detail URL, or None."""
    m = _PRODUCT_PATH_RE.match(urlsplit(url).path)
    if not m:
        return None
    return _category(m.group("cat")), m.group("id")


def normalise_fuel(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    if raw.upper() in FUEL_CODES:
        return FUEL_CODES[raw.upper()]
    low = raw.lower()
    if "hybride" in low or (("électri" in low or "electri" in low)
                            and any(w in low for w in ("essence", "gazole", "diesel"))):
        if "non rechargeable" in low:
            return "hybrid"
        if "rechargeable" in low:
            return "plugin_hybrid"
        return "hybrid"
    for word, fuel in FUEL_WORDS.items():
        if word in low:
            return fuel
    return "other"


def normalise_gearbox(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    low = raw.lower()
    if "auto" in low:
        return "automatic"
    if "manu" in low:
        return "manual"
    return None


def _soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("noscript"):
        tag.decompose()
    return soup


# --------------------------------------------------------------------------- listing page


def parse_card(card: Tag) -> Optional[LotStub]:
    """Parse one listing card. Returns None only when the card has no lot link."""
    link = card.select_one(".card-title a")
    if link is None or not link.get("href"):
        return None
    detail_url = urljoin(BASE_URL, str(link["href"]))
    if not is_alcopa_url(detail_url):
        raise ParseError(f"Unexpected lot link {detail_url!r}")
    ident = product_id_from_url(detail_url)
    if ident is None:
        raise ParseError(f"No product id in {detail_url}")
    category, product_id = ident

    title = link.get_text(" ", strip=True)
    brand, model = (None, title)
    if "|" in title:
        brand, model = (part.strip() or None for part in title.split("|", 1))

    stub = LotStub(
        house=AlcopaAdapter.slug,
        external_id=product_id,
        category=category,
        detail_url=detail_url,
        brand=brand.upper() if brand else None,
        model=model.upper() if model else None,
    )

    text_block = card.select_one(".card-body .card-text")
    if text_block is not None:
        trim_p = text_block.select_one("p.mb-2")
        stub.trim = trim_p.get_text(" ", strip=True) or None if trim_p else None
        tech_p = text_block.select_one("p.mb-1")
        if tech_p is not None:
            lines = [ln.strip() for ln in tech_p.get_text("\n", strip=True).split("\n") if ln.strip()]
            if lines and len(lines[0]) <= 3:
                stub.fuel = normalise_fuel(lines[0])
            for line in lines:
                m = re.search(r"1(?:ère|ere|re)\s*mise\s*:\s*(\d{4})", line, re.IGNORECASE)
                if m:
                    stub.year = int(m.group(1))
            stub.mileage_km = parse_km_lines(lines)
            stub.gearbox = normalise_gearbox(" ".join(lines[1:]))

    footer = card.select_one(".card-footer")
    if footer is not None:
        footer_text = footer.get_text(" ", strip=True)
        lot = re.search(r"Lot\s*n?°?\s*(\d+)", footer_text, re.IGNORECASE)
        if lot and int(lot.group(1)) > 0:
            stub.lot_number = int(lot.group(1))
        loc = footer.select_one("p[title='Lieu de stockage']")
        if loc is not None:
            stub.storage_location = loc.get_text(" ", strip=True) or None
        price = re.search(r"Mise à prix\s*:\s*(--|[^€]{0,20}€)", footer_text)
        if price:
            stub.starting_price = parse_eur(price.group(1))
        for el in footer.find_all(string=re.compile("garantie", re.IGNORECASE)):
            label = el.strip()
            if label and not re.search(r"\bsans\s+garantie\b", label, re.IGNORECASE):
                stub.warranty = label[:60]
                break
        battery = re.search(r"Certificat batterie\s*(\d{1,3})\s*%", footer_text, re.IGNORECASE)
        if battery:
            stub.battery_soh_pct = float(battery.group(1))

    countdown = card.select_one("[data-ts]")
    raw_ts = str(countdown.get("data-ts", "")) if countdown is not None else ""
    if raw_ts.isascii() and raw_ts.isdigit():
        ts = int(raw_ts)
        if ts > 10**11:  # milliseconds
            ts //= 1000
        try:
            stub.closing_hint_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            stub.closing_hint_at = None  # optional hint: never fail a card over it
    current = card.select_one("[data-current-price]")
    if current is not None:
        stub.current_price_attr = parse_plain_amount(current.get("data-current-price"))
    return stub


def parse_sale_page(html: str, page: int = 1) -> dict[str, Any]:
    """Parse one listing page: sale metadata, stubs, parse failures, next-page flag."""
    soup = _soup(html)
    title_el = soup.select_one("h5.text-prim")
    title = title_el.get_text(" ", strip=True) if title_el else ""
    total = None
    m = re.search(r"Nombre de lots\s*:?\s*(\d{1,3}(?:[ \u00a0\u202f.]\d{3})+|\d+)", soup.get_text(" "))
    if m:
        total = first_int(m.group(1))
    else:
        count_el = soup.select_one(".founded_items .nb_items")
        if count_el is not None:
            total = first_int(count_el.get_text())

    cards = soup.select(".col-12.col-lg-6.align-items-stretch") or soup.select(".card.h-100")
    stubs: list[LotStub] = []
    failures = 0
    for card in cards:
        try:
            stub = parse_card(card)
        except (ParseError, ValueError, AttributeError, OverflowError, OSError):
            failures += 1
            continue
        if stub is None:
            failures += 1
        else:
            stubs.append(stub)

    room = None
    rm = re.search(r"à\s+([A-Za-zÀ-ÿ\- ]+?)\s*(?::|$)", title)
    if rm:
        room = rm.group(1).strip()
    return {
        "title": title or None,
        "sale_date": parse_fr_date(title),
        "room": room,
        "expected_lots": total,
        "stubs": stubs,
        "parse_failures": failures,
        "cards": len(cards),
        "has_next": _has_next_page(soup, page),
    }


def _has_next_page(soup: BeautifulSoup, current_page: int) -> bool:
    if soup.select_one("button[name='page'][value='suivant']:not([disabled]):not([aria-disabled='true'])"):
        return True
    values = [int(b["value"]) for b in soup.select("button[name='page']") if str(b.get("value", "")).isdigit()]
    return any(v > current_page for v in values)


# --------------------------------------------------------------------------- detail page


def parse_detail_page(html: str, external_id: str) -> LotDetail:
    soup = _soup(html)
    rows = soup.select("table.table-striped tr")
    if not rows:
        raise ParseError("No specification table (layout change, consent or error page)")

    detail = LotDetail(external_id=external_id)
    specs: dict[str, str] = {}
    for row in rows:
        th, td = row.select_one("th"), row.select_one("td")
        if th is None or td is None:
            continue
        label = _norm_label(th.get_text(" ", strip=True))
        value = td.get_text(" ", strip=True)
        if label in specs:  # the table is rendered twice on lot pages
            continue
        specs[label] = value

    fields: dict[str, Any] = {}
    for label, value in specs.items():
        identity = _identity_kind(label)
        if identity:
            if getattr(detail, identity) is None:
                setattr(detail, identity, value or None)
            continue
        detail.raw_specs[label] = redact(value)[:200]
        key = SPEC_FIELD_MAP.get(label)
        if not key or not value:
            continue
        if key == "mileage_km":
            fields[key] = parse_km(value if "km" in value.lower() else f"{value} km")
        elif key in {"engine_cc", "co2", "power_hp"}:
            fields[key] = first_int(value)
        elif key == "first_registration":
            day = parse_fr_date(value)
            if day:
                fields["first_registration"] = day.isoformat()
                fields["year"] = day.year
        elif key == "fuel":
            fields["fuel"] = normalise_fuel(value)
        elif key == "gearbox":
            fields["gearbox"] = normalise_gearbox(value)
        elif key == "vat_recoverable":
            low = value.lower()
            fields["vat_regime"] = "ht_recuperable" if low.startswith("oui") else ("ttc" if low.startswith("non") else "unknown")
        elif key in {"brand", "model"}:
            fields[key] = value.upper()
        elif key in {"trim", "body_type", "storage_location"}:
            fields[key] = value
    if detail.vin is None:  # unusual label: recover the VIN from the values to hash it
        detail.vin = next((v for v in map(find_vin, specs.values()) if v), None)
    detail.fields = fields

    # Cross-check the product id against the embedded damage JSON, when present.
    for script in soup.find_all("script"):
        m = re.search(r'"product_id"\s*:\s*"?(\d+)', script.get_text())
        if m and m.group(1) != external_id:
            raise ParseError(f"Page product_id {m.group(1)} does not match {external_id}")
        if m:
            break

    seen_types: set[str] = set()
    for a in soup.select("a[href*='/getDocument/']"):
        href = str(a.get("href", ""))
        doc_type = None
        if "/getDocument/ct/" in href:
            doc_type = "ct"
        elif "/getDocument/mh/" in href:
            doc_type = "maintenance"
        elif "/getDocument/moba/" in href:
            doc_type = "battery"
        url = urljoin(BASE_URL, href)
        if doc_type and doc_type not in seen_types and is_alcopa_url(url):
            seen_types.add(doc_type)
            detail.documents.append(DocumentRef(doc_type, url))

    photos = {str(a.get("href")) for a in soup.select("a[data-gallery='pictures']") if a.get("href")}
    detail.photo_count = len(photos) or (1 if soup.select_one("img[itemprop='image']") else 0)

    header = soup.find(lambda t: t.name in ("h3", "h4") and "commentaires" in t.get_text().lower())
    if header is not None:
        parts = []
        for sib in header.find_next_siblings():
            if sib.name in ("h2", "h3", "h4"):
                break
            text = sib.get_text(" ", strip=True)
            if text:
                parts.append(text)
        if parts:
            detail.comments = " ".join(parts)[:2000]

    bat = soup.select_one(".progress-value")
    if bat is not None:
        m = re.search(r"(\d{1,3})\s*%", bat.get_text())
        if m:
            fields["battery_soh_pct"] = float(m.group(1))

    form = soup.select_one("form[data-current-price]")
    if form is not None:
        fields["current_price_attr"] = parse_plain_amount(form.get("data-current-price"))
    page_text = soup.get_text(" ", strip=True)
    m = re.search(r"Mise à prix\s*:?\s*(--|[^€]{0,20}€)", page_text)
    if m:
        fields["starting_price"] = parse_eur(m.group(1))
    if re.search(r"batterie\s+(?:en\s+)?location|location\s+de\s+(?:la\s+)?batterie", page_text, re.IGNORECASE):
        fields["battery_leased"] = 1

    crumb = soup.select_one("a[href*='/salle-de-vente-encheres/'], a[href*='/vente-encheres-en-ligne/']")
    if crumb is not None and is_alcopa_url(urljoin(BASE_URL, str(crumb["href"]))):
        detail.sale_url = urljoin(BASE_URL, str(crumb["href"]))
    return detail


# --------------------------------------------------------------------------- adapter


class AlcopaAdapter(AuctionAdapter):
    slug = "alcopa"
    display_name = "Alcopa Auction"
    base_url = BASE_URL
    url_patterns = (re.compile(r"^https?://(?:www\.)?alcopa-auction\.fr/"),)

    # Buyer premium for room sales: 12% HT / 14.4% TTC, minimum €360 TTC, per the CGV as
    # read by a third party (medium confidence). Online sale prices include fees.
    ROOM_FEE_RATE_TTC = 0.144
    ROOM_FEE_MIN_TTC = 360.0
    VAT_RATE = 0.20
    MAX_PAGES = 200  # hard stop for a listing walk

    def sale_from_url(self, url: str) -> SaleRef:
        full = urljoin(BASE_URL, url)
        parts = urlsplit(full)
        m = _SALE_PATH_RE.match(parts.path)
        if not m:
            raise ValueError(f"Not an Alcopa sale URL: {url}")
        kind = "online" if m.group("online") else ("multisite" if m.group("room") == "multisite" else "room")
        room = None if kind != "room" else m.group("room").replace("-", " ").title()
        return SaleRef(house=self.slug, external_id=m.group("id"), url=full, kind=kind, room=room)

    def discover_sales(self) -> list[SaleRef]:
        raise NotImplementedError(
            "Sale discovery needs the Phase 2 live recon of the calendar page; pass --url."
        )

    def _page_url(self, sale_url: str, page: int) -> str:
        parts = urlsplit(sale_url)
        query = parse_qs(parts.query)
        query["page"] = [str(page)]
        return f"{parts.scheme}://{parts.netloc}{parts.path}?{urlencode(query, doseq=True)}"

    def list_sale(self, sale: SaleRef, max_pages: Optional[int] = None) -> SaleListing:
        stubs: dict[str, LotStub] = {}
        reasons: list[str] = []
        failures = duplicates = pages = 0
        meta: dict[str, Any] = {}
        page = 1
        page_cap = self.MAX_PAGES
        first_page_cards = 0
        last_cards = 0
        while True:
            html = self.http.get_text(self._page_url(sale.url, page), referer=sale.url)
            parsed = parse_sale_page(html, page)
            pages += 1
            if page == 1:
                meta = parsed
                first_page_cards = parsed["cards"]
                if parsed["expected_lots"] and first_page_cards:
                    page_cap = min(page_cap, -(-parsed["expected_lots"] // first_page_cards) + 2)
            failures += parsed["parse_failures"]
            added = 0
            for stub in parsed["stubs"]:
                if stub.external_id in stubs:
                    duplicates += 1
                else:
                    stubs[stub.external_id] = stub
                    added += 1
            last_cards = parsed["cards"]
            if parsed["cards"] == 0:
                reasons.append("no_cards_on_first_page" if page == 1 else f"empty_page={page}")
                break
            if page > 1 and added == 0:
                reasons.append(f"duplicate_page={page}")  # site served an earlier page again
                break
            if not parsed["has_next"]:
                break
            if max_pages and page >= max_pages:
                reasons.append(f"scoped:max_pages={max_pages}")
                break
            if page >= page_cap:
                reasons.append(f"page_cap_hit={page_cap}")
                break
            page += 1

        starts_at = ends_at = None
        if meta.get("sale_date"):
            starts_at = paris_datetime(meta["sale_date"], 0, 0)
            ends_at = paris_datetime(meta["sale_date"], 23, 59) if sale.kind == "room" else None
        enriched = SaleRef(
            house=sale.house,
            external_id=sale.external_id,
            url=sale.url,
            kind=sale.kind,
            audience=sale.audience,
            room=meta.get("room") or sale.room,
            title=meta.get("title"),
            starts_at=starts_at,
            ends_at=ends_at,
            expected_lots=meta.get("expected_lots"),
        )
        if failures:
            reasons.append(f"card_parse_failures={failures}")
        expected = enriched.expected_lots
        if expected is None:
            reasons.append("expected_lots_unknown")
            if pages > 1 and last_cards and last_cards == first_page_cards:
                reasons.append("last_page_full")  # cannot tell whether a page is missing
        if expected is not None:
            tolerance = max(2, round(expected * 0.02))
            if len(stubs) < expected - tolerance:
                reasons.append(f"fewer_lots_than_announced={len(stubs)}/{expected}")
        complete = not reasons
        return SaleListing(
            sale=enriched,
            stubs=list(stubs.values()),
            complete=complete,
            reasons=reasons,
            pages_fetched=pages,
            parse_failures=failures,
            duplicates=duplicates,
        )

    def fetch_detail(self, stub: LotStub) -> LotDetail:
        html = self.http.get_text(stub.detail_url)
        return parse_detail_page(html, stub.external_id)

    def fee_model(self, price: float, sale_kind: str, buyer: str, vat_regime: str) -> float:
        base = price
        if vat_regime == "ht_recuperable" and buyer == "particulier":
            base = price * (1 + self.VAT_RATE)  # a private buyer cannot recover VAT
        if sale_kind == "online":
            return round(base, 2)
        fee = max(price * self.ROOM_FEE_RATE_TTC, self.ROOM_FEE_MIN_TTC)
        return round(base + fee, 2)


def default_sale_url() -> str:
    return urljoin(BASE_URL, DEFAULT_SALE_URL)
