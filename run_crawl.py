"""CLI entry point to run the crawler (kept for the README commands).

Equivalent to ``python -m auction_agent crawl``. Same flags as the original prototype:
--url (one sale), --pages, --limit, --no-docs; plus --force-unlock.
"""
import sys

from auction_agent.cli import main

if __name__ == "__main__":
    sys.exit(main(["crawl", *sys.argv[1:]]))
