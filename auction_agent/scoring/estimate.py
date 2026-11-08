"""Offline AI cost estimate (``python -m auction_agent llm-estimate``).

Builds the real payloads from the database with the same planning as a scoring run
(``service.plan_reviews``), chunks them the same way, and prices them for every model in
``pricing.PRICES``. It never builds an LLM client, never calls a network API and never
writes to the database. Token counts are estimates (characters ÷ ``CHARS_PER_TOKEN``);
answers use fixed guesses until real reviews are stored, then their measured average.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

from ..adapters._parsing import PARIS
from ..config import DEFAULT_REASONING_EFFORTS, Settings, parse_utc_iso, utc_now
from .llm import PROMPT_VERSION, RESPONSE_SCHEMA, SYSTEM_PROMPT, build_prompt, payload_hash, payload_json
from .pricing import (
    BATCH_DISCOUNT,
    CHARS_PER_TOKEN,
    OUTPUT_TOKENS_TYPICAL,
    OUTPUT_TOKENS_WORST,
    PRICES,
    THINKING_TOKENS_PER_BATCH,
    cost_usd,
)
from .service import _chunks, llm_payload, plan_reviews

WEEKS_PER_MONTH = 52 / 12
# Thinking ceiling per batch (the max_tokens room the Claude client gives each effort level).
THINKING_TOKENS_WORST = {"none": 0, "off": 0, "minimal": 1_000, "low": 3_000, "medium": 6_000, "high": 10_000,
                         "xhigh": 16_000, "max": 16_000}
MIN_MEASURED_REVIEWS = 5  # below this, the fixed guesses are used for the answers


def resolve_sale_id(conn: sqlite3.Connection, sale_id: Optional[int]) -> Optional[int]:
    """The database id of the sale: the given id, the auction house's sale number, or the latest sale."""
    if sale_id is None:
        row = conn.execute("SELECT id FROM sales ORDER BY starts_at DESC, id DESC LIMIT 1").fetchone()
        return int(row[0]) if row else None
    if conn.execute("SELECT 1 FROM sales WHERE id = ?", (sale_id,)).fetchone():
        return int(sale_id)
    row = conn.execute("SELECT id FROM sales WHERE external_id = ?", (str(sale_id),)).fetchone()
    return int(row[0]) if row else None


def _measured_output(conn: sqlite3.Connection, model: str) -> tuple[Optional[float], int]:
    """Average output tokens per reviewed car (thinking included) for this model and prompt."""
    row = conn.execute(
        """SELECT sum(tokens_out), count(*) FROM llm_assessments
           WHERE status = 'ok' AND prompt_version = ? AND (llm_model = ? OR llm_model LIKE ?)""",
        (PROMPT_VERSION, model, f"{model}-%")).fetchone()
    total, n = row[0], int(row[1] or 0)
    if not n or total is None or n < MIN_MEASURED_REVIEWS:
        return None, n
    return total / n, n


