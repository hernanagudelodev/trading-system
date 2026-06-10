"""
auto_run.py
===========
Automated daily paper trading runner.

Runs twice daily (configured in Railway cron):
    - Morning: 10:00am ET (30min after market open)
    - Afternoon: 2:30pm ET (90min before close)

Workflow:
    1. Run market_context.py → fresh macro context
    2. Run scanner.py --universe → full opportunity scan
    3. Run paper_sync → update P&L, auto-close stop loss/target
    4. Call Anthropic API with full context + web search
       → Claude analyzes and returns recommendations as JSON
    5. Execute recommended paper trades
    6. Send ntfy push summary
    7. Save run log to reports/

Dependencies:
    All scripts in scripts/
    ANTHROPIC_API_KEY, TASTYTRADE_*, NTFY_TOPIC in .env
"""

import os
import sys
import json
import time
import asyncio
import requests
import traceback
from datetime import datetime, date

from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

# Add scripts/ to path
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR    = os.path.dirname(_SCRIPTS_DIR)
_REPORTS_DIR = os.path.join(_BASE_DIR, "reports")

sys.path.insert(0, _SCRIPTS_DIR)

NTFY_TOPIC    = os.getenv("NTFY_TOPIC", "")
NTFY_BASE_URL = "https://ntfy.sh"
AI_MODEL = "claude-sonnet-4-6"

# Max paper trades to open per run
# Claude decides based on signal quality — this is a hard safety cap
MAX_NEW_TRADES_PER_RUN = 5


# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def send_ntfy(title, message, priority="default"):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"{NTFY_BASE_URL}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title":    title.encode("utf-8"),
                "Priority": priority,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"  ntfy error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Market Context
# ══════════════════════════════════════════════════════════════════════════════

