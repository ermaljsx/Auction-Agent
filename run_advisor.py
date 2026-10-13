"""CLI entry point to score vehicles and write the HTML report (kept for the README).

Equivalent to ``python -m auction_agent advise``. Same flags as the original prototype:
--sale-id, --max-price, --open; plus --no-llm and --llm-limit.
"""
import sys

from auction_agent.cli import main

if __name__ == "__main__":
    sys.exit(main(["advise", *sys.argv[1:]]))