def estimate_llm_cost(conn: sqlite3.Connection, settings: Settings, *, sale_id: int, all_cars: bool = False,
                      runs_per_week: float = 2.0, now: Optional[datetime] = None) -> dict[str, Any]:
    """Everything ``llm-estimate`` prints, as data. Reads only."""
    now_dt = now or utc_now()
    today = now_dt.astimezone(PARIS).date()
    model = settings.llm_model or "none"
    plan = plan_reviews(conn, settings, now_dt=now_dt, today=today, model_name=model, sale_id=sale_id)
    if all_cars:  # one-off review of the whole sale, whatever its date
        items = []
        for part in plan.partials:
            if part.facts.vehicle_id in plan.pool:
                payload = llm_payload(part)
                items.append((part, "verdict", payload, payload_hash(payload, model)))
    else:
        items = list(plan.triggered)
    reviewed = {(r[0], r[1]) for r in conn.execute(
        "SELECT vehicle_id, llm_input_hash FROM llm_assessments WHERE status = 'ok'")}
    to_send = [t for t in items if (t[0].facts.vehicle_id, t[3]) not in reviewed]
    cap = None if all_cars else max(0, settings.llm_limit)
    sent = to_send if cap is None else to_send[:cap]
    worst = items if cap is None else items[:cap]
    size = settings.llm_batch_size
    sent_batches, worst_batches = _chunks(sent, size), _chunks(worst, size)

    overhead_chars = (len(SYSTEM_PROMPT) + len(json.dumps(RESPONSE_SCHEMA, ensure_ascii=False))
                      + len(build_prompt([])))
    payload_chars = [len(payload_json(t[2])) for t in items]

    def batch_input(batch: list[tuple]) -> int:
        chars = overhead_chars + sum(len(payload_json(t[2])) + 2 for t in batch)
        return int(round(chars / CHARS_PER_TOKEN))

    measured, measured_n = _measured_output(conn, model)
    models = []
    for name, price in PRICES.items():
        effort = (settings.llm_reasoning_effort if price.provider == settings.llm_provider
                  else DEFAULT_REASONING_EFFORTS.get(price.provider, "none"))
        use_measured = measured is not None and name == model

        def run_cost(batches: list[list[tuple]], per_car: float, thinking: int) -> Optional[float]:
            total = 0.0
            for batch in batches:
                c = cost_usd(name, batch_input(batch), int(round(per_car * len(batch) + thinking)))
                if c is None:
                    return None
                total += c
            return total

        typical_car = measured if use_measured else OUTPUT_TOKENS_TYPICAL
        typical_thinking = 0 if use_measured else THINKING_TOKENS_PER_BATCH.get(effort, 0)
        typical = run_cost(sent_batches, typical_car, typical_thinking) or 0.0
        worst_cost = run_cost(worst_batches, OUTPUT_TOKENS_WORST, THINKING_TOKENS_WORST.get(effort, 0)) or 0.0
        models.append({
            "model": name, "provider": price.provider, "effort": effort, "measured": use_measured,
            "run_typical": typical, "run_worst": worst_cost,
            "week_typical": typical * runs_per_week, "week_worst": worst_cost * runs_per_week,
            "month_typical": typical * runs_per_week * WEEKS_PER_MONTH,
            "month_worst": worst_cost * runs_per_week * WEEKS_PER_MONTH,
            "batch_month_typical": typical * runs_per_week * WEEKS_PER_MONTH * BATCH_DISCOUNT,
            "batch_month_worst": worst_cost * runs_per_week * WEEKS_PER_MONTH * BATCH_DISCOUNT,
            "price": price,
        })
    sale = conn.execute("SELECT external_id, starts_at FROM sales WHERE id = ?", (sale_id,)).fetchone()
    starts = parse_utc_iso(sale["starts_at"]) if sale is not None and sale["starts_at"] else None
    biggest = max((batch_input(b) for b in worst_batches), default=0)
    threshold = getattr(PRICES.get(model), "long_threshold", None)
    return {
        "sale_external_id": sale["external_id"] if sale is not None else None,
        "sale_id": sale_id,
        "sale_date": starts.astimezone(PARIS).date().isoformat() if starts else None,
        "provider": settings.llm_provider,
        "model": model,
        "effort": settings.llm_reasoning_effort,
        "all_cars": all_cars,
        "pool": len(plan.pool),
        "upcoming_in_sale": len(plan.pool & plan.upcoming),
        "listed": plan.listed_count,
        "items": len(items),
        "kinds": {k: sum(1 for t in items if t[1] == k) for k in ("verdict", "adjust")},
        "cached": len(items) - len(to_send),
        "to_send": len(sent),
        "capped": len(to_send) - len(sent),
        "llm_limit": None if all_cars else settings.llm_limit,
        "batches": len(sent_batches),
        "payload_mean": (sum(payload_chars) / len(payload_chars)) if payload_chars else 0.0,
        "payload_max": max(payload_chars, default=0),
        "overhead_chars": overhead_chars,
        "input_tokens_run": sum(batch_input(b) for b in sent_batches),
        "biggest_batch_tokens": biggest,
        "long_threshold": threshold,
        "measured_output": measured,
        "measured_n": measured_n,
        "runs_per_week": runs_per_week,
        "models": models,
    }


def _usd(x: float) -> str:
    return f"${x:.4f}"


