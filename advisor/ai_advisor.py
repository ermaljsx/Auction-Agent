"""AI Advisor analyzing auction vehicles and producing structured recommendations."""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    REPORTS_DIR,
    TOP_RECOMMENDATIONS_COUNT,
)
from database import get_all_vehicles_with_documents
from pdf_extractor import parse_controle_technique

console = Console()


def format_vehicle_for_prompt(v: dict[str, Any]) -> dict[str, Any]:
    """Format vehicle data into a compact summary for the AI prompt."""
    docs = v.get("documents_parsed", {})
    ct_info = None

    # Parse CT text if available
    if "ct" in docs and docs["ct"].get("extracted_text"):
        ct_info = parse_controle_technique(docs["ct"]["extracted_text"])

    return {
        "id": v.get("id"),
        "lot_number": v.get("lot_number"),
        "brand": v.get("brand"),
        "model": v.get("model"),
        "trim": v.get("trim"),
        "year": v.get("year"),
        "registration_date": v.get("first_registration_date"),
        "mileage_km": v.get("mileage_km"),
        "fuel_type": v.get("fuel_type"),
        "gearbox": v.get("gearbox"),
        "co2": v.get("co2"),
        "starting_price": v.get("starting_price"),
        "current_bid": v.get("current_bid"),
        "warranty": v.get("warranty"),
        "battery_health": v.get("battery_health"),
        "comments": v.get("comments"),
        "detail_url": v.get("detail_url"),
        "ct_result": ct_info.get("result") if ct_info else "NO_CT_FILE",
        "ct_critical_defects": ct_info.get("defects_critical", []) if ct_info else [],
        "ct_major_defects": ct_info.get("defects_major", []) if ct_info else [],
        "ct_minor_defects_count": len(ct_info.get("defects_minor", [])) if ct_info else 0,
    }


def call_gemini(prompt: str) -> str:
    """Call Google Gemini API with fallback handling."""
    api_key = GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Please set it in your environment: "
            "set GEMINI_API_KEY=your_key_here"
        )

    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    response = model.generate_content(prompt)
    return response.text


def analyze_vehicles(
    sale_id: Optional[int] = None,
    max_price: Optional[float] = None,
    batch_size: int = 40,
) -> dict[str, Any]:
    """
    Run AI analysis on all crawled vehicles.
    Returns structured recommendations and rankings.
    """
    console.rule("[bold magenta]AI Vehicle Advisor[/bold magenta]")

    vehicles = get_all_vehicles_with_documents(sale_id=sale_id, max_price=max_price)
    if not vehicles:
        console.print("[yellow]No vehicles found in database matching criteria.[/yellow]")
        return {}

    console.print(f"Loaded {len(vehicles)} vehicles from database.")

    # Format vehicles
    formatted = [format_vehicle_for_prompt(v) for v in vehicles]

    # Pre-filter disqualifications locally:
    # 1. Any vehicle with CRITICAL defects
    # 2. Huge mileage (> 250,000 km) unless very cheap
    candidates = []
    disqualified = []
    for f in formatted:
        if f.get("ct_critical_defects"):
            disqualified.append({
                "lot": f.get("lot_number"),
                "car": f"{f.get('brand')} {f.get('model')}",
                "reason": f"Critical CT defects: {f['ct_critical_defects']}",
            })
        else:
            candidates.append(f)

    console.print(f"  {len(candidates)} viable candidates, {len(disqualified)} pre-disqualified.")

    # Build prompt
    prompt = f"""
You are an expert French used car market analyst and mechanic. You evaluate auction vehicles
from Alcopa Auction to find the best buys for a private individual or professional buyer.

Evaluate the following list of {len(candidates)} vehicles and produce a top-{TOP_RECOMMENDATIONS_COUNT} ranked recommendation list.

For each vehicle consider:
1. **RELIABILITY (0-100)**:
   - Brand and model track record (Toyota, Honda, Mazda, certain VW/Renault/Peugeot engines are reliable; beware of known problematic engines like PureTech 1.2 wet timing belt issues, 1.5 BlueHDi camshaft chain issues, early Renault 1.2 TCe, etc.)
   - Mileage relative to age (normal in France is ~15,000 km/year). Excessive mileage = higher wear risk.
2. **CONDITION (0-100)**:
   - Technical inspection (Contrôle technique):
     * FAVORABLE with 0 minor defects = excellent
     * FAVORABLE with minor defects = good, normal wear
     * DEFAVORABLE with major defects = needs repairs before registration, factor cost in!
     * NO_CT_FILE = penalize slightly for uncertainty
   - Battery health for EVs/hybrids (>90% is great, <80% is risky)
   - Warranty included is a big plus!
3. **VALUE FOR MONEY (0-100)**:
   - Compare starting price/current bid to French used market value (La Centrale / Leboncoin typical prices)
   - Is it priced reasonably for its age and mileage?
4. **OVERALL SCORE (0-100)**: Weighted average: Reliability (35%), Condition (35%), Value (30%).

Here is the candidate vehicle dataset (JSON):
{json.dumps(candidates[:80], indent=2, ensure_ascii=False)}

Respond ONLY with valid JSON in this exact structure:
{{
  "market_overview": "Brief summary of the auction offering",
  "top_recommendations": [
    {{
      "rank": 1,
      "lot_number": 501,
      "brand": "KIA",
      "model": "SPORTAGE",
      "trim": "...",
      "year": 2022,
      "mileage_km": 200166,
      "price": 10200,
      "overall_score": 85,
      "reliability_score": 80,
      "condition_score": 88,
      "value_score": 87,
      "verdict": "Clear 2-sentence summary of why this is a good buy",
      "pros": ["Pro 1", "Pro 2", "Pro 3"],
      "cons": ["Con 1", "Con 2"],
      "recommended_max_bid": 12500,
      "watch_outs": "Specific things to inspect or known model quirks"
    }}
  ],
  "engine_warnings": [
    {{
      "lot_number": 504,
      "warning": "e.g. PureTech 1.2 engine has known wet belt degradation issues"
    }}
  ],
  "disqualified_summary": "{len(disqualified)} vehicles had critical defects or major disqualifiers"
}}
"""

    console.print("[dim]Calling Gemini AI for analysis...[/dim]")
    try:
        raw_response = call_gemini(prompt)
        # Clean markdown code block wraps if present
        clean_json = raw_response.strip()
        if clean_json.startswith("```"):
            clean_json = clean_json.split("\n", 1)[1]
        if clean_json.endswith("```"):
            clean_json = clean_json.rsplit("\n", 1)[0]
        if clean_json.startswith("json"):
            clean_json = clean_json[4:].strip()

        analysis = json.loads(clean_json)
        analysis["disqualified"] = disqualified
        console.print("[green]AI analysis completed successfully![/green]")
        return analysis
    except Exception as e:
        console.print(f"[red]AI analysis failed: {e}[/red]")
        # Return fallback heuristic ranking
        fallback = generate_heuristic_ranking(candidates)
        fallback["disqualified"] = disqualified
        return fallback


def generate_heuristic_ranking(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Fallback ranking based on rule-based heuristics if AI fails."""
    console.print("[yellow]Using heuristic ranking fallback...[/yellow]")
    scored = []
    for c in candidates:
        year = c.get("year") or 2018
        km = c.get("mileage_km") or 150000
        price = c.get("starting_price") or c.get("current_bid") or 10000

        # Heuristic scoring
        # Newer is better
        age = max(1, 2026 - year)
        # Annual km
        km_per_year = km / age
        km_score = max(0, 100 - (km_per_year / 300))

        # Price per year of life left
        value_score = max(0, 100 - (price / 250))

        # CT condition
        ct_score = 90 if c.get("ct_result") == "FAVORABLE" else 60
        if c.get("warranty"):
            ct_score += 10

        overall = int(0.4 * km_score + 0.3 * value_score + 0.3 * ct_score)
        scored.append({
            "lot_number": c.get("lot_number"),
            "brand": c.get("brand"),
            "model": c.get("model"),
            "trim": c.get("trim"),
            "year": year,
            "mileage_km": km,
            "price": price,
            "overall_score": overall,
            "reliability_score": int(km_score),
            "condition_score": int(ct_score),
            "value_score": int(value_score),
            "verdict": f"Scored based on age ({year}), mileage ({km:,} km), and starting price (€{price:,}).",
            "pros": ["Clean CT" if c.get("ct_result") == "FAVORABLE" else "Budget price"],
            "cons": ["High mileage" if km > 150000 else "Standard wear"],
            "recommended_max_bid": int(price * 1.15),
            "watch_outs": "Verify maintenance history before bidding.",
        })

    scored.sort(key=lambda x: x["overall_score"], reverse=True)
    for i, s in enumerate(scored[:TOP_RECOMMENDATIONS_COUNT]):
        s["rank"] = i + 1

    return {
        "market_overview": "Heuristic ranking based on vehicle age, mileage, and price.",
        "top_recommendations": scored[:TOP_RECOMMENDATIONS_COUNT],
        "engine_warnings": [],
        "disqualified_summary": "Heuristic fallback mode used.",
    }


def generate_html_report(analysis: dict[str, Any], output_path: Optional[Path] = None) -> Path:
    """Generate a clean, responsive HTML report from the analysis."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = REPORTS_DIR / f"recommendation_{timestamp}.html"

    top = analysis.get("top_recommendations", [])
    warnings = analysis.get("engine_warnings", [])
    overview = analysis.get("market_overview", "")

    disqualified = analysis.get("disqualified", [])
    disqualified_html = ""
    if disqualified:
        disq_items = "".join(
            f"<li><strong>Lot {d.get('lot')}:</strong> {d.get('car')} — <span class='disq-reason'>{d.get('reason')}</span></li>"
            for d in disqualified
        )
        disqualified_html = f"""
        <div class="disqualified-box">
            <h3>🚫 Véhicules Écartés / Défauts Critiques (Carton Rouge)</h3>
            <p>Ces véhicules ont été automatiquement exclus des recommandations principales en raison d'anomalies critiques ou majeures :</p>
            <ul>{disq_items}</ul>
        </div>
        """

    cards_html = ""
    for r in top:
        pros_li = "".join(f"<li>{p}</li>" for p in r.get("pros", []))
        cons_li = "".join(f"<li>{c}</li>" for c in r.get("cons", []))

        score = r.get("overall_score", 0)
        if score >= 80:
            score_color = "#10b981"
            tier_badge = '<span class="tier-pill tier-green">🟢 SUPER AFFAIRE</span>'
        elif score >= 60:
            score_color = "#f59e0b"
            tier_badge = '<span class="tier-pill tier-yellow">🟡 AFFAIRE CORRECTE</span>'
        else:
            score_color = "#ef4444"
            tier_badge = '<span class="tier-pill tier-red">🔴 RISQUÉ / À ÉVITER</span>'

        cards_html += f"""
        <div class="car-card">
            <div class="card-header">
                <div class="rank-badge">#{r.get('rank')}</div>
                <div class="car-title">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <h3>{r.get('brand')} {r.get('model')}</h3>
                        {tier_badge}
                    </div>
                    <p class="trim">{r.get('trim', '')}</p>
                </div>
                <div class="score-badge" style="background: {score_color}">
                    <span class="score-num">{r.get('overall_score')}</span>
                    <span class="score-label">/ 100</span>
                </div>
            </div>

            <div class="specs-grid">
                <div class="spec"><span class="label">Lot</span><span class="val">#{r.get('lot_number')}</span></div>
                <div class="spec"><span class="label">Année</span><span class="val">{r.get('year')}</span></div>
                <div class="spec"><span class="label">Kilométrage</span><span class="val">{r.get('mileage_km', 0):,} km</span></div>
                <div class="spec"><span class="label">Prix de départ</span><span class="val price">{r.get('price', 0):,} €</span></div>
                <div class="spec"><span class="label">Max suggéré</span><span class="val max-bid">{r.get('recommended_max_bid', 0):,} €</span></div>
            </div>

            <div class="scores-row">
                <div class="sub-score"><span>Fiabilité</span><b>{r.get('reliability_score')}/100</b></div>
                <div class="sub-score"><span>État (CT)</span><b>{r.get('condition_score')}/100</b></div>
                <div class="sub-score"><span>Rapport Qualité/Prix</span><b>{r.get('value_score')}/100</b></div>
            </div>

            <div class="verdict">
                <strong>Avis expert:</strong> {r.get('verdict')}
            </div>

            <div class="pros-cons">
                <div class="pros">
                    <h4>Points forts</h4>
                    <ul>{pros_li}</ul>
                </div>
                <div class="cons">
                    <h4>Points faibles</h4>
                    <ul>{cons_li}</ul>
                </div>
            </div>

            {f'<div class="watch-out">⚠️ <strong>À surveiller:</strong> {r.get("watch_outs")}</div>' if r.get("watch_outs") else ''}
        </div>
        """

    warnings_html = ""
    if warnings:
        warnings_li = "".join(f"<li><strong>Lot {w.get('lot_number')}:</strong> {w.get('warning')}</li>" for w in warnings)
        warnings_html = f"""
        <div class="warning-box">
            <h3>⚠️ Moteurs et modèles à risque identifiés</h3>
            <ul>{warnings_li}</ul>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="utf-8">
    <title>Alcopa Auction — Rapport Recommandations Véhicules</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f8fafc;
            color: #1e293b;
            padding: 24px;
            line-height: 1.5;
        }}
        .container {{ max-width: 1100px; margin: 0 auto; }}
        header {{
            background: linear-gradient(135deg, #0075ca, #00508c);
            color: white;
            padding: 32px;
            border-radius: 12px;
            margin-bottom: 24px;
        }}
        header h1 {{ font-size: 28px; margin-bottom: 8px; }}
        header p {{ opacity: 0.9; }}
        .overview {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 24px;
            border-left: 4px solid #0075ca;
        }}
        .car-card {{
            background: white;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border: 1px solid #e2e8f0;
        }}
        .card-header {{
            display: flex;
            align-items: center;
            gap: 16px;
            margin-bottom: 16px;
        }}
        .rank-badge {{
            background: #0075ca;
            color: white;
            font-size: 20px;
            font-weight: bold;
            width: 48px;
            height: 48px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .car-title {{ flex: 1; }}
        .car-title h3 {{ font-size: 20px; color: #0f172a; }}
        .car-title .trim {{ font-size: 13px; color: #64748b; }}
        .score-badge {{
            color: white;
            padding: 8px 16px;
            border-radius: 8px;
            text-align: center;
        }}
        .score-num {{ font-size: 24px; font-weight: bold; display: block; line-height: 1; }}
        .score-label {{ font-size: 11px; opacity: 0.9; }}
        .specs-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 12px;
            background: #f1f5f9;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 16px;
        }}
        .spec .label {{ display: block; font-size: 11px; color: #64748b; text-transform: uppercase; }}
        .spec .val {{ font-size: 15px; font-weight: 600; }}
        .spec .price {{ color: #0075ca; }}
        .spec .max-bid {{ color: #10b981; }}
        .scores-row {{
            display: flex;
            gap: 16px;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 1px solid #e2e8f0;
        }}
        .sub-score {{ flex: 1; font-size: 13px; }}
        .sub-score span {{ color: #64748b; display: block; }}
        .sub-score b {{ font-size: 16px; color: #0f172a; }}
        .verdict {{
            background: #f8fafc;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 16px;
            font-size: 14px;
        }}
        .pros-cons {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-bottom: 12px;
        }}
        .pros, .cons {{ font-size: 13px; }}
        .pros h4 {{ color: #10b981; margin-bottom: 6px; }}
        .cons h4 {{ color: #ef4444; margin-bottom: 6px; }}
        .pros ul, .cons ul {{ padding-left: 18px; }}
        .watch-out {{
            background: #fffbeb;
            border: 1px solid #fde68a;
            color: #92400e;
            padding: 10px;
            border-radius: 6px;
            font-size: 13px;
        }}
        .warning-box {{
            background: #fef2f2;
            border: 1px solid #fecaca;
            color: #991b1b;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 24px;
        }}
        .warning-box h3 {{ margin-bottom: 12px; }}
        .warning-box ul {{ padding-left: 20px; }}
        .disqualified-box {{
            background: #fff1f2;
            border: 2px dashed #f43f5e;
            color: #881337;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 24px;
        }}
        .disqualified-box h3 {{ margin-bottom: 8px; color: #be123c; }}
        .disqualified-box p {{ font-size: 14px; margin-bottom: 12px; }}
        .disqualified-box ul {{ padding-left: 20px; font-size: 13px; }}
        .tier-pill {{
            font-size: 11px;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            display: inline-block;
        }}
        .tier-green {{
            background: #d1fae5;
            color: #065f46;
            border: 1px solid #a7f3d0;
        }}
        .tier-yellow {{
            background: #fef3c7;
            color: #92400e;
            border: 1px solid #fde68a;
        }}
        .tier-red {{
            background: #fee2e2;
            color: #991b1b;
            border: 1px solid #fecaca;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Rapport d'Analyse IA — Vente aux Enchères Alcopa</h1>
            <p>Généré automatiquement • Évaluation basée sur la fiabilité, le contrôle technique, et le prix</p>
        </header>

        <div class="overview">
            <h3>Vue d'ensemble du marché</h3>
            <p>{overview}</p>
        </div>

        {warnings_html}
        {disqualified_html}

        <h2>Top Recommandations d'Achat</h2>
        {cards_html}
    </div>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    console.print(f"[bold green]HTML Report saved to: {output_path}[/bold green]")
    return output_path
