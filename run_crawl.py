"""CLI entry point to run the Alcopa Auction crawler."""
import argparse
import sys

from crawler import run_full_crawl
from config import DEFAULT_SALE_URL


def main():
    parser = argparse.ArgumentParser(
        description="Crawl vehicle listings and spec documents from Alcopa Auction."
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_SALE_URL,
        help="Sale page URL path (relative to alcopa-auction.fr or full URL)",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=None,
        help="Maximum number of listing pages to crawl (default: all)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of vehicles to process (useful for testing)",
    )
    parser.add_argument(
        "--no-docs",
        action="store_true",
        help="Skip downloading PDFs (faster for quick inventory checks)",
    )

    args = parser.parse_args()

    try:
        run_full_crawl(
            sale_url=args.url,
            max_pages=args.pages,
            limit_vehicles=args.limit,
            download_docs=not args.no_docs,
        )
    except KeyboardInterrupt:
        print("\nCrawl interrupted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
