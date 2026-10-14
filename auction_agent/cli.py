"""Command-line interface: ``python -m auction_agent <command>``.

Commands: crawl, score, report, advise (score + report), stats, capture, cleanup-legacy.
``run_crawl.py`` and ``run_advisor.py`` are thin shims kept for the README commands.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import webbrowser
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urljoin, urlsplit

from rich.console import Console

from . import db
from .adapters import get_adapter_class
from .adapters.alcopa import BASE_URL as ALCOPA_BASE_URL
from .config import DEFAULT_SALE_URL, PROJECT_DIR, Settings
from .http_client import FetchError, PoliteClient
from .documents import sweep_quarantine
from .ingest import CrawlOptions, run_crawl
from .privacy import redact

console = Console()


def _settings() -> Settings:
    return Settings.from_env()


# --------------------------------------------------------------------------- crawl


def cmd_crawl(args: argparse.Namespace, settings: Settings) -> int:
    adapter_cls = get_adapter_class(args.house)
    sale_url = urljoin(adapter_cls.base_url + "/", args.url)
    with PoliteClient(settings) as http:
        adapter = adapter_cls(http)
        report = run_crawl(
            settings,
            adapter,
            CrawlOptions(
                sale_url=sale_url,
                max_pages=args.pages,
                limit=args.limit,
                download_docs=not args.no_docs,
                force_unlock=args.force_unlock,
            ),
            log=lambda msg: console.print(msg, markup=False, highlight=False),
        )
    style = {"ok": "green", "incomplete": "yellow"}.get(report.status, "red")
    console.rule(f"[bold {style}]Crawl {report.status}[/bold {style}]")
    if report.sale_id is not None:
        console.print(f"  {'sale id':>14}: {report.sale_id}  (use with run_advisor.py --sale-id)")
    for key, value in report.counts.items():
        console.print(f"  {key:>14}: {value}")
    for reason in report.reasons:
        console.print(f"  [yellow]note[/yellow] {reason}", highlight=False, markup=True)
    for failure in report.failures[:10]:
        console.print(f"  [red]failed[/red] {failure['external_id']}: {failure['error']}", highlight=False)
    return 0 if report.status in ("ok", "incomplete") else 2


# --------------------------------------------------------------------------- scoring / report


def cmd_score(args: argparse.Namespace, settings: Settings) -> int:
    from .scoring.service import rescore_all

    with db.open_db(settings.db_path) as conn:
        result = rescore_all(conn, settings, use_llm=not args.no_llm, llm_limit=args.llm_limit,
                             log=lambda m: console.print(m, markup=False, highlight=False))
    console.print(f"Scored {result['scored']} vehicles "
                  f"(green {result['green']}, yellow {result['yellow']}, red {result['red']}); "
                  f"LLM calls {result['llm_calls']}, cached {result['llm_cached']}.")
    for note in result.get("notes", []):
        console.print(f"  [yellow]note[/yellow] {note}", highlight=False)
    return 0


def cmd_report(args: argparse.Namespace, settings: Settings) -> int:
    from .report import generate_report

    with db.open_db(settings.db_path) as conn:
        path = generate_report(conn, settings, sale_id=args.sale_id, max_price=args.max_price)
    console.print(f"[bold green]Report saved to {path}[/bold green]")
    if args.open:
        webbrowser.open(path.as_uri())
    return 0


def cmd_advise(args: argparse.Namespace, settings: Settings) -> int:
    cmd_score(args, settings)
    return cmd_report(args, settings)


# --------------------------------------------------------------------------- maintenance


def cmd_stats(args: argparse.Namespace, settings: Settings) -> int:
    with db.open_db(settings.db_path) as conn:
        stats = db.get_stats(conn)
        sales = conn.execute(
            "SELECT id, house, external_id, room, starts_at, status FROM sales ORDER BY starts_at DESC, id DESC LIMIT 20"
        ).fetchall()
    for key, value in stats.items():
        console.print(f"  {key:>18}: {value}", highlight=False)
    if sales:
        console.print("  sales (id = value for --sale-id):")
        for s in sales:
            console.print(f"    {s['id']:>4}  {s['house']} {s['external_id']}  {s['room'] or ''}  "
                          f"{(s['starts_at'] or '')[:10]}  {s['status']}", highlight=False)
    return 0


def cmd_capture(args: argparse.Namespace, settings: Settings) -> int:
    """Save real pages as redacted fixtures so parser tests run on real markup."""
    out_dir = Path(args.out) if args.out else PROJECT_DIR / "tests" / "fixtures" / "alcopa" / "real"
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = []
    if args.url:
        targets.append(("sale", urljoin(ALCOPA_BASE_URL + "/", args.url)))
    for detail in args.detail or []:
        targets.append(("detail", urljoin(ALCOPA_BASE_URL + "/", detail)))
    if not targets:
        console.print("[red]Nothing to capture: pass --url and/or --detail.[/red]")
        return 1
    with PoliteClient(settings) as http:
        for kind, url in targets:
            try:
                html = http.get_text(url)
            except FetchError as exc:
                console.print(f"[red]{url}: {exc}[/red]", highlight=False)
                return 2
            tail = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "page"
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", tail)[:80]
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
            target = (out_dir / f"{kind}-{safe}-{digest}.html").resolve()
            if target.parent != out_dir.resolve():
                console.print(f"[red]Refusing to write outside {out_dir}[/red]", highlight=False)
                return 2
            target.write_text(redact(html) or "", encoding="utf-8")
            name = target.name
            console.print(f"Saved {out_dir / name}", highlight=False)
    console.print("[yellow]Review these files by hand before committing them (they are git-ignored).[/yellow]")
    return 0


def cmd_cleanup_legacy(args: argparse.Namespace, settings: Settings) -> int:
    """Retire the v1 database, delete the prototype's PDF cache and expired quarantine."""
    actions: list[tuple[str, Path]] = []
    if db.is_legacy_v1(settings.db_path):
        actions.append(("retire v1 database (identifying data removed, file moved aside)", settings.db_path))
    # The prototype always wrote PDFs under PROJECT_DIR/data/documents; also check data_dir.
    for docs_dir in sorted({PROJECT_DIR / "data" / "documents", settings.data_dir / "documents"}):
        if docs_dir.exists():
            for child in sorted(docs_dir.iterdir()):
                if child.is_dir() and (child.name.startswith("lot_") or child.name.startswith("veh_")):
                    actions.append(("delete prototype PDF folder", child))
    if args.delete_backups:
        for folder in sorted({settings.db_path.parent, settings.data_dir}):
            for backup in sorted(folder.glob("auction.v1-*.db")):
                actions.append(("delete retired v1 database", backup))
    if not actions:
        removed = sweep_quarantine(settings) if args.yes else 0
        console.print(f"Nothing to clean up{f' (removed {removed} expired quarantined PDF(s))' if removed else ''}.")
        return 0
    for what, path in actions:
        console.print(f"  {what}: {path}", highlight=False)
    if not args.yes:
        console.print("[yellow]Dry run. Re-run with --yes to apply.[/yellow]")
        return 0
    for what, path in actions:
        if what.startswith("retire"):
            backup = db.retire_legacy_database(path)
            if backup is not None and args.delete_backups:
                backup.unlink(missing_ok=True)  # the backup created just now goes too
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    removed = sweep_quarantine(settings)
    console.print(f"[green]Done.[/green]{f' Removed {removed} expired quarantined PDF(s).' if removed else ''}")
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auction_agent", description="Auction-Agent command line")
    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="Crawl one sale (listing, detail pages, documents)")
    crawl.add_argument("--house", default="alcopa", help="Auction house adapter (default: alcopa)")
    crawl.add_argument("--url", default=DEFAULT_SALE_URL, help="Sale page URL (path or full URL)")
    crawl.add_argument("--pages", type=int, default=None, help="Maximum listing pages")
    crawl.add_argument("--limit", type=int, default=None, help="Maximum vehicles to process")
    crawl.add_argument("--no-docs", action="store_true", help="Skip document downloads")
    crawl.add_argument("--force-unlock", action="store_true", help="Release a stuck crawl lock first")
    crawl.set_defaults(func=cmd_crawl)

    def add_score_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--no-llm", action="store_true",
                       help="Heuristic scoring only (no LLM calls, cached LLM output ignored)")
        p.add_argument("--llm-limit", type=int, default=None, help="Maximum LLM calls this run")

    def add_report_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--sale-id", type=int, default=None, help="Only this sale (database id)")
        p.add_argument("--max-price", type=float, default=None, help="Maximum all-in price in euros")
        p.add_argument("--open", action="store_true", help="Open the report in a browser")

    score = sub.add_parser("score", help="Score every active vehicle")
    add_score_args(score)
    score.set_defaults(func=cmd_score)

    report = sub.add_parser("report", help="Write the HTML report")
    add_report_args(report)
    report.set_defaults(func=cmd_report)

    advise = sub.add_parser("advise", help="Score, then write the HTML report")
    add_score_args(advise)
    add_report_args(advise)
    advise.set_defaults(func=cmd_advise)

    stats = sub.add_parser("stats", help="Database statistics")
    stats.set_defaults(func=cmd_stats)

    capture = sub.add_parser("capture", help="Save redacted real pages as test fixtures")
    capture.add_argument("--url", help="Sale page URL")
    capture.add_argument("--detail", action="append", help="Lot detail URL (repeatable)")
    capture.add_argument("--out", help="Output directory")
    capture.set_defaults(func=cmd_capture)

    cleanup = sub.add_parser("cleanup-legacy", help="Remove prototype data (v1 DB, lot_* PDFs)")
    cleanup.add_argument("--yes", action="store_true", help="Apply (default is a dry run)")
    cleanup.add_argument("--delete-backups", action="store_true", help="Also delete retired v1 databases")
    cleanup.set_defaults(func=cmd_cleanup_legacy)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args, _settings()) or 0)
    except KeyboardInterrupt:
        console.print("\nInterrupted.")
        return 130
    except (db.LegacyDatabaseError, db.RunLockedError, RuntimeError, ValueError, KeyError) as exc:
        console.print(f"[red]{exc}[/red]", highlight=False)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
