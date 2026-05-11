"""
scanner.py
==========
Market opportunity scanner for Bull Call Spread analysis system.

This script:
    1. Runs criteria + scoring for a list of tickers
    2. Saves all results to PostgreSQL (db.py)
    3. Displays a detailed console report per ticker
    4. Sends ALL results in a single AI call for comparative interpretation

Run manually when looking for entry opportunities.

Dependencies:
    criteria.py  → raw market data
    scoring.py   → scoring and verdict
    db.py        → save to PostgreSQL
    .env         → DATABASE_URL, ANTHROPIC_API_KEY

Usage:
    python scanner.py
    python scanner.py --tickers AAPL MSFT SPY TSLA NVDA
"""

import os
import sys
import json
import argparse
from datetime import datetime

import anthropic
from dotenv import load_dotenv

from criteria import get_all_criteria
from scoring import score_criteria
from db import save_analysis, get_open_positions

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_TICKERS = [
    # Tecnología
    "GOOGL", "META", "AMZN", "CRM", "NFLX",
    # Finanzas
    "JPM", "GS", "V", "MA", "BAC",
    # Consumo
    "HD", "WMT", "COST", "NKE", "MCD",
]

# AI model to use for interpretation
AI_MODEL = "claude-opus-4-5"
AI_MAX_TOKENS = 2000

# Your trading context — update as your situation changes
TRADING_CONTEXT = {
    "strategy":       "Bull Call Spread",
    "capital":        15000,
    "max_risk_pct":   2,
    "broker":         "Thinkorswim (paperMoney)",
    "is_paper":       True,
    "language":       "Spanish",  
}


# ══════════════════════════════════════════════════════════════════════════════
# CONSOLE OUTPUT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def verdict_icon(verdict):
    return {"VIABLE": "✅", "CAUTION": "⚠️ ", "DO_NOT_TRADE": "❌"}.get(verdict, "—")

def score_icon(score, max_score=0):
    if max_score == 0:
        return "—"
    pct = score / max_score
    if pct >= 0.68:
        return "✅"
    elif pct >= 0.35:
        return "⚠️ "
    return "❌"

def criterion_icon(score):
    if score > 0:
        return "✅"
    elif score == 0:
        return "—"
    return "❌"

def print_ticker_detail(scored):
    """Print detailed analysis for one ticker."""
    verdict = scored["verdict"]
    icon = verdict_icon(verdict)
    score = scored["score"]
    score_max = scored["score_max"]
    score_pct = scored["score_pct"]

    print(f"\n── {scored['ticker']} {'─' * (50 - len(scored['ticker']))}")
    print(f"Price: ${scored['price']:.2f}  |  "
          f"Score: {score}/{score_max} ({score_pct}%)  |  "
          f"{icon} {verdict}")
    print()

    # Group criteria by category
    categories = {
        "TECHNICAL": [
            "trend_25d", "moving_averages", "sma50_direction",
            "rsi", "week_52", "support_resistance", "candlestick"
        ],
        "VOLATILITY": [
            "hv", "iv_vs_hv", "iv_percentile",
            "beta", "put_call_ratio", "open_interest"
        ],
        "OPERATIONAL": ["earnings", "volume"],
        "FUNDAMENTAL": ["pe", "eps_growth", "debt_equity", "profit_margin"],
    }

    criteria = scored["criteria_scores"]

    for category, keys in categories.items():
        print(f"  {category}")
        for key in keys:
            if key in criteria:
                c = criteria[key]
                icon = criterion_icon(c["score"])
                label = c["label"]
                pts = f"{c['score']:+d}"
                print(f"    {icon} {key:<22} {label:<35} ({pts})")
        print()


def print_summary_table(all_scored):
    """Print a summary table of all tickers sorted by score."""
    sorted_results = sorted(all_scored, key=lambda x: x["score"], reverse=True)

    print("\n" + "═" * 70)
    print(f"{'TICKER':<8} {'PRICE':>8} {'SCORE':>10} {'PCT':>7}  VERDICT")
    print("═" * 70)

    for s in sorted_results:
        icon = verdict_icon(s["verdict"])
        print(f"{s['ticker']:<8} "
              f"${s['price']:>7.2f} "
              f"{s['score']:>4}/{s['score_max']:<4} "
              f"{s['score_pct']:>6.1f}%  "
              f"{icon} {s['verdict']}")

    print("═" * 70)


