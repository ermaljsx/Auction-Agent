"""Adapter contract: every auction house plugs in through one subclass of AuctionAdapter.

The core (ingest, lifecycle, scoring, web) only sees the normalised dataclasses below;
it never parses HTML. Adding a house = one new module in this package plus fixtures.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Literal, Optional

if TYPE_CHECKING:  # pragma: no cover
    from ..http_client import PoliteClient

Fuel = Literal["petrol", "diesel", "hybrid", "plugin_hybrid", "electric", "lpg", "cng", "e85", "other"]
Category = Literal["voiture", "utilitaire", "materiel", "other"]
DocType = Literal["ct", "maintenance", "battery"]


class ParseError(RuntimeError):
    """The page did not have the expected structure (layout change, consent or error page)."""


@dataclass(frozen=True)
class SaleRef:
    house: str
    external_id: str
    url: str
    kind: str = "room"  # room | online | multisite | other
    audience: str = "public"  # public | pro
    room: Optional[str] = None
    title: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    expected_lots: Optional[int] = None


@dataclass
class LotStub:
    """What a listing card shows. Cheap: one listing page holds many stubs."""

    house: str
    external_id: str
    category: str
    detail_url: str
    lot_number: Optional[int] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    fuel: Optional[str] = None
    year: Optional[int] = None
    mileage_km: Optional[int] = None
    gearbox: Optional[str] = None
    starting_price: Optional[float] = None
    current_price_attr: Optional[float] = None
    closing_hint_at: Optional[datetime] = None
    storage_location: Optional[str] = None
    warranty: Optional[str] = None
    battery_soh_pct: Optional[float] = None


@dataclass
class DocumentRef:
    doc_type: str  # DocType
    url: str


@dataclass
class LotDetail:
    """What a detail page shows. VIN and plate are returned to the core only so it can
    hash them; they are never stored in clear text."""

    external_id: str
    fields: dict[str, object] = field(default_factory=dict)
    vin: Optional[str] = None
    registration: Optional[str] = None
    documents: list[DocumentRef] = field(default_factory=list)
    comments: Optional[str] = None
    photo_count: int = 0
    raw_specs: dict[str, str] = field(default_factory=dict)
    sale_url: Optional[str] = None


@dataclass
class SaleListing:
    sale: SaleRef
    stubs: list[LotStub]
    complete: bool
    reasons: list[str] = field(default_factory=list)
    pages_fetched: int = 0
    parse_failures: int = 0
    duplicates: int = 0


class AuctionAdapter(ABC):
    slug: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    base_url: ClassVar[str] = ""
    url_patterns: ClassVar[tuple[re.Pattern[str], ...]] = ()
    poll_cron: ClassVar[str] = "40 6 * * *"  # Europe/Paris (Phase 2 worker)
    min_interval_s: ClassVar[float] = 2.0

    _registry: ClassVar[dict[str, type["AuctionAdapter"]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.slug:
            existing = AuctionAdapter._registry.get(cls.slug)
            if existing is not None and existing is not cls:
                raise ValueError(f"Duplicate auction adapter slug {cls.slug!r}")
            AuctionAdapter._registry[cls.slug] = cls

    def __init__(self, http: "PoliteClient") -> None:
        self.http = http

    @abstractmethod
    def sale_from_url(self, url: str) -> SaleRef:
        """Build a SaleRef for a sale URL given on the command line."""

    @abstractmethod
    def discover_sales(self) -> list[SaleRef]:
        """All upcoming and running sales (Phase 2, after live recon)."""

    @abstractmethod
    def list_sale(self, sale: SaleRef, max_pages: Optional[int] = None) -> SaleListing:
        """Walk a sale's listing pages. Must report completeness honestly."""

    @abstractmethod
    def fetch_detail(self, stub: LotStub) -> LotDetail:
        """Fetch and parse one lot's detail page. Raises ParseError on unexpected layout."""

    @abstractmethod
    def fee_model(self, price: float, sale_kind: str, buyer: str, vat_regime: str) -> float:
        """All-in price a buyer pays for a given hammer/opening price (estimate)."""

    def handles(self, url: str) -> bool:
        return any(p.search(url) for p in self.url_patterns)
