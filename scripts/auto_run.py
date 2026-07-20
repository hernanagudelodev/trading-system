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

    OBLIGATORIAS (el run explota si faltan — a propósito):
        MAX_PORTFOLIO_RISK_PCT  tope de riesgo agregado, % del capital (ej: 40)
        ACCOUNT_NLV             capital base
        TRADING_MODE            'paper' | 'live'  (ausente -> 'paper')
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

# Notificaciones: fuente ÚNICA en notify.py. Antes había una copia de send_push
# acá y otra en monitor.py, ya divergidas (monitor chequeaba status_code, esta
# no). Misma enfermedad que las tres copias de pricing.
from notify import send_push

AI_MODEL = "claude-sonnet-4-6"


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

def run_step3_sync():
    """
    Paso 3: sincroniza P&L y detecta cierres, en el libro del modo activo.

    paper -> cmd_paper_sync(): recalcula P&L de paper_positions con precios reales
             y auto-cierra stops/targets de paper.
    live  -> run_sync(): baja del broker a `positions` — el estado real manda.

    EL BUG QUE ESTO ARREGLA (20-jul, primer auto_run en live)
        Antes esto llamaba cmd_paper_sync() SIEMPRE, sin mirar el modo. En live
        el paso 3 sincronizaba contra paper_positions — la tabla equivocada —
        mientras el resto del run operaba sobre la cuenta real. Mismo patrón que
        el bug del monitor: dos funciones (una paper, una live) y el llamador
        elegía la de paper sin preguntar el modo. La bandera vive en
        current_mode() y en ningún otro lado.
    """
    from executor import current_mode
    mode = current_mode()
    print(f"\n  [3/6] Sincronizando [{mode}]...")
    try:
        import trade as trade_module
        if mode == "live":
            trade_module.run_sync()
        else:
            trade_module.cmd_paper_sync()
        return True
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Get current state from DB
# ══════════════════════════════════════════════════════════════════════════════

