"""Command-line interface: ``python -m auction_agent <command>``.

Commands: crawl, publish, recon, score, report, advise (score + report), stats, llm-estimate,
preview, capture, cleanup-legacy.
``run_crawl.py`` and ``run_advisor.py`` are thin shims kept for the README commands.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urljoin, urlsplit

from rich.console import Console
from rich.markup import escape

from . import db
from .adapters import get_adapter_class
from .adapters.alcopa import BASE_URL as ALCOPA_BASE_URL, WEEKLY_SALE_QUERY
from .adapters._parsing import PARIS
from .config import (DEFAULT_SALE_URL, PROJECT_DIR, EnvFileReport, Settings, load_env_file, parse_utc_iso,
                     utc_iso, utc_now)
from .http_client import FetchError, PoliteClient
from .documents import sweep_quarantine
from .ingest import CrawlOptions, run_crawl
from .privacy import redact

console = Console()
_env_report: Optional[EnvFileReport] = None  # what main() loaded from .env (names only)


def _settings() -> Settings:
    return Settings.from_env()


def llm_status(settings: Settings, env_report: Optional[EnvFileReport] = None) -> str:
    """One line saying which provider and model run and where the key came from (never the key)."""
    key_name = settings.llm_key_name
    if not settings.llm_api_key:
        source = "not set (see .env.example)"
    elif env_report is not None and key_name in env_report.loaded:
        source = f"from {env_report.path.name}"
    else:
        source = "from the environment"
    return (f"LLM: {settings.llm_provider} {settings.llm_model}, reasoning effort "
            f"{settings.llm_reasoning_effort}, {key_name} {source}")


def _cost_text(cost: Optional[float]) -> str:
    return "cost unknown" if cost is None else f"cost about ${cost:.4f}"


# --------------------------------------------------------------------------- crawl


def _print_resolution(diag: dict) -> None:
    pages = ", ".join(f"{name}: {value}" for name, value in diag.get("pages", {}).items())
    console.print(f"  room {diag.get('room')}, today {diag.get('today')}; sale links found ({pages})",
                  markup=False, highlight=False)
    for c in diag.get("candidates", [])[:15]:
        console.print(f"    {c['id']:>6}  {c['date'] or '????-??-??'}  {c['text']}", markup=False, highlight=False)
    for name, hint in diag.get("hints", {}).items():
        console.print(f"  {name} page structure: {hint}", markup=False, highlight=False)
    chosen = diag.get("chosen")
    console.print(f"  chosen: {chosen['id']} on {chosen['date']}" if chosen else "  chosen: none",
                  markup=False, highlight=False)


def resolve_sale_url(adapter, url: str, today=None) -> Optional[str]:
    """A room URL becomes this week's sale URL (None if none was found); others pass through."""
    room = adapter.room_from_url(url) if hasattr(adapter, "room_from_url") else None
    if room is None:
        return url
    sale, diag = adapter.resolve_next_sale(room, today=today)
    _print_resolution(diag)
    return sale.url if sale else None


def cmd_crawl(args: argparse.Namespace, settings: Settings) -> int:
    adapter_cls = get_adapter_class(args.house)
    with PoliteClient(settings) as http:
        adapter = adapter_cls(http)
        sale_url = resolve_sale_url(adapter, urljoin(adapter_cls.base_url + "/", args.url))
        if sale_url is None:
            console.print("[red]No upcoming sale found for this room; pass a sale URL with --url.[/red]")
            return 3
        quiet = getattr(args, "quiet", False)
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
            log=(lambda _msg: None) if quiet else (lambda msg: console.print(msg, markup=False, highlight=False)),
        )
    style = {"ok": "green", "incomplete": "yellow"}.get(report.status, "red")
    console.rule(f"[bold {style}]Crawl {report.status}[/bold {style}]")
    if report.sale_id is not None:
        console.print(f"  {'sale id':>14}: {report.sale_id}  (use with run_advisor.py --sale-id)")
    for key, value in report.counts.items():
        console.print(f"  {key:>14}: {value}")
    reasons = report.reasons
    if quiet:  # public logs: no per-car URLs (detail pages, tokenised document links)
        reasons = list(dict.fromkeys(re.sub(r"https?://\S+", "<url>", r) for r in reasons))
    for reason in reasons:
        console.print(f"  [yellow]note[/yellow] {escape(reason)}", highlight=False)
    if quiet:  # public logs (GitHub Actions): counts only, no per-car lines
        if report.failures:
            console.print(f"  [red]failed[/red] {len(report.failures)} vehicle(s)", highlight=False)
    else:
        for failure in report.failures[:10]:
            console.print(f"  [red]failed[/red] {escape(str(failure['external_id']))}: {escape(str(failure['error']))}",
                          highlight=False)
    return 0 if report.status in ("ok", "incomplete") else 2


