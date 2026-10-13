"""Central configuration, read from environment variables with safe defaults.

Every setting can be overridden with an environment variable (see ``Settings.from_env``).
Relative paths are resolved against the project root, never the current working
directory, so scheduled runs (cron, Windows Task Scheduler) behave the same as
interactive ones.
"""
from __future__ import annotations

import dataclasses
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

if sys.version_info < (3, 10):  # pragma: no cover - guard for old interpreters
    raise RuntimeError("Auction-Agent requires Python 3.10 or newer")

PROJECT_DIR = Path(__file__).resolve().parents[1]

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Kept from the original prototype so `python run_crawl.py` keeps its meaning:
# one Alcopa sale, passenger vehicles only.
DEFAULT_SALE_URL = (
    "/salle-de-vente-encheres/beauvais/10206"
    "?sort=number_asc&category_view=VP"
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def utc_now() -> datetime:
    """Current time, timezone-aware, truncated to whole seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_iso(dt: Optional[datetime] = None) -> str:
    """Format a datetime as the single timestamp format used in the database.

    All ``*_at`` columns use ``YYYY-MM-DDTHH:MM:SSZ`` (UTC) so that text comparison in
    SQL orders correctly. Naive datetimes are treated as UTC.
    """
    if dt is None:
        dt = utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_utc_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_DIR / path)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Build with ``Settings.from_env()``; override in tests."""

    data_dir: Path
    db_path: Path
    reports_dir: Path
    quarantine_dir: Path
    identity_key_path: Path

    # HTTP politeness
    user_agent: str = DEFAULT_USER_AGENT
    request_timeout: float = 30.0
    min_request_interval: float = 2.0
    max_retry_after_s: float = 60.0
    respect_robots: bool = True

    # Documents
    max_document_bytes: int = 15 * 1024 * 1024
    max_pdf_pages: int = 8
    quarantine_max_files: int = 50
    quarantine_ttl_days: int = 7

    # Ingest behaviour
    relist_detail_check: bool = True
    late_ct_recheck_hours: int = 48
    max_ingest_attempts: int = 5

    # Scoring
    buyer_profile: str = "particulier"  # 'particulier' | 'pro'
    engine_risk_max_tier: str = "yellow"  # 'yellow' | 'red'
    green_min: int = 80
    yellow_min: int = 60
    top_n: int = 10

    # LLM (optional; disabled unless all three are set)
    gemini_api_key: str = ""
    gemini_model: str = ""
    gemini_paid_tier: bool = False
    llm_limit: int = 60
    llm_batch_size: int = 20

    @classmethod
    def from_env(cls, **overrides: Any) -> "Settings":
        data_dir = Path(overrides["data_dir"]) if "data_dir" in overrides else _env_path(
            "AUCTION_AGENT_DATA_DIR", PROJECT_DIR / "data"
        )
        values: dict[str, Any] = dict(
            data_dir=data_dir,
            db_path=_env_path("AUCTION_AGENT_DB", data_dir / "auction.db"),
            reports_dir=_env_path("AUCTION_AGENT_REPORTS_DIR", PROJECT_DIR / "reports"),
            quarantine_dir=data_dir / "quarantine",
            identity_key_path=data_dir / "identity.key",
            user_agent=os.environ.get("AUCTION_AGENT_USER_AGENT", DEFAULT_USER_AGENT),
            request_timeout=_env_float("AUCTION_AGENT_REQUEST_TIMEOUT", 30.0),
            min_request_interval=_env_float("AUCTION_AGENT_MIN_INTERVAL", 2.0),
            respect_robots=_env_bool("AUCTION_AGENT_RESPECT_ROBOTS", True),
            relist_detail_check=_env_bool("RELIST_DETAIL_CHECK", True),
            buyer_profile=os.environ.get("BUYER_PROFILE", "particulier"),
            engine_risk_max_tier=os.environ.get("ENGINE_RISK_MAX_TIER", "yellow"),
            green_min=_env_int("TIER_GREEN_MIN", 80),
            yellow_min=_env_int("TIER_YELLOW_MIN", 60),
            gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
            gemini_model=os.environ.get("GEMINI_MODEL", ""),
            gemini_paid_tier=_env_bool("GEMINI_PAID_TIER", False),
            llm_limit=_env_int("LLM_LIMIT", 60),
        )
        values.update(overrides)
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.buyer_profile not in {"particulier", "pro"}:
            raise ValueError("BUYER_PROFILE must be 'particulier' or 'pro'")
        if self.engine_risk_max_tier not in {"yellow", "red"}:
            raise ValueError("ENGINE_RISK_MAX_TIER must be 'yellow' or 'red'")
        if not (0 < self.yellow_min < self.green_min <= 100):
            raise ValueError("Tier thresholds must satisfy 0 < yellow_min < green_min <= 100")

    def replace(self, **changes: Any) -> "Settings":
        return dataclasses.replace(self, **changes)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.gemini_api_key and self.gemini_model and self.gemini_paid_tier)