def run_market_context():
    print("\n  [1/6] Running market_context.py...")
    try:
        import market_context
        data = market_context.run()
        print(f"  Verdict: {data['verdict']} | VIX: {data['vix']['current']}")
        return data
    except Exception as e:
        print(f"  ERROR: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Scanner
# ══════════════════════════════════════════════════════════════════════════════

def run_scanner():
    print("\n  [2/6] Running scanner --universe...")
    try:
        from universe import get_scanner_candidates
        from scanner import run_scan, TIER1_STARS

        tickers = get_scanner_candidates()
        if not tickers:
            print("  Universe failed — falling back to Tier 1 stars")
            tickers = TIER1_STARS

        run_scan(tickers, expand_to_universe=False)

        # Read the AI-friendly summary (compact, designed for API)
        ai_path = os.path.join(_REPORTS_DIR, "scanner_ai_summary.md")
        if os.path.exists(ai_path):
            with open(ai_path, encoding="utf-8") as f:
                content = f.read()
            print(f"  AI summary ready ({len(content)} chars)")
            return content

        # Fallback to full report
        report_path = os.path.join(_REPORTS_DIR, "scanner_report.md")
        if os.path.exists(report_path):
            with open(report_path, encoding="utf-8") as f:
                content = f.read()
            print(f"  Full report ({len(content)} chars) — AI summary not found")
            return content

        return None
    except Exception as e:
        print(f"  ERROR: {e}")
        traceback.print_exc()
        return None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Paper Sync
# ══════════════════════════════════════════════════════════════════════════════

def run_paper_sync():
    print("\n  [3/6] Running paper_sync...")
    try:
        import trade as trade_module
        trade_module.cmd_paper_sync()
        return True
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Get current state from DB
# ══════════════════════════════════════════════════════════════════════════════

def get_current_state():
    """Get open paper positions and recent closed positions from DB."""
    try:
        import psycopg2
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur  = conn.cursor()

        # Open paper positions
        cur.execute("""
            SELECT ticker, strategy, strike_low, strike_high, expiration,
                   premium_paid, total_cost, gross_pnl, pnl_pct,
                   profit_pct_of_max, opened_at
            FROM paper_positions
            WHERE UPPER(status) = 'OPEN'
            ORDER BY opened_at DESC
        """)
        cols  = [d[0] for d in cur.description]
        open_positions = [dict(zip(cols, row)) for row in cur.fetchall()]

        # Recently closed (last 5)
        cur.execute("""
            SELECT ticker, strategy, strike_low, strike_high,
                   gross_pnl, pnl_pct, close_reason, closed_at
            FROM paper_positions
            WHERE UPPER(status) = 'CLOSED'
            ORDER BY closed_at DESC
            LIMIT 5
        """)
        cols   = [d[0] for d in cur.description]
        closed = [dict(zip(cols, row)) for row in cur.fetchall()]

        cur.close()
        conn.close()

        return {"open": open_positions, "recently_closed": closed}
    except Exception as e:
        print(f"  DB error: {e}")
        return {"open": [], "recently_closed": []}


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Claude Analysis via API
# ══════════════════════════════════════════════════════════════════════════════

def run_claude_analysis(market_ctx, scanner_report, db_state):
    print("\n  [4/6] Calling Anthropic API...")

    # Try to read compact AI summary of market context
    mc_ai_path = os.path.join(_REPORTS_DIR, "market_context_ai.md")
    if os.path.exists(mc_ai_path):
        with open(mc_ai_path, encoding="utf-8") as f:
            market_context_text = f.read()
    else:
        # Build from dict as fallback
        vix     = market_ctx.get("vix", {}) if market_ctx else {}
        spy     = market_ctx.get("spy", {}) if market_ctx else {}
        verdict = market_ctx.get("verdict", "N/A") if market_ctx else "N/A"
        detail  = market_ctx.get("verdict_detail", "") if market_ctx else ""
        market_context_text = (
            f"VERDICT: {verdict} — {detail}\n"
            f"VIX: {vix.get('current', 'N/A')} {vix.get('level', '')} "
            f"{vix.get('trend', '')}\n"
            f"SPY: ${spy.get('price', 'N/A')} {spy.get('trend', '')} "
            f"{spy.get('pct_25d', 0):+.1f}% 25d"
        )
    if db_state["open"]:
        lines = []
        for p in db_state["open"]:
            pnl     = float(p["gross_pnl"] or 0) if p["gross_pnl"] else 0
            pnl_pct = float(p["pnl_pct"] or 0)   if p["pnl_pct"]   else 0
            pmax    = float(p["profit_pct_of_max"] or 0) * 100 if p["profit_pct_of_max"] else 0
            exp     = str(p["expiration"])
            dte     = (date.fromisoformat(exp) - date.today()).days if exp else "?"
            lines.append(
                f"- {p['ticker']} {p['strategy']} ${p['strike_low']}/{p['strike_high']} "
                f"exp {exp} ({dte}d) | P&L ${pnl:+.0f} ({pnl_pct:+.1f}%) | "
                f"{pmax:.0f}% del máximo"
            )
        open_pos_summary = "\n".join(lines)
    else:
        open_pos_summary = "Ninguna posición paper abierta."

    closed_summary = ""
    if db_state["recently_closed"]:
        lines = []
        for p in db_state["recently_closed"]:
            pnl    = float(p["gross_pnl"] or 0)
            reason = p["close_reason"] or "MANUAL"
            lines.append(
                f"- {p['ticker']} {p['strategy']}: ${pnl:+.0f} ({reason})"
            )
        closed_summary = "\n".join(lines)
    else:
        closed_summary = "Sin cierres recientes."

    macro_events_str = ""
    if market_ctx and market_ctx.get("macro_events"):
        for e in market_ctx["macro_events"]:
            macro_events_str += f"- {e['event']} en {e['days_away']}d ({e['impact']}): {e['description']}\n"

    earnings_str = ""
    if market_ctx and market_ctx.get("upcoming_earnings"):
        for e in market_ctx["upcoming_earnings"]:
            earnings_str += f"- {e['ticker']} ({e['sector']}) reporta en {e['days_away']}d\n"
    if not earnings_str:
        earnings_str = "Sin earnings de riesgo en los próximos 10 días."

    prompt = f"""Eres un sistema automatizado de paper trading de opciones.
Tu trabajo es analizar el scanner report, el contexto macro y las posiciones actuales,
y tomar decisiones de trading con criterio conservador.

IMPORTANTE: Debes responder ÚNICAMENTE con un objeto JSON válido.
No escribas ningún texto antes ni después del JSON.
No uses markdown ni bloques de código.
Empieza tu respuesta directamente con el carácter {{

=== CONTEXTO MACRO ===
{market_context_text}

=== POSICIONES PAPER ABIERTAS ===
{open_pos_summary}

=== CIERRES RECIENTES ===
{closed_summary}

=== SCANNER REPORT ===
{scanner_report or 'No disponible.'}

=== INSTRUCCIONES ===
1. Usa web search para buscar noticias recientes de los candidatos que pasaron filtros.
2. Considera el contexto macro y eventos de la semana.
3. Evalúa cada candidato con criterio conservador.
4. Decide cuáles abrir como paper trade y cuáles ignorar.
5. Decide si alguna posición abierta debe cerrarse anticipadamente por cambio de tesis.

Reglas:
- debit positivo = Bull Call Spread (pagas), negativo = Bull Put Spread (cobras crédito)
- Solo recomendar trades con señal clara y contexto favorable
- Si hay evento macro HIGH/VERY_HIGH en menos de 2 días, ser muy conservador
- No abrir trades en sectores con earnings de riesgo en menos de 7 días
- Máximo {MAX_NEW_TRADES_PER_RUN} trades nuevos por run
- Si no hay nada convincente, devolver new_trades vacío y explicar en no_trade_reason

Responde SOLO con este JSON (sin texto adicional, sin markdown):
{{
  "analysis_summary": "Párrafo breve del contexto del día y decisiones tomadas",
  "new_trades": [
    {{
      "ticker": "CVS",
      "strategy": "Bull Call Spread",
      "strike_low": 94.0,
      "strike_high": 101.0,
      "expiration": "2026-07-10",
      "debit": 2.62,
      "rationale": "Párrafo explicando por qué este trade"
    }}
  ],
  "close_positions": [
    {{
      "ticker": "CRM",
      "reason": "Razón para cerrar anticipadamente"
    }}
  ],
  "no_trade_reason": "Si no hay trades nuevos, explicar por qué"
}}"""

    try:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      AI_MODEL,
                "max_tokens": 4000,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=120,
        )

        if response.status_code != 200:
            print(f"  API error: {response.status_code} — {response.text[:200]}")
            return None

        data = response.json()

        # Extract text from response (may include tool_use blocks from web search)
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        text = text.strip()

        # Try to extract JSON — Claude may include text before/after
        # Strategy 1: direct parse
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            # Strategy 2: find JSON object in text
            import re
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except json.JSONDecodeError:
                    # Strategy 3: strip markdown code blocks
                    clean = re.sub(r'```(?:json)?', '', text).strip()
                    try:
                        result = json.loads(clean)
                    except json.JSONDecodeError as e:
                        print(f"  JSON parse error: {e}")
                        print(f"  Raw response: {text[:500]}")
                        return None
            else:
                print(f"  No JSON found in response")
                print(f"  Raw response: {text[:500]}")
                return None
        print(f"  Analysis complete. New trades: {len(result.get('new_trades', []))}")
        return result

    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        print(f"  Raw response: {text[:500]}")
        return None
    except Exception as e:
        print(f"  API call error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 — Execute recommendations
# ══════════════════════════════════════════════════════════════════════════════

def execute_recommendations(analysis):
    """Execute Claude's recommendations — open new trades, close positions."""
    if not analysis:
        return {"opened": [], "closed": [], "errors": []}

    results = {"opened": [], "closed": [], "errors": []}

    import trade as trade_module

    # Close positions Claude recommends closing
    for close_rec in analysis.get("close_positions", []):
        ticker = close_rec.get("ticker", "").upper()
        reason = close_rec.get("reason", "AUTO_CLOSE_AI")
        print(f"\n  Closing {ticker}: {reason}")
        try:
            trade_module.cmd_paper_close(ticker)
            results["closed"].append({
                "ticker": ticker,
                "reason": reason
            })
        except Exception as e:
            print(f"  Error closing {ticker}: {e}")
            results["errors"].append(f"Close {ticker}: {e}")

    # Open new paper trades
    new_trades = analysis.get("new_trades", [])
    opened_count = 0

    for trade_rec in new_trades:
        if opened_count >= MAX_NEW_TRADES_PER_RUN:
            print(f"  Max trades per run reached ({MAX_NEW_TRADES_PER_RUN})")
            break

        ticker     = trade_rec.get("ticker", "").upper()
        strike_low = trade_rec.get("strike_low")
        strike_high = trade_rec.get("strike_high")
        expiration  = trade_rec.get("expiration")
        debit       = trade_rec.get("debit")
        rationale   = trade_rec.get("rationale", "")

        if not all([ticker, strike_low, strike_high, expiration, debit is not None]):
            print(f"  Skipping incomplete trade rec: {trade_rec}")
            continue

        print(f"\n  Opening {ticker} ${strike_low}/{strike_high} debit={debit}")
        try:
            trade_module.cmd_paper_buy(
                ticker=ticker,
                strike_low=float(strike_low),
                strike_high=float(strike_high),
                expiration_str=expiration,
                debit=float(debit),
                rationale=rationale,
                context_json=None,  # auto-read from reports
            )
            results["opened"].append({
                "ticker":    ticker,
                "strategy":  "Bull Put Spread" if debit < 0 else "Bull Call Spread",
                "strikes":   f"${strike_low}/{strike_high}",
                "debit":     debit,
            })
            opened_count += 1
        except Exception as e:
            print(f"  Error opening {ticker}: {e}")
            results["errors"].append(f"Open {ticker}: {e}")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# STEP 7 — Push notification summary
# ══════════════════════════════════════════════════════════════════════════════

def send_run_summary(market_ctx, analysis, results, run_time):
    """Send push notification with run summary."""
    verdict = market_ctx.get("verdict", "N/A") if market_ctx else "N/A"
    vix     = market_ctx["vix"]["current"] if market_ctx else "N/A"

    lines = [f"🤖 Auto-run {datetime.now().strftime('%H:%M')} | {verdict} | VIX {vix}"]

    # Summary from Claude
    if analysis and analysis.get("analysis_summary"):
        summary = analysis["analysis_summary"]
        if len(summary) > 150:
            summary = summary[:147] + "..."
        lines.append(f"\n📊 {summary}")

    # Opened trades
    if results["opened"]:
        lines.append(f"\n✅ Abrí {len(results['opened'])} posición(es):")
        for t in results["opened"]:
            strategy_short = "BCS" if "Bull Call" in t["strategy"] else "BPS"
            sign = "db" if t["debit"] > 0 else "cr"
            lines.append(f"  • {t['ticker']} {strategy_short} {t['strikes']} ${abs(t['debit']):.2f}{sign}")
    else:
        no_trade = analysis.get("no_trade_reason", "") if analysis else ""
        if no_trade:
            lines.append(f"\n⏸ Sin trades nuevos: {no_trade[:100]}")
        else:
            lines.append("\n⏸ Sin trades nuevos hoy")

    # Closed positions
    if results["closed"]:
        lines.append(f"\n🔴 Cerré {len(results['closed'])} posición(es):")
        for c in results["closed"]:
            lines.append(f"  • {c['ticker']}: {c['reason'][:60]}")

    # Errors
    if results["errors"]:
        lines.append(f"\n⚠️ {len(results['errors'])} error(es) — revisar logs")

    lines.append(f"\n⏱ Completado en {run_time:.0f}s")

    message = "\n".join(lines)
    priority = "high" if results["opened"] or results["closed"] else "default"

    send_ntfy(
        title=f"Auto-run | {len(results['opened'])} abiertos | {len(results['closed'])} cerrados",
        message=message,
        priority=priority
    )


# ══════════════════════════════════════════════════════════════════════════════
# SAVE LOG
# ══════════════════════════════════════════════════════════════════════════════

def save_log(market_ctx, analysis, results, run_time):
    """Save run log to reports/."""
    os.makedirs(_REPORTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    log_path  = os.path.join(_REPORTS_DIR, f"auto_run_{timestamp}.log")

    verdict = market_ctx.get("verdict", "N/A") if market_ctx else "N/A"

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"AUTO RUN — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"{'=' * 50}\n\n")
        f.write(f"Verdict: {verdict}\n")
        f.write(f"Run time: {run_time:.0f}s\n\n")

        if analysis:
            f.write(f"ANALYSIS SUMMARY:\n{analysis.get('analysis_summary', 'N/A')}\n\n")

        f.write(f"OPENED ({len(results['opened'])}):\n")
        for t in results["opened"]:
            f.write(f"  {t['ticker']} {t['strategy']} {t['strikes']} debit={t['debit']}\n")

        f.write(f"\nCLOSED ({len(results['closed'])}):\n")
        for c in results["closed"]:
            f.write(f"  {c['ticker']}: {c['reason']}\n")

        if results["errors"]:
            f.write(f"\nERRORS:\n")
            for e in results["errors"]:
                f.write(f"  {e}\n")

        if analysis and analysis.get("new_trades"):
            f.write(f"\nFULL TRADE RATIONALE:\n")
            for t in analysis["new_trades"]:
                f.write(f"\n{t['ticker']} ${t.get('strike_low')}/{t.get('strike_high')}:\n")
                f.write(f"{t.get('rationale', 'N/A')}\n")

        if analysis and analysis.get("no_trade_reason"):
            f.write(f"\nNO TRADE REASON:\n{analysis['no_trade_reason']}\n")

    print(f"\n  Log saved: {log_path}")
    return log_path


# ══════════════════════════════════════════════════════════════════════════════
# MARKET HOURS CHECK
# ══════════════════════════════════════════════════════════════════════════════

def is_market_day():
    """Only run on weekdays."""
    return datetime.now().weekday() < 5


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    start_time = time.time()
    timestamp  = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'=' * 55}")
    print(f"  AUTO RUN — {timestamp}")
    print(f"{'=' * 55}")

    if not is_market_day():
        print("  Weekend — skipping run")
        return

    # Step 1 — Market context
    market_ctx = run_market_context()

    # Step 2 — Scanner
    scanner_report = run_scanner()

    # Step 3 — Paper sync (auto-closes stop loss / target)
    run_paper_sync()

    # Step 4 — Get current DB state
    print("\n  [5/6] Reading DB state...")
    db_state = get_current_state()
    print(f"  Open positions: {len(db_state['open'])} | "
          f"Recent closed: {len(db_state['recently_closed'])}")

    # Step 5 — Claude analysis
    analysis = run_claude_analysis(market_ctx, scanner_report, db_state)

    # Step 6 — Execute recommendations
    print("\n  [6/6] Executing recommendations...")
    results = execute_recommendations(analysis)

    run_time = time.time() - start_time

    # Summary push notification
    send_run_summary(market_ctx, analysis, results, run_time)

    # Save log
    save_log(market_ctx, analysis, results, run_time)

    print(f"\n{'=' * 55}")
    print(f"  AUTO RUN COMPLETE — {run_time:.0f}s")
    print(f"  Opened: {len(results['opened'])} | "
          f"Closed: {len(results['closed'])} | "
          f"Errors: {len(results['errors'])}")
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    main()