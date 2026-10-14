"""Central configuration, read from environment variables with safe defaults.

Every setting can be overridden with an environment variable (see ``Settings.from_env``).
Relative paths are resolved against the project root, never the current working
directory, so scheduled runs (cron, Windows Task Scheduler) behave the same as
interactive ones.
"""
from __future__ import annotations

import codecs
import dataclasses
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, MutableMapping, Optional

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

# Cheapest OpenAI model not scheduled for retirement as of 2026-10-14 (prices and
# deprecations: see README "Optional: OpenAI review"). Override with LLM_MODEL.
DEFAULT_LLM_MODEL = "gpt-6-luna"
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

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


def _unquote(value: str) -> str:
    """Strip surrounding whitespace and one pair of matching quotes."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def clean_secret(value: Optional[str]) -> str:
    """Normalise a pasted secret: cmd's ``set KEY="sk-..."`` keeps the quotes."""
    return _unquote(value or "")


# --------------------------------------------------------------------------- .env file

_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class EnvFileReport:
    """What ``load_env_file`` did. Holds variable names only, never their values."""

    path: Path
    loaded: list[str] = field(default_factory=list)
    kept_existing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def env_file_path() -> Path:
    return _env_path("AUCTION_AGENT_ENV_FILE", PROJECT_DIR / ".env")


def _decode_env_bytes(data: bytes) -> str:
    # Windows PowerShell 5.1 writes UTF-16 with `>`; Notepad may add a UTF-8 BOM.
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def load_env_file(
    path: Optional[Path] = None, environ: Optional[MutableMapping[str, str]] = None
) -> EnvFileReport:
    """Load ``KEY=value`` lines from the git-ignored ``.env`` file.

    A variable that is already set (non-empty) in the environment always wins, so a
    shell or Task Scheduler setting can override the file. Problems with the file are
    reported as warnings and never stop the program: the LLM is optional.
    """
    env = os.environ if environ is None else environ
    path = Path(path) if path is not None else env_file_path()
    report = EnvFileReport(path)
    stray = path.with_name(path.name + ".txt")
    if stray.exists():
        report.warnings.append(
            f"Found '{stray.name}': Windows may have saved your settings with a hidden .txt "
            f"extension. Rename it to '{path.name}'."
        )
    if not path.is_file():
        return report
    try:
        text = _decode_env_bytes(path.read_bytes())
    except (OSError, UnicodeError) as exc:
        report.warnings.append(
            f"Could not read {path} ({exc.__class__.__name__}); save it as UTF-8. Continuing without it."
        )
        return report
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _ENV_KEY_RE.fullmatch(key):
            report.warnings.append(f"{path.name} line {number} ignored (expected KEY=value).")
            continue
        value = _unquote(value)
        if not value:
            continue
        if env.get(key):
            report.kept_existing.append(key)
            continue
        env[key] = value
        report.loaded.append(key)
    return report


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
    robots_token: str = "AuctionAgent"

    # Documents
    max_document_bytes: int = 15 * 1024 * 1024
    max_pdf_pages: int = 8
    quarantine_max_files: int = 50
    quarantine_ttl_days: int = 7

    # Ingest behaviour
    relist_detail_check: bool = True
    late_ct_recheck_hours: int = 48
    late_ct_budget: int = 50
    max_ingest_attempts: int = 5

    # Scoring
    buyer_profile: str = "particulier"  # 'particulier' | 'pro'
    engine_risk_max_tier: str = "yellow"  # 'yellow' | 'red'
    green_min: int = 80
    yellow_min: int = 60
    top_n: int = 10

    # LLM review (optional): enabled when OPENAI_API_KEY is set
    openai_api_key: str = field(default="", repr=False)
    llm_model: str = DEFAULT_LLM_MODEL
    llm_reasoning_effort: str = "none"
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
            robots_token=os.environ.get("AUCTION_AGENT_ROBOTS_TOKEN", "AuctionAgent"),
            late_ct_budget=_env_int("LATE_CT_BUDGET", 50),
            relist_detail_check=_env_bool("RELIST_DETAIL_CHECK", True),
            buyer_profile=os.environ.get("BUYER_PROFILE", "particulier"),
            engine_risk_max_tier=os.environ.get("ENGINE_RISK_MAX_TIER", "yellow"),
            green_min=_env_int("TIER_GREEN_MIN", 80),
            yellow_min=_env_int("TIER_YELLOW_MIN", 60),
            openai_api_key=clean_secret(os.environ.get("OPENAI_API_KEY")),
            llm_model=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_LLM_MODEL,
            llm_reasoning_effort=os.environ.get("LLM_REASONING_EFFORT", "").strip().lower() or "none",
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
        if self.llm_reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"LLM_REASONING_EFFORT must be one of: {', '.join(REASONING_EFFORTS)}")

    def replace(self, **changes: Any) -> "Settings":
        return dataclasses.replace(self, **changes)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key and self.llm_model)