def format_estimate(est: dict[str, Any]) -> list[str]:
    """The lines ``llm-estimate`` prints."""
    lines = ["AI cost estimate (offline: no API call, nothing written to the database)"]
    lines.append(f"Sale {est['sale_external_id']} (id {est['sale_id']}) on {est['sale_date'] or 'unknown date'}; "
                 f"provider {est['provider']}, model {est['model']}, reasoning effort {est['effort']}.")
    if est["all_cars"]:
        lines.append(f"Cars (--all): every active car of the sale, {est['items']}; LLM_LIMIT is ignored.")
    else:
        lines.append(f"Cars: {est['listed']} listed on the site; {est['items']} would be reviewed "
                     f"(verdict {est['kinds']['verdict']}, adjust {est['kinds']['adjust']}); "
                     f"{est['upcoming_in_sale']} of the sale's {est['pool']} cars end within 7 days.")
    send = f"{est['cached']} already reviewed (cached); {est['to_send']} to send this run in {est['batches']} batch(es)"
    if est["capped"]:
        send += f"; {est['capped']} left for a later run (LLM_LIMIT {est['llm_limit']})"
    lines.append(send + ".")
    if not est["items"]:
        lines.append("No car would be sent now: the sale is over, more than 7 days away, or has no listed car "
                     "(no AI call is made then). Use --all to price a review of the whole sale.")
        return lines
    lines.append(f"Payload per car: mean {est['payload_mean']:,.0f} chars, max {est['payload_max']:,}; "
                 f"fixed part per request {est['overhead_chars']:,} chars (instructions, answer schema).")
    threshold = est["long_threshold"]
    lines.append(f"Input: about {est['input_tokens_run']:,} tokens this run ({CHARS_PER_TOKEN:g} chars per token, "
                 f"an estimate); largest batch about {est['biggest_batch_tokens']:,} tokens"
                 + (f" (the dearer rate card starts above {threshold:,})." if threshold else "."))
    if threshold and est["biggest_batch_tokens"] > threshold:
        lines.append(f"warning: a batch would exceed {threshold:,} input tokens and be billed on the dearer rate card.")
    if est["measured_output"] is not None:
        lines.append(f"Output per car: {est['measured_output']:,.0f} tokens measured on {est['measured_n']} stored "
                     f"reviews of {est['model']} (thinking included); {OUTPUT_TOKENS_WORST} per car in the worst case.")
    else:
        lines.append(f"Output per car (guesses until real reviews are stored): {OUTPUT_TOKENS_TYPICAL} tokens "
                     f"typical, {OUTPUT_TOKENS_WORST} worst, plus thinking per batch by effort "
                     f"(low {THINKING_TOKENS_PER_BATCH['low']:,} typical, {THINKING_TOKENS_WORST['low']:,} worst).")
    runs = est["runs_per_week"]
    runs_txt = f"{runs:g}"
    lines.append("")
    header = (f"{'Model':<18} {'Effort':<7} {'Per run':>19}  {f'Per week (x{runs_txt})':>19}  "
              f"{'Per month':>19}  {'Batch API, month':>19}")
    lines.append(header)
    for m in est["models"]:
        def pair(a: float, b: float) -> str:
            return f"{_usd(a)} - {_usd(b)}"
        lines.append(f"{m['model']:<18} {m['effort']:<7} {pair(m['run_typical'], m['run_worst']):>19}  "
                     f"{pair(m['week_typical'], m['week_worst']):>19}  {pair(m['month_typical'], m['month_worst']):>19}  "
                     f"{pair(m['batch_month_typical'], m['batch_month_worst']):>19}")
    lines.append("")
    lines.append("Each range is typical - worst. Typical: the cars not reviewed yet, short answers. Worst: every "
                 "triggered car reviewed again with the longest answers. Unchanged cars are cached, so a second "
                 "run in the same week usually costs less. The Batch API (half price, results within 24 h) is "
                 "not used yet. The provider's spend limit is the real cap.")
    for m in est["models"]:
        p = m["price"]
        card = (f" (${p.long_input_per_mtok:.2f} / ${p.long_output_per_mtok:.2f} above {p.long_threshold:,} prompt "
                "tokens)") if p.long_threshold else ""
        lines.append(f"Price {m['model']}: ${p.input_per_mtok:.2f} / ${p.output_per_mtok:.2f} per 1M input / output "
                     f"tokens{card}; source: {p.source}.")
    return lines