def get_current_state():
    """
    Estado de la cartera del LIBRO ACTIVO para dárselo a Claude.

    La tabla depende del modo — antes 'paper_positions' estaba hardcodeado, así
    que en live le pasaba a Claude la cartera de PAPER como si fuera la real. La
    decisión de qué abrir con plata real se tomaba mirando el libro equivocado:
    el gate de concentración creería tomado un ticker que en la cuenta real está
    libre, y al revés. Mismo patrón que execute_recommendations, que ya elegía
    la tabla por modo — esta lectura se había quedado atrás.
    """
    from executor import current_mode
    table = "positions" if current_mode() == "live" else "paper_positions"
    try:
        import psycopg2
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur  = conn.cursor()

        # Posiciones abiertas del libro activo
        cur.execute(f"""
            SELECT ticker, strategy, strike_low, strike_high, expiration,
                   premium_paid, total_cost, gross_pnl, pnl_pct,
                   profit_pct_of_max, opened_at
            FROM {table}
            WHERE UPPER(status) = 'OPEN'
            ORDER BY opened_at DESC
        """)
        cols  = [d[0] for d in cur.description]
        open_positions = [dict(zip(cols, row)) for row in cur.fetchall()]

        # Cerradas recientes (últimas 5) del mismo libro
        cur.execute(f"""
            SELECT ticker, strategy, strike_low, strike_high,
                   gross_pnl, pnl_pct, close_reason, closed_at
            FROM {table}
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

    # El tope de cartera se le declara al LLM para que ordene por convicción.
    # El gate de código lo hace cumplir igual — esto es contexto, no garantía.
    from option_selector import portfolio_risk_pct
    _pct = portfolio_risk_pct()

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
3. Evalúa cada candidato con criterio conservador. Conservador significa elegir bien y respetar los gates —NO significa abstenerse de operar cuando hay un buen candidato. Si el scanner trae una estructura viable con señal clara, ábrela.
4. Decide cuáles abrir como paper trade y cuáles ignorar.
5. Decide si alguna posición abierta debe cerrarse anticipadamente por cambio de tesis.

Reglas:
- debit positivo = Bull Call Spread (pagas), negativo = Bull Put Spread (cobras crédito)
- Solo recomendar trades con señal clara y contexto favorable
- Si hay evento macro VERY_HIGH en 2 días o menos, NO abrir ninguna posición nueva (regla dura: el sistema descarta cualquier apertura igual). Los cierres sí están permitidos.
- Eventos HIGH o MEDIUM (CPI, PPI, NFP, etc.) NO prohíben abrir. Son contexto para elegir mejor, no motivo para abstenerte. NO inventes reglas duras de evento: la ÚNICA prohibición por evento es la de VERY_HIGH del punto anterior, que ya aplica el sistema por código. Con un HIGH podés y debés operar si hay un candidato con señal clara.
- No abrir trades con earnings del subyacente en menos de 21 días (riesgo de IV crush)
- No hay límite de CANTIDAD de trades por run. Lo que limita es el RIESGO AGREGADO: el sistema rechaza toda apertura que haga que la pérdida máxima combinada de la cartera supere el {_pct:.0f}% del capital.
- Ordená new_trades por convicción DESCENDENTE. Si el presupuesto de riesgo se agota, el sistema abre los primeros de la lista y descarta el resto: el orden importa.
- Si no hay nada convincente, devolver new_trades vacío y explicar en no_trade_reason
- NO incluyas fecha de expiración: el sistema la fija automáticamente desde la cadena real (no la calcules tú)

Responde SOLO con este JSON (sin texto adicional, sin markdown):
{{
  "headline": "Frase corta y COMPLETA (máximo 120 caracteres) que resuma la decisión del día para una notificación push. Debe entenderse sola y NO cortarse a la mitad. Ej: 'Abrí TGT y MSFT; resto sin señal clara' o 'Sin aperturas: ningún candidato pasó los filtros hoy'.",
  "analysis_summary": "Párrafo breve del contexto del día y decisiones tomadas",
  "new_trades": [
    {{
      "ticker": "CVS",
      "strategy": "Bull Call Spread",
      "strike_low": 94.0,
      "strike_high": 101.0,
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

    # El executor se elige UNA vez (paper/live según TRADING_MODE). auto_run
    # emite intenciones y no vuelve a preguntar el modo.
    from executor import get_executor, OpenIntent
    executor = get_executor()

    # Close positions Claude recommends closing
    for close_rec in analysis.get("close_positions", []):
        ticker = close_rec.get("ticker", "").upper()
        reason = close_rec.get("reason", "AUTO_CLOSE_AI")
        print(f"\n  Closing {ticker}: {reason}")
        try:
            # close_position devuelve True solo si el cierre se ejecutó.
            # Si no consiguió precio real, se niega y devuelve False:
            # NO reportarlo como cerrado (antes el log/push mentía — caso DAL).
            ok = executor.close_position(ticker, reason)
            if ok:
                results["closed"].append({
                    "ticker": ticker,
                    "reason": reason
                })
            else:
                msg = f"{ticker}: cierre NO ejecutado (sin precio real) — sigue ABIERTA"
                print(f"  ⚠️  {msg}")
                results["errors"].append(f"Close {msg}")
        except Exception as e:
            print(f"  Error closing {ticker}: {e}")
            results["errors"].append(f"Close {ticker}: {e}")

    # Open new trades
    new_trades = analysis.get("new_trades", [])

    # ── Estado de la cartera del libro activo ─────────────────────────────────
    # UNA lectura para dos gates: concentración (mismo ticker) y riesgo agregado.
    # La tabla depende del libro. Antes 'paper_positions' estaba hardcodeado —
    # en live habría leído el libro equivocado sin decir nada.
    from executor import current_mode
    from option_selector import CAPITAL, position_max_loss, portfolio_risk_pct

    _mode  = current_mode()
    _table = "positions" if _mode == "live" else "paper_positions"

    # Tope agregado: obligatorio en los DOS libros. Sin default silencioso.
    PCT                = portfolio_risk_pct()
    MAX_PORTFOLIO_RISK = CAPITAL * PCT / 100.0

    open_tickers = set()
    current_risk = 0.0
    try:
        import psycopg2
        _c   = psycopg2.connect(os.getenv("DATABASE_URL"))
        _cur = _c.cursor()
        _cur.execute(f"""
            SELECT UPPER(ticker), strike_low, strike_high, premium_paid, contracts
            FROM {_table}
            WHERE UPPER(status) = 'OPEN'
        """)
        for _tkr, _sl, _sh, _prem, _n in _cur.fetchall():
            open_tickers.add(_tkr)
            current_risk += position_max_loss(_sl, _sh, _prem, _n)
        _cur.close(); _c.close()
    except Exception as e:
        # FAIL-CLOSED. Antes seguía "sin bloqueo de concentración" — pero no poder
        # ver tu exposición es exactamente cuando NO hay que abrir. Los cierres
        # de arriba ya se ejecutaron y se conservan en results.
        msg = f"no se pudo leer la cartera ({_table}): {e} — NO se abre nada este run"
        print(f"  ⛔ {msg}")
        results["errors"].append(msg)
        return results

    print(f"\n  [cartera/{_mode}] {len(open_tickers)} abiertas · "
          f"riesgo ${current_risk:,.0f} / ${MAX_PORTFOLIO_RISK:,.0f} "
          f"({current_risk/CAPITAL*100:.1f}% del capital)")

    print(f"  [tope/{PCT:.0f}%] sin límite de cantidad — el presupuesto de "
          f"riesgo se consume en el ORDEN de la lista del LLM")

    opened_this_run = set()

    for trade_rec in new_trades:
        ticker     = trade_rec.get("ticker", "").upper()

        # Bloqueo de mismo ticker (cartera + este run)
        if ticker in open_tickers or ticker in opened_this_run:
            print(f"  [concentración] {ticker} ya tiene posición abierta — se omite (no apilar mismo nombre)")
            continue

        strike_low  = trade_rec.get("strike_low")
        strike_high = trade_rec.get("strike_high")
        debit       = trade_rec.get("debit")
        rationale   = trade_rec.get("rationale", "")

        # Completitud de lo que dio el LLM — antes de gastar red en la cadena.
        if not all([ticker, strike_low, strike_high, debit is not None]):
            print(f"  Skipping incomplete trade rec: {trade_rec}")
            continue

        # Tope de riesgo agregado de cartera.
        _new_risk = position_max_loss(strike_low, strike_high, debit)
        if current_risk + _new_risk > MAX_PORTFOLIO_RISK:
            print(f"  [tope-cartera] {ticker} se omite: ${current_risk:,.0f} "
                  f"+ ${_new_risk:,.0f} > ${MAX_PORTFOLIO_RISK:,.0f}")
            continue

        # B: la fecha de expiración NO la elige el LLM (inventa sábados).
        # Se toma la expiración REAL de la cadena — misma regla que option_selector.
        import option_selector
        real_exp = option_selector.get_real_expiration(ticker)
        if real_exp is None:
            print(f"  ⚠️  {ticker}: no se pudo obtener expiración real de la cadena — se omite")
            continue
        expiration = real_exp.isoformat()
        if trade_rec.get("expiration") and trade_rec["expiration"] != expiration:
            print(f"  [exp-fix] {ticker}: LLM dijo {trade_rec['expiration']}, "
                  f"se usa la real {expiration}")

        print(f"\n  Opening {ticker} ${strike_low}/{strike_high} debit={debit}")
        try:
            intent = OpenIntent(
                ticker=ticker,
                strike_low=float(strike_low),
                strike_high=float(strike_high),
                expiration=expiration,
                debit=float(debit),
                rationale=rationale,
                context_json=None,  # auto-read from reports
            )
            ok = executor.open_position(intent)
            if ok:
                results["opened"].append({
                    "ticker":    ticker,
                    "strategy":  "Bull Put Spread" if debit < 0 else "Bull Call Spread",
                    "strikes":   f"${strike_low}/{strike_high}",
                    "debit":     debit,
                })
                opened_this_run.add(ticker)
                current_risk += _new_risk        # acumular DENTRO del run
            else:
                msg = f"{ticker}: apertura NO ejecutada"
                print(f"  ⚠️  {msg}")
                results["errors"].append(f"Open {msg}")
        except Exception as e:
            print(f"  Error opening {ticker}: {e}")
            results["errors"].append(f"Open {ticker}: {e}")


    # La DB no sabe nada de lo que se acaba de abrir. El executor decide si hace
    # falta sincronizar: paper no (cmd_paper_buy ya escribió), live sí. Se llama
    # sin preguntar el modo — la bandera vive en get_executor() y en ningún otro
    # lado.
    if results["opened"]:
        try:
            executor.sync_after_opens()
        except Exception as e:
            msg = f"sync post-apertura falló: {e}"
            print(f"  ⚠️  {msg}")
            results["errors"].append(msg)

    return results


# ══════════════════════════════════════════════════════════════════════════════
# STEP 7 — Push notification summary
# ══════════════════════════════════════════════════════════════════════════════

def send_run_summary(market_ctx, analysis, results, run_time):
    """Send push notification with run summary — corto y completo, sin cortar frases."""
    from executor import current_mode
    mode    = current_mode()
    tag     = "🔴 LIVE" if mode == "live" else "📄 PAPER"
    verdict = market_ctx.get("verdict", "N/A") if market_ctx else "N/A"
    vix     = market_ctx["vix"]["current"] if market_ctx else "N/A"

    # El modo va PRIMERO y con color: con dos workers corriendo, lo primero que
    # tenés que saber al ver el push es si movió plata real o no.
    lines = [f"{tag} · Auto-run {datetime.now().strftime('%H:%M')} | {verdict} | VIX {vix}"]

    # Headline: frase completa que el LLM escribió para que quepa (no se trunca).
    # Fallback al analysis_summary recortado en límite de palabra si no hay headline.
    headline = (analysis.get("headline") if analysis else "") or ""
    if not headline and analysis and analysis.get("analysis_summary"):
        s = analysis["analysis_summary"]
        headline = s if len(s) <= 140 else s[:137].rsplit(" ", 1)[0] + "…"
    if headline:
        lines.append(f"\n📊 {headline}")

    # Opened trades (líneas cortas y completas)
    if results["opened"]:
        lines.append(f"\n✅ Abrí {len(results['opened'])} posición(es):")
        for t in results["opened"]:
            strategy_short = "BCS" if "Bull Call" in t["strategy"] else "BPS"
            sign = "db" if t["debit"] > 0 else "cr"
            lines.append(f"  • {t['ticker']} {strategy_short} {t['strikes']} ${abs(t['debit']):.2f}{sign}")
    elif not results["closed"]:
        # Sin aperturas ni cierres: el headline ya explica el porqué; no repetir
        # el no_trade_reason largo (queda completo en el log/DB).
        lines.append("\n⏸ Sin trades nuevos")

    # Closed positions (motivo recortado en límite de palabra, no a la mitad)
    if results["closed"]:
        lines.append(f"\n🔴 Cerré {len(results['closed'])} posición(es):")
        for c in results["closed"]:
            r = c["reason"]
            r = r if len(r) <= 60 else r[:57].rsplit(" ", 1)[0] + "…"
            lines.append(f"  • {c['ticker']}: {r}")

    # Errors
    if results["errors"]:
        lines.append(f"\n⚠️ {len(results['errors'])} error(es) — revisar logs")

    lines.append(f"\n⏱ Completado en {run_time:.0f}s")

    message = "\n".join(lines)
    priority = "high" if results["opened"] or results["closed"] else "default"

    send_push(
        title=f"{tag} · {len(results['opened'])} abiertos · {len(results['closed'])} cerrados",
        message=message,
        priority=priority
    )


# ══════════════════════════════════════════════════════════════════════════════
# SAVE LOG — file + DB
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_log_table():
    """Create auto_run_logs table if it doesn't exist."""
    
    try:
        import psycopg2
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_run_logs (
                id           SERIAL PRIMARY KEY,
                run_at       TIMESTAMP DEFAULT NOW(),
                slot         VARCHAR(20),
                verdict      VARCHAR(20),
                vix          DECIMAL(6,2),
                opened       INTEGER DEFAULT 0,
                closed       INTEGER DEFAULT 0,
                errors       INTEGER DEFAULT 0,
                summary      TEXT,
                no_trade_reason TEXT,
                full_log     TEXT,
                run_time_sec INTEGER,
                mode         VARCHAR(10) NOT NULL DEFAULT 'paper'
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"  Log table error: {e}")


def _save_log_to_db(market_ctx, analysis, results, run_time, slot="unknown"):
    """Save run log to auto_run_logs table in PostgreSQL."""
    try:
        import psycopg2
        _ensure_log_table()

        verdict = market_ctx.get("verdict", "N/A") if market_ctx else "N/A"
        vix     = market_ctx["vix"]["current"] if market_ctx else None
        summary = analysis.get("analysis_summary", "") if analysis else ""
        no_trade = analysis.get("no_trade_reason", "") if analysis else ""

        # Build full log text
        lines = [f"AUTO RUN — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                 f"Slot: {slot} | Verdict: {verdict} | Run time: {run_time:.0f}s",
                 ""]

        if summary:
            lines += [f"ANALYSIS:\n{summary}", ""]

        lines.append(f"OPENED ({len(results['opened'])}):")
        for t in results["opened"]:
            lines.append(f"  {t['ticker']} {t['strategy']} {t['strikes']} debit={t['debit']}")

        lines.append(f"\nCLOSED ({len(results['closed'])}):")
        for c in results["closed"]:
            lines.append(f"  {c['ticker']}: {c['reason']}")

        if results["errors"]:
            lines.append(f"\nERRORS:")
            for e in results["errors"]:
                lines.append(f"  {e}")

        if analysis and analysis.get("new_trades"):
            lines.append(f"\nTRADE RATIONALE:")
            for t in analysis["new_trades"]:
                lines.append(f"\n{t['ticker']} ${t.get('strike_low')}/{t.get('strike_high')}:")
                lines.append(t.get("rationale", "N/A"))

        if no_trade:
            lines.append(f"\nNO TRADE REASON:\n{no_trade}")

        full_log = "\n".join(lines)

        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur  = conn.cursor()
        
        from executor import current_mode

        cur.execute("""
            INSERT INTO auto_run_logs
                (slot, verdict, vix, opened, closed, errors,
                 summary, no_trade_reason, full_log, run_time_sec, mode)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            slot,
            verdict,
            vix,
            len(results["opened"]),
            len(results["closed"]),
            len(results["errors"]),
            summary[:1000] if summary else "",
            no_trade[:500] if no_trade else "",
            full_log,
            int(run_time),
            current_mode(),
        ))

        log_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        print(f"  Log saved to DB (id={log_id})")
        return log_id
    except Exception as e:
        print(f"  DB log error: {e}")
        return None


def save_log(market_ctx, analysis, results, run_time, slot="unknown"):
    """Save run log to DB (persistent) + file (local)."""

    # Always save to DB — persists across Railway deploys
    _save_log_to_db(market_ctx, analysis, results, run_time, slot)

    # Also save to file locally (useful for local runs)
    try:
        os.makedirs(_REPORTS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        log_path  = os.path.join(_REPORTS_DIR, f"auto_run_{timestamp}.log")
        verdict   = market_ctx.get("verdict", "N/A") if market_ctx else "N/A"

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

        print(f"  Log saved: {log_path}")
        return log_path
    except Exception as e:
        print(f"  File log error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# WEEKLY SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def generate_weekly_summary():
    """
    Generate weekly summary of paper trading performance.
    Called every Friday on the second run of the day.
    Saves to reports/weekly_summary_YYYY-MM-DD.md and sends push.
    """
    try:
        import psycopg2
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur  = conn.cursor()

        # Trades closed this week
        cur.execute("""
            SELECT ticker, strategy, strike_low, strike_high,
                   gross_pnl, pnl_pct, close_reason, closed_at,
                   opened_at
            FROM paper_positions
            WHERE UPPER(status) = 'CLOSED'
              AND closed_at >= NOW() - INTERVAL '7 days'
            ORDER BY closed_at DESC
        """)
        cols   = [d[0] for d in cur.description]
        closed = [dict(zip(cols, row)) for row in cur.fetchall()]

        # Currently open positions
        cur.execute("""
            SELECT ticker, strategy, strike_low, strike_high,
                   expiration, gross_pnl, pnl_pct, profit_pct_of_max,
                   opened_at
            FROM paper_positions
            WHERE UPPER(status) = 'OPEN'
            ORDER BY opened_at
        """)
        cols = [d[0] for d in cur.description]
        open_pos = [dict(zip(cols, row)) for row in cur.fetchall()]

        cur.close()
        conn.close()

        # Calculate weekly P&L
        total_pnl  = sum(float(p["gross_pnl"] or 0) for p in closed)
        wins       = sum(1 for p in closed if float(p["gross_pnl"] or 0) > 0)
        losses     = len(closed) - wins
        win_rate   = round(wins / len(closed) * 100, 1) if closed else 0

        timestamp  = datetime.now().strftime("%Y-%m-%d")
        lines      = [
            f"# Weekly Summary — {timestamp}",
            f"",
            f"## Resumen Semanal",
            f"- Trades cerrados: {len(closed)}",
            f"- Ganadores: {wins} | Perdedores: {losses}",
            f"- Win rate: {win_rate}%",
            f"- P&L neto semana: ${total_pnl:+.0f}",
            f"",
        ]

        if closed:
            lines.append("## Trades Cerrados Esta Semana")
            lines.append("")
            for p in closed:
                pnl    = float(p["gross_pnl"] or 0)
                reason = p["close_reason"] or "MANUAL"
                sign   = "✅" if pnl >= 0 else "❌"
                strat  = "BCS" if "Call" in (p["strategy"] or "") else "BPS"
                lines.append(
                    f"{sign} {p['ticker']} {strat} "
                    f"${p['strike_low']}/{p['strike_high']} | "
                    f"P&L ${pnl:+.0f} | {reason}"
                )
            lines.append("")

        if open_pos:
            lines.append("## Posiciones Abiertas")
            lines.append("")
            for p in open_pos:
                pnl  = float(p["gross_pnl"] or 0) if p["gross_pnl"] else 0
                pmax = float(p["profit_pct_of_max"] or 0) * 100 if p["profit_pct_of_max"] else 0
                exp  = str(p["expiration"])[:10]
                dte  = (datetime.strptime(exp, "%Y-%m-%d").date() -
                        datetime.now().date()).days if exp else "?"
                strat = "BCS" if "Call" in (p["strategy"] or "") else "BPS"
                lines.append(
                    f"- {p['ticker']} {strat} "
                    f"${p['strike_low']}/{p['strike_high']} "
                    f"({dte}d) | P&L ${pnl:+.0f} | {pmax:.0f}% max"
                )
            lines.append("")

        lines.append(f"---")
        lines.append(f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}")

        content   = "\n".join(lines)
        log_path  = os.path.join(_REPORTS_DIR, f"weekly_summary_{timestamp}.md")
        os.makedirs(_REPORTS_DIR, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"\n  Weekly summary saved: {log_path}")

        # Push notification
        push_lines = [
            f"📊 Resumen semanal {timestamp}",
            f"Cerrados: {len(closed)} | Win rate: {win_rate}%",
            f"P&L neto: ${total_pnl:+.0f}",
        ]
        if closed:
            push_lines.append("")
            for p in closed[:5]:  # max 5 in push
                pnl  = float(p["gross_pnl"] or 0)
                sign = "✅" if pnl >= 0 else "❌"
                push_lines.append(f"{sign} {p['ticker']}: ${pnl:+.0f}")

        push_lines.append(f"\nAbiertas: {len(open_pos)}")

        send_push(
            title=f"Resumen semanal | P&L ${total_pnl:+.0f} | {win_rate}% win",
            message="\n".join(push_lines),
            priority="default"
        )

        return log_path

    except Exception as e:
        print(f"  Weekly summary error: {e}")
        return None


def is_friday_afternoon():
    """True on Fridays after 2pm ET (19:00 UTC)."""
    now = datetime.utcnow()
    return now.weekday() == 4 and now.hour >= 19


# ══════════════════════════════════════════════════════════════════════════════
# MARKET HOURS CHECK
# ══════════════════════════════════════════════════════════════════════════════

def is_market_day():
    """Only run on weekdays."""
    return datetime.now().weekday() < 5


# ══════════════════════════════════════════════════════════════════════════════
# DATA QUALITY GUARD
# ══════════════════════════════════════════════════════════════════════════════

def assess_data_quality(market_ctx, scanner_report):
    """
    Detecta degradación silenciosa ANTES de llamar a la IA.
    Devuelve (ok: bool, level: str, reason: str).
      level = 'abort' (feed roto, no operar) | 'warn' (raro pero válido) | 'ok'

    Casos 'abort':
      - market_context devolvió None (macro no disponible)
      - scanner devolvió None/vacío
      - >80% de los candidatos tienen IV N/A (feed Tastytrade caído)
    """
    import re

    if market_ctx is None:
        return False, "abort", "market_context devolvió None — macro no disponible"

    if not scanner_report or not scanner_report.strip():
        return False, "abort", "scanner devolvió None/vacío"

    iv_vals = re.findall(r"\|\s*IV\s+(N/A|[0-9.]+%)", scanner_report)
    total   = len(iv_vals)
    iv_na   = sum(1 for v in iv_vals if v == "N/A")

    if total == 0:
        return True, "warn", "0 candidatos pasaron filtros (posible mercado débil)"

    na_frac = iv_na / total
    if total >= 5 and na_frac > 0.8:
        return False, "abort", (
            f"IV N/A en {iv_na}/{total} candidatos ({na_frac:.0%}) — "
            f"feed Tastytrade caído, NO es condición de mercado"
        )

    return True, "ok", f"datos OK ({iv_na}/{total} con IV N/A)"


def event_block_active(market_ctx, max_days=2):
    """
    Compuerta dura de evento macro.
    Devuelve (blocked: bool, event: dict|None).
    Bloquea aperturas si hay un evento VERY_HIGH a <= max_days días.
    HIGH/MEDIUM NO bloquean (decisión: solo VERY_HIGH).
    """
    if not market_ctx:
        return False, None
    for e in market_ctx.get("macro_events", []):
        days = e.get("days_away")
        if e.get("impact") == "VERY_HIGH" and days is not None and days <= max_days:
            return True, e
    return False, None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    start_time = time.time()
    timestamp  = datetime.now().strftime("%Y-%m-%d %H:%M")

    from executor import current_mode
    RUN_TAG = "🔴 LIVE" if current_mode() == "live" else "📄 PAPER"

    print(f"\n{'=' * 55}")
    print(f"  AUTO RUN [{current_mode()}] — {timestamp}")
    print(f"{'=' * 55}")

    if not is_market_day():
        print("  Weekend — skipping run")
        return

    # Wrap entire run in try/except to catch failures and notify
    try:
        # Step 1 — Market context
        print("\n  [1/6] Running market_context.py...")
        market_ctx = run_market_context()

        # Step 2 — Scanner
        print("\n  [2/6] Running scanner --universe...")
        scanner_report = run_scanner()

        # ── GUARDA DE CALIDAD DE DATOS ──────────────────────────────────────
        # Aborta ANTES de la IA si la macro/scanner fallaron o si la IV viene
        # N/A en casi todo el universo (feed Tastytrade caído). Convierte un
        # fallo silencioso ("no trades") en un fallo ruidoso (push urgent).
        ok, level, reason = assess_data_quality(market_ctx, scanner_report)
        print(f"\n  [guard] {level.upper()}: {reason}")

        if not ok:  # level == 'abort'
            run_time = time.time() - start_time
            send_push(
                title=f"{RUN_TAG} · 🚨 Auto-run ABORTADO — datos inválidos",
                message=(
                    f"Run {timestamp} abortado ANTES de la IA.\n"
                    f"{reason}\n"
                    f"No se llamó a Claude ni se tocó ninguna posición.\n"
                    f"Revisar Tastytrade/credenciales en Railway."
                ),
                priority="urgent",
            )
            save_log(
                market_ctx, None,
                {"opened": [], "closed": [], "errors": [f"ABORT: {reason}"]},
                run_time, slot=os.getenv("AUTO_RUN_SLOT", "manual"),
            )
            return

        if level == "warn":
            send_push(
                title=f"{RUN_TAG} · ⚠️ Auto-run — datos sospechosos",
                message=f"Run {timestamp}: {reason}\nContinúa, pero revisá.",
                priority="high",
            )

        # Step 3 — Sync del libro activo (auto-cierra stop/target en paper;
        # en live baja el estado real del broker). Mode-aware: ver run_step3_sync.
        run_step3_sync()

        # Step 4 — Get current DB state
        print("\n  [4/6] Reading DB state...")
        db_state = get_current_state()
        print(f"  Open positions: {len(db_state['open'])} | "
              f"Recent closed: {len(db_state['recently_closed'])}")

        # Step 5 — Claude analysis
        print("\n  [5/6] Calling Anthropic API...")
        analysis = run_claude_analysis(market_ctx, scanner_report, db_state)

        # ── COMPUERTA DE EVENTO MACRO (determinista, por encima del LLM) ────
        # Si hay un evento VERY_HIGH a ≤2 días, se descartan TODAS las aperturas
        # propuestas por el LLM. Los cierres se respetan (salir siempre permitido).
        blocked, ev = event_block_active(market_ctx, max_days=2)
        if blocked and analysis and analysis.get("new_trades"):
            dropped = len(analysis["new_trades"])
            analysis["new_trades"] = []
            print(f"\n  [event-gate] BLOQUEADO: {ev['event']} VERY_HIGH en "
                  f"{ev['days_away']}d → {dropped} apertura(s) descartada(s)")
            send_push(
                title=f"{RUN_TAG} · 🚫 Aperturas bloqueadas — evento VERY_HIGH",
                message=(
                    f"{ev['event']} en {ev['days_away']}d (VERY_HIGH).\n"
                    f"El LLM propuso {dropped} apertura(s); la compuerta las descartó.\n"
                    f"Los cierres no se ven afectados."
                ),
                priority="high",
            )
        elif blocked:
            print(f"\n  [event-gate] {ev['event']} VERY_HIGH en {ev['days_away']}d "
                  f"— sin aperturas propuestas, nada que descartar")

        # Step 6 — Execute recommendations
        print("\n  [6/6] Executing recommendations...")
        results = execute_recommendations(analysis)

        run_time = time.time() - start_time

        # Summary push notification
        send_run_summary(market_ctx, analysis, results, run_time)

        # Save log to DB + file
        save_log(market_ctx, analysis, results, run_time,
                 slot=os.getenv("AUTO_RUN_SLOT", "manual"))

        # Weekly summary — only on Fridays afternoon run
        if is_friday_afternoon():
            print("\n  Generating weekly summary...")
            generate_weekly_summary()

        print(f"\n{'=' * 55}")
        print(f"  AUTO RUN COMPLETE — {run_time:.0f}s")
        print(f"  Opened: {len(results['opened'])} | "
              f"Closed: {len(results['closed'])} | "
              f"Errors: {len(results['errors'])}")
        print(f"{'=' * 55}\n")

    except Exception as e:
        run_time = time.time() - start_time
        error_msg = str(e)
        print(f"\n  FATAL ERROR: {error_msg}")
        traceback.print_exc()

        # Send failure alert
        send_push(
            title=f"{RUN_TAG} · 🚨 Auto-run FALLÓ",
            message=(
                f"Error en auto-run {timestamp}\n"
                f"Error: {error_msg[:200]}\n"
                f"Tiempo: {run_time:.0f}s\n"
                f"Revisar logs en Railway."
            ),
            priority="urgent"
        )


if __name__ == "__main__":
    main()