def _redact_urls(lines: list[str]) -> list[str]:
    return list(dict.fromkeys(re.sub(r"https?://\S+", "<url>", line) for line in lines))


def cmd_publish(args: argparse.Namespace, settings: Settings, now: Optional[datetime] = None) -> int:
    """This week's run: find the sale, crawl it, score it, upload the snapshot for the site.

    Exit codes: 0 ok · 2 blocked, failed or upload refused · 3 no sale found this week ·
    4 the sale is live but nothing could be put forward, or the AI review did not run.
    A snapshot is uploaded in every case except a failed upload, so the site always shows
    what happened (the cars from the last good crawl stay visible after a blocked run).
    """
    from .blobstore import SNAPSHOT_PATH, LocalStore, StoreError, VercelBlobStore
    from .http_client import BlockedError, RateLimitedError, RobotsDisallowedError
    from .scoring.service import rescore_all
    from .snapshot import build_snapshot, encode

    try:
        store = LocalStore(Path(args.out)) if args.out else VercelBlobStore()
    except StoreError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]", highlight=False)
        return 2
    say = (lambda msg: console.print(msg, markup=False, highlight=False))
    # The scheduled log shows which AI runs and where its key came from (never the key).
    console.print("LLM: off (--no-llm)" if args.no_llm else llm_status(settings, _env_report),
                  markup=False, highlight=False, soft_wrap=True)
    clock = (lambda: now) if now is not None else utc_now  # tests pin the time
    today = clock().astimezone(PARIS).date()
    code = 0
    sale_id = None
    crawl: dict = {"status": "skipped" if args.no_crawl else "running", "started_at": utc_iso(clock())}
    if not args.no_crawl:
        adapter_cls = get_adapter_class(args.house)
        with PoliteClient(settings) as http:
            adapter = adapter_cls(http)
            try:
                sale_url = resolve_sale_url(adapter, urljoin(adapter_cls.base_url + "/", args.url), today=today)
            except (BlockedError, RateLimitedError, RobotsDisallowedError) as exc:
                sale_url, code = None, 2
                crawl.update(status="blocked", reasons=_redact_urls([str(exc)]))
            except FetchError as exc:
                sale_url, code = None, 2
                crawl.update(status="failed", reasons=_redact_urls([str(exc)]))
            else:
                if sale_url is None:
                    code = 3
                    crawl.update(status="no_sale", reasons=["no upcoming Monday sale found on the room page"])
            if sale_url is not None:
                report = run_crawl(settings, adapter, CrawlOptions(sale_url=sale_url), log=say,
                                   now_fn=clock)
                sale_id = report.sale_id
                crawl.update(status=report.status, counts=dict(report.counts),
                             failed=len(report.failures), reasons=_redact_urls(report.reasons))
                if report.status not in ("ok", "incomplete"):
                    code = 2
    crawl["finished_at"] = utc_iso(clock())
    use_llm = not args.no_llm
    with db.open_db(settings.db_path) as conn:
        if sale_id is None:  # blocked or no crawl: show the latest sale we know, as it was
            row = conn.execute("SELECT id FROM sales ORDER BY starts_at DESC, id DESC LIMIT 1").fetchone()
            sale_id = row["id"] if row else None
        # Every car the site lists for this sale gets an AI verdict (scoring.service.plan_reviews).
        result = rescore_all(conn, settings, use_llm=use_llm, llm_limit=args.llm_limit, log=say,
                             now=clock(), today=today, sale_id=sale_id)
        llm = {
            "enabled": use_llm and settings.llm_enabled,
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "reasoning_effort": settings.llm_reasoning_effort,
            "triggered": result["llm_triggered"],
            "calls": result["llm_calls"],
            "cached": result["llm_cached"],
            "tokens_in": result.get("llm_tokens_in", 0),
            "tokens_out": result.get("llm_tokens_out", 0),
            "cost_usd": result.get("llm_cost_usd"),
            "error": result.get("llm_error"),
            "notes": _redact_urls(result.get("notes", [])),
        }
        snapshot = build_snapshot(conn, settings, sale_id=sale_id, crawl=crawl, llm=llm, now=clock())
    data = encode(snapshot)
    try:
        store.put(SNAPSHOT_PATH, data, "application/json; charset=utf-8")
    except StoreError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]", highlight=False)
        return 2
    sale = snapshot["sale"] or {}
    say(f"Snapshot {'written' if args.out else 'uploaded'} to {store.where}: sale {sale.get('external_id')} on {sale.get('date')} "
        f"({sale.get('state', 'none')}), {len(snapshot['top'])} top pick(s)"
        f"{' (provisional)' if snapshot['provisional'] else ''}, {len(snapshot['cards'])} more listed; "
        f"crawl {crawl['status']}; AI calls {llm['calls']}, cached {llm['cached']}"
        f"{', ' + _cost_text(llm['cost_usd']) if llm['calls'] else ''}.")
    if code == 0 and sale.get("state") in ("upcoming", "today"):
        problems = []
        if not snapshot["top"]:
            problems.append("no car could be put forward")
        if use_llm and not llm["enabled"]:
            hint = settings.llm_other_key_hint
            problems.append(f"AI review is off: set {settings.llm_key_name} in .env" + (f" ({hint})" if hint else ""))
        elif use_llm and llm["error"]:
            problems.append(f"AI review stopped ({llm['error']} error, see notes)")
        elif use_llm and snapshot["top"] and llm["calls"] + llm["cached"] == 0:
            problems.append("no AI review was made")
        for problem in problems:
            console.print(f"  [yellow]attention[/yellow] {escape(problem)}", highlight=False)
        if problems:
            code = 4
        coverage = snapshot["llm"].get("coverage") or {}
        if use_llm and llm["enabled"] and not llm["error"] and coverage.get("with_verdict", 0) < coverage.get("listed", 0):
            # Informative only (exit code unchanged): e.g. the budget ran out or a batch failed.
            console.print(f"  [yellow]attention[/yellow] AI verdicts on {coverage['with_verdict']} of the "
                          f"{coverage['listed']} listed cars (see notes)", highlight=False)
    return code


def cmd_recon(args: argparse.Namespace, settings: Settings) -> int:
    """Live check before any crawl: robots.txt rules, then this week's sale for a room."""
    adapter_cls = get_adapter_class(args.house)
    base = adapter_cls.base_url
    paths = {
        "room page": f"/salle-de-vente-encheres/{args.room}",
        "sales calendar": "/calendrier-des-ventes",
        "sale listing": f"/salle-de-vente-encheres/{args.room}/1?{WEEKLY_SALE_QUERY}&page=1",
        "lot detail": "/voiture-occasion/marque/modele-123456",
        "CT document": "/getDocument/ct/x",
    }
    with PoliteClient(settings) as http:
        console.print("robots.txt:", markup=False)
        for name, path in paths.items():
            verdict = "allowed" if http.robots_allows(base + path) else "DISALLOWED"
            console.print(f"  {name:>15}: {verdict}", markup=False, highlight=False)
        adapter = adapter_cls(http)
        sale, diag = adapter.resolve_next_sale(args.room)
    console.print("this week's sale:", markup=False)
    _print_resolution(diag)
    if sale is None:
        return 3
    console.print(f"  url: {sale.url}", markup=False, highlight=False)
    return 0


# --------------------------------------------------------------------------- scoring / report


def cmd_score(args: argparse.Namespace, settings: Settings) -> int:
    from .scoring.service import rescore_all

    status = "LLM: off (--no-llm)" if args.no_llm else llm_status(settings, _env_report)
    console.print(status, markup=False, highlight=False, soft_wrap=True)
    with db.open_db(settings.db_path) as conn:
        result = rescore_all(conn, settings, use_llm=not args.no_llm, llm_limit=args.llm_limit,
                             log=lambda m: console.print(m, markup=False, highlight=False))
    calls = result['llm_calls']
    console.print(f"Scored {result['scored']} vehicles "
                  f"(green {result['green']}, yellow {result['yellow']}, red {result['red']}); "
                  f"LLM calls {calls}, cached {result['llm_cached']}"
                  f"{', ' + _cost_text(result.get('llm_cost_usd')) if calls else ''}.")
    for note in result.get("notes", []):
        console.print(f"  [yellow]note[/yellow] {escape(note)}", highlight=False)
    return 0


def cmd_report(args: argparse.Namespace, settings: Settings) -> int:
    from .report import generate_report

    with db.open_db(settings.db_path) as conn:
        path = generate_report(conn, settings, sale_id=args.sale_id, max_price=args.max_price)
    console.print(f"[bold green]Report saved to {escape(str(path))}[/bold green]")
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
        summary = db.assessment_summary(conn)
    for key, value in stats.items():
        console.print(f"  {key:>18}: {value}", highlight=False)
    console.print(f"  {'confidence':>18}: full {summary['full']}, partial {summary['partial']}", highlight=False)
    for reason, count in summary["partial_reasons"].items():
        console.print(f"  {'partial because':>18}: {reason} × {count}", markup=False, highlight=False)
    if sales:
        console.print("  sales (id = value for --sale-id):")
        for s in sales:
            console.print(f"    {s['id']:>4}  {s['house']} {s['external_id']}  {s['room'] or ''}  "
                          f"{_paris_day(s['starts_at'])}  {s['status']}", markup=False, highlight=False)
    return 0


def cmd_llm_estimate(args: argparse.Namespace, settings: Settings) -> int:
    """Offline AI cost estimate: real payloads from the database, no client, no network."""
    from .scoring.estimate import estimate_llm_cost, format_estimate, resolve_sale_id

    if args.runs_per_week <= 0:
        console.print("[red]--runs-per-week must be above 0.[/red]")
        return 2
    with db.open_db(settings.db_path) as conn:
        sale_id = resolve_sale_id(conn, args.sale_id)
        if sale_id is None:
            what = f"No sale {args.sale_id} in the database" if args.sale_id is not None else "No sale in the database"
            console.print(f"{what}: run a crawl first (python -m auction_agent stats lists the sales).",
                          markup=False, highlight=False)
            return 3
        est = estimate_llm_cost(conn, settings, sale_id=sale_id, all_cars=args.all,
                                runs_per_week=args.runs_per_week)
    for line in format_estimate(est):
        console.print(line, markup=False, highlight=False, soft_wrap=True)  # keep the table on one line
    return 0


def _paris_day(starts_at: Optional[str]) -> str:
    """The sale day in Paris: a Monday sale is stored as Sunday 23:00 UTC."""
    starts = parse_utc_iso(starts_at) if starts_at else None
    return starts.astimezone(PARIS).date().isoformat() if starts else ""


def cmd_preview(args: argparse.Namespace, settings: Settings) -> int:
    """Serve the site on this computer with a local (or the uploaded) snapshot."""
    from .preview import blob_source, folder_source, make_server

    if args.blob:
        source, what = blob_source(), "the private Vercel Blob store (BLOB_READ_WRITE_TOKEN)"
    else:
        folder = Path(args.snapshot) if args.snapshot else settings.data_dir / "snapshot-local"
        source, what = folder_source(folder), str(folder / "snapshot" / "latest.json")
    try:
        server = make_server(source, port=args.port)
    except (OSError, OverflowError) as exc:
        console.print(f"[red]Port {args.port} is not free ({escape(exc.__class__.__name__)}): "
                      f"try --port {args.port + 1}[/red]", highlight=False)
        return 2
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    console.print(f"Site preview: {url}  (snapshot: {what})", markup=False, highlight=False)
    console.print("Only this computer can open it. Press Ctrl+C to stop.", markup=False, highlight=False)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("Preview stopped.")
    finally:
        server.server_close()
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
                console.print(f"[red]{escape(url)}: {escape(str(exc))}[/red]", highlight=False)
                return 2
            tail = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "page"
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", tail)[:80]
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
            target = (out_dir / f"{kind}-{safe}-{digest}.html").resolve()
            if target.parent != out_dir.resolve():
                console.print(f"[red]Refusing to write outside {escape(str(out_dir))}[/red]", highlight=False)
                return 2
            target.write_text(redact(html) or "", encoding="utf-8")
            name = target.name
            console.print(f"Saved {out_dir / name}", markup=False, highlight=False)
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
        console.print(f"  {what}: {path}", markup=False, highlight=False)
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


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{value!r} is not a port (0 to 65535)")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auction_agent", description="Auction-Agent command line")
    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="Crawl one sale (listing, detail pages, documents)")
    crawl.add_argument("--house", default="alcopa", help="Auction house adapter (default: alcopa)")
    crawl.add_argument("--url", default=DEFAULT_SALE_URL,
                       help="Sale URL, or a room URL to crawl this week's sale there (default: Beauvais)")
    crawl.add_argument("--pages", type=int, default=None, help="Maximum listing pages")
    crawl.add_argument("--limit", type=int, default=None, help="Maximum vehicles to process")
    crawl.add_argument("--no-docs", action="store_true", help="Skip document downloads")
    crawl.add_argument("--force-unlock", action="store_true", help="Release a stuck crawl lock first")
    crawl.add_argument("--quiet", action="store_true", help="Counts only, no per-vehicle lines (public logs)")
    crawl.set_defaults(func=cmd_crawl)

    publish = sub.add_parser("publish", help="This week's run: crawl, score and upload the snapshot for the site")
    publish.add_argument("--house", default="alcopa", help="Auction house adapter (default: alcopa)")
    publish.add_argument("--url", default=DEFAULT_SALE_URL,
                         help="Room URL (this week's sale is found there) or one sale URL (default: Beauvais)")
    publish.add_argument("--no-crawl", action="store_true", help="Skip the crawl: rescore and publish what is stored")
    publish.add_argument("--no-llm", action="store_true", help="Heuristic scoring only")
    publish.add_argument("--llm-limit", type=int, default=None, help="Maximum vehicles sent to the LLM this run")
    publish.add_argument("--out", help="Write the snapshot to this folder instead of uploading it (dry run)")
    publish.set_defaults(func=cmd_publish)

    recon = sub.add_parser("recon", help="Check robots.txt and find this week's sale (2-4 requests)")
    recon.add_argument("--house", default="alcopa", help="Auction house adapter (default: alcopa)")
    recon.add_argument("--room", default="beauvais", help="Room slug, as in /salle-de-vente-encheres/<room>")
    recon.set_defaults(func=cmd_recon)

    def add_score_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--no-llm", action="store_true",
                       help="Heuristic scoring only (no LLM calls, cached LLM output ignored)")
        p.add_argument("--llm-limit", type=int, default=None, help="Maximum vehicles sent to the LLM this run")

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

    estimate = sub.add_parser("llm-estimate",
                              help="Estimate the AI review cost from the stored cars (offline, no API call)")
    estimate.add_argument("--sale-id", type=int, default=None,
                          help="Sale (database id or the auction house's number; default: the latest sale)")
    estimate.add_argument("--all", action="store_true",
                          help="Price a review of every active car of the sale instead of the listed ones")
    estimate.add_argument("--runs-per-week", type=float, default=2.0,
                          help="Scoring runs per week (default: 2, Thursday and Saturday)")
    estimate.set_defaults(func=cmd_llm_estimate)

    preview = sub.add_parser("preview", help="Open the site on this computer with a local snapshot")
    preview.add_argument("--snapshot", help="Folder written by `publish --out` (default: data/snapshot-local)")
    preview.add_argument("--blob", action="store_true",
                         help="Show the snapshot uploaded to the private Vercel Blob store instead")
    preview.add_argument("--port", type=_port, default=8765, help="Local port (default: 8765)")
    preview.add_argument("--open", action="store_true", help="Open it in the browser")
    preview.set_defaults(func=cmd_preview)

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
    global _env_report
    parser = build_parser()
    args = parser.parse_args(argv)
    # Every entry point goes through here, so .env (API key, IDENTITY_HMAC_KEY…) is
    # always applied the same way. Values already set in the environment win.
    _env_report = load_env_file()
    for warning in _env_report.warnings:
        console.print(f"[yellow]warning[/yellow] {escape(warning)}", highlight=False)
    try:
        return int(args.func(args, _settings()) or 0)
    except KeyboardInterrupt:
        console.print("\nInterrupted.")
        return 130
    except (db.LegacyDatabaseError, db.RunLockedError, RuntimeError, ValueError, KeyError) as exc:
        console.print(f"[red]{escape(str(exc))}[/red]", highlight=False)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
