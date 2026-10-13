"""CLI entry point to run the AI Advisor on crawled auction data."""
import argparse
import sys
import webbrowser

from rich.console import Console

from advisor.ai_advisor import analyze_vehicles, generate_html_report

console = Console()


def main():
    parser = argparse.ArgumentParser(
        description="Run AI advisor on crawled Alcopa Auction vehicle data."
    )
    parser.add_argument(
        "--sale-id",
        type=int,
        default=None,
        help="Specific sale ID to analyze (default: all)",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=None,
        help="Maximum starting price/bid filter in Euros (e.g. 15000)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Automatically open the HTML report in your default browser",
    )

    args = parser.parse_args()

    try:
        analysis = analyze_vehicles(
            sale_id=args.sale_id,
            max_price=args.max_price,
        )

        if not analysis:
            console.print("[red]No analysis generated. Check if you have crawled data first.[/red]")
            sys.exit(1)

        report_path = generate_html_report(analysis)

        if args.open:
            webbrowser.open(report_path.as_uri())

    except KeyboardInterrupt:
        print("\nAnalysis interrupted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