# ══════════════════════════════════════════════════════════════════════════════
# AI INTERPRETATION
# ══════════════════════════════════════════════════════════════════════════════

def build_ai_prompt(all_scored, open_positions, context):
    """
    Build a single prompt with all ticker results for comparative AI analysis.
    """
    # Format open positions
    positions_str = "None currently open."
    if open_positions:
        lines = []
        for p in open_positions:
            lines.append(
                f"  - {p['ticker']} | {p['strategy']} | "
                f"Strike {p['strike_low']}/{p['strike_high']} | "
                f"Exp {p['expiration']} | "
                f"Cost ${p['total_cost']} | "
                f"Status: {p['status']}"
            )
        positions_str = "\n".join(lines)

    # Format scored results
    results_str = ""
    for s in sorted(all_scored, key=lambda x: x["score"], reverse=True):
        results_str += f"\n{'─' * 40}\n"
        results_str += f"TICKER: {s['ticker']} | Price: ${s['price']:.2f} | "
        results_str += f"Score: {s['score']}/{s['score_max']} ({s['score_pct']}%) | "
        results_str += f"Verdict: {s['verdict']}\n"

        for criterion, data in s["criteria_scores"].items():
            results_str += f"  {criterion}: {data['label']} ({data['score']:+d})\n"

    prompt = f"""You are an expert options trading mentor specialized in Bull Call Spreads.
        Your student has been learning for several weeks and understands all the criteria below.
        Be direct, specific, and educational. Avoid generic advice.

        TRADING CONTEXT:
        Strategy:     {context['strategy']}
        Capital:      ${context['capital']:,}
        Max risk/trade: {context['max_risk_pct']}% (${context['capital'] * context['max_risk_pct'] / 100:.0f} max per trade)
        Broker:       {context['broker']}
        Paper trading: {context['is_paper']}

        OPEN POSITIONS:
        {positions_str}

        TODAY'S SCAN RESULTS:
        {results_str}

        Please provide:

        1. RANKING — Order these tickers from best to worst entry opportunity today. 
        Explain briefly why each is ranked where it is.

        2. TOP PICK — If you had to choose ONE ticker to open a Bull Call Spread today, 
        which would it be and why? Include suggested strike range based on current price.

        3. ALERTS — What are the most important warning signals across all tickers today?
        Focus on signals that could cause losses if ignored.

        4. OPEN POSITIONS ASSESSMENT — How are the current open positions looking 
        given today's scan? Should any be closed or monitored closely?

        5. MARKET CONTEXT — What does the overall picture of these tickers tell you 
        about current market conditions? Is this a good environment for Bull Call Spreads?

        Keep your response focused and actionable. Max 400 words."""
    
    prompt += f"\n\nIMPORTANT: Respond entirely in {context.get('language', 'English')}."


    return prompt


def get_ai_interpretation(all_scored, open_positions, context):
    """
    Send all results to Claude in a single API call for comparative analysis.

    Returns:
        tuple: (client, conversation_history, ai_text)
               client y conversation_history se retornan para el loop de conversación
    """
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        prompt = build_ai_prompt(all_scored, open_positions, context)

        conversation_history = [{"role": "user", "content": prompt}]

        print("\n⏳ Getting AI interpretation...")

        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=AI_MAX_TOKENS,
            messages=conversation_history
        )

        ai_text = response.content[0].text
        conversation_history.append({"role": "assistant", "content": ai_text})

        return client, conversation_history, ai_text

    except Exception as e:
        return None, [], f"⚠️  AI interpretation unavailable: {e}"

# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCANNER
# ══════════════════════════════════════════════════════════════════════════════

