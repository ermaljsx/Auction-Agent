"""Central configuration for the Alcopa Auction Crawler and AI Advisor."""
import os
from pathlib import Path

# Base URLs
BASE_URL = "https://www.alcopa-auction.fr"
DEFAULT_SALE_URL = (
    "/salle-de-vente-encheres/beauvais/10206"
    "?sort=number_asc&category_view=VP"
)

# Crawl settings
REQUEST_DELAY_SECONDS = 1.5   # Polite delay between requests
REQUEST_TIMEOUT = 30           # Timeout for HTTP requests in seconds
MAX_RETRIES = 3                # Retry failed requests
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Paths
PROJECT_DIR = Path(__file__).parent.resolve()
DATA_DIR = PROJECT_DIR / "data"
DB_PATH = DATA_DIR / "auction.db"
DOCUMENTS_DIR = DATA_DIR / "documents"
REPORTS_DIR = PROJECT_DIR / "reports"

# AI Advisor
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_BUDGET_MAX = None  # None = no limit, or e.g. 15000 for €15,000 max
TOP_RECOMMENDATIONS_COUNT = 10