def run_scan(tickers):
    """
    Run the full scan pipeline for a list of tickers.

    Pipeline:
        criteria → scoring → db → console → AI interpretation
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'═' * 70}")
    print(f"  BULL CALL SPREAD SCANNER — {timestamp}")
    print(f"  Tickers: {', '.join(tickers)}")
    print(f"{'═' * 70}")

    all_scored = []
    failed = []

    # ── Process each ticker ───────────────────────────────────────────────────
    for ticker in tickers:
        print(f"\n⏳ Analyzing {ticker}...", end=" ", flush=True)

        try:
            # Step 1 — Fetch raw criteria
            criteria = get_all_criteria(ticker)
            if criteria is None:
                print(f"❌ Insufficient data")
                failed.append(ticker)
                continue

            # Step 2 — Score criteria
            scored = score_criteria(criteria)
            if scored is None:
                print(f"❌ Scoring failed")
                failed.append(ticker)
                continue

            # Step 3 — Save to database
            analysis_id = save_analysis(scored)
            scored["analysis_id"] = analysis_id

            # Step 4 — Print detailed report
            print(f"✅ Done (Score: {scored['score']}/{scored['score_max']})")
            print_ticker_detail(scored)

            all_scored.append(scored)

        except Exception as e:
            print(f"❌ Error: {e}")
            failed.append(ticker)
            continue

    if not all_scored:
        print("\n❌ No tickers could be analyzed. Check your connection.")
        return

    # ── Summary table ─────────────────────────────────────────────────────────
    print_summary_table(all_scored)

    # ── Failed tickers ────────────────────────────────────────────────────────
    if failed:
        print(f"\n⚠️  Failed tickers: {', '.join(failed)}")

    # ── AI Interpretation ─────────────────────────────────────────────────────
    open_positions = get_open_positions()

    client, conversation_history, ai_text = get_ai_interpretation(
        all_scored, open_positions, TRADING_CONTEXT
    )

    print(f"\n{'═' * 70}")
    print("  AI INTERPRETATION")
    print(f"{'═' * 70}")
    print(ai_text)
    print(f"{'═' * 70}\n")

    # ── Loop de conversación ──────────────────────────────────────────────────
    if client:
        print("💬 Puedes preguntarme sobre el análisis. Escribe 'exit' para terminar.\n")

        while True:
            user_input = input("Tu pregunta: ").strip()

            if not user_input:
                continue

            if user_input.lower() == "exit":
                break

            conversation_history.append({"role": "user", "content": user_input})

            follow_up = client.messages.create(
                model=AI_MODEL,
                max_tokens=AI_MAX_TOKENS,
                messages=conversation_history
            )

            ai_reply = follow_up.content[0].text
            conversation_history.append({"role": "assistant", "content": ai_reply})

            print(f"\n🤖 {ai_reply}\n")

    # ── Stats — solo se muestran al salir ────────────────────────────────────
    viable   = sum(1 for s in all_scored if s["verdict"] == "VIABLE")
    caution  = sum(1 for s in all_scored if s["verdict"] == "CAUTION")
    no_trade = sum(1 for s in all_scored if s["verdict"] == "DO_NOT_TRADE")

    print(f"\nScan complete: {len(all_scored)} analyzed | "
          f"✅ {viable} VIABLE | "
          f"⚠️  {caution} CAUTION | "
          f"❌ {no_trade} DO NOT TRADE")
    print(f"Results saved to database.\n")

    """ ai_text = get_ai_interpretation(all_scored, open_positions, TRADING_CONTEXT)

    print(f"\n{'═' * 70}")
    print("  AI INTERPRETATION")
    print(f"{'═' * 70}")
    print(ai_text)
    print(f"{'═' * 70}\n")

    # ── Stats ─────────────────────────────────────────────────────────────────
    viable = sum(1 for s in all_scored if s["verdict"] == "VIABLE")
    caution = sum(1 for s in all_scored if s["verdict"] == "CAUTION")
    no_trade = sum(1 for s in all_scored if s["verdict"] == "DO_NOT_TRADE")

    print(f"Scan complete: {len(all_scored)} analyzed | "
          f"✅ {viable} VIABLE | "
          f"⚠️  {caution} CAUTION | "
          f"❌ {no_trade} DO NOT TRADE")
    print(f"Results saved to database.\n") """


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bull Call Spread opportunity scanner"
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=DEFAULT_TICKERS,
        help="List of tickers to scan (default: AAPL SPY TSLA NVDA MSFT)"
    )
    args = parser.parse_args()

    run_scan(args.ticker if hasattr(args, 'ticker') else args.tickers)