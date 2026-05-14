"""
monitor.py
==========
Position monitoring script for Bull Call Spread analysis system.

Runs in background during market hours checking open positions against:
    - Take profit targets
    - Stop loss limits
    - Days to expiration (DTE)
    - Current market criteria

Alert levels:
    🟢 NORMAL  → within expected ranges       → console only
    🟡 WATCH   → approaching targets          → console only
    🟠 ACTION  → take profit reached          → console + email
    🔴 URGENT  → stop loss / max profit       → console + email immediately

Usage:
    python monitor.py              → run once
    python monitor.py --loop       → run every 30 min during market hours
    python monitor.py --interval 15 → run every 15 min

Dependencies:
    criteria.py  → current market data
    scoring.py   → current score
    db.py        → positions + snapshots
    .env         → DATABASE_URL, ANTHROPIC_API_KEY,
                   EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD
"""

import os
import time
import argparse
import smtplib
import schedule
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import anthropic
from dotenv import load_dotenv

from criteria import get_all_criteria
from scoring import score_criteria
from db import get_open_positions, save_analysis

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Exit rules (from your personal trading system — Week 8)
TAKE_PROFIT_MIN_PCT  = 0.50   # 50% of max profit → ACTION
TAKE_PROFIT_MAX_PCT  = 0.70   # 70% of max profit → URGENT
STOP_LOSS_PCT        = 0.50   # 50% of premium paid lost → URGENT
STOP_LOSS_WATCH_PCT  = 0.25   # 25% of premium paid lost → WATCH
WATCH_PROFIT_PCT     = 0.30   # 30% of max profit → WATCH
MIN_DTE              = 7      # days to expiration → ACTION
WATCH_DTE            = 10     # days to expiration → WATCH

# Market hours (ET)
MARKET_OPEN_HOUR     = 9
MARKET_OPEN_MIN      = 30
MARKET_CLOSE_HOUR    = 16
MARKET_CLOSE_MIN     = 0

# Email config
EMAIL_FROM     = os.getenv("EMAIL_FROM")
EMAIL_TO       = os.getenv("EMAIL_TO")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

# AI config
AI_MODEL      = "claude-opus-4-5"
AI_MAX_TOKENS = 1500

# Trading context
TRADING_CONTEXT = {
    "strategy": "Bull Call Spread",
    "capital":  15000,
    "language": "Spanish",
}


# ══════════════════════════════════════════════════════════════════════════════
# MARKET HOURS CHECK
# ══════════════════════════════════════════════════════════════════════════════

def is_market_open():
    """
    Check if US market is currently open.
    Simple check — does not account for holidays.
    """
    now = datetime.now()

    # Weekend check
    if now.weekday() >= 5:
        return False

    # Hours check (assumes local time is ET — adjust if needed)
    market_open  = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MIN,  second=0)
    market_close = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0)

    return market_open <= now <= market_close

# ══════════════════════════════════════════════════════════════════════════════
# P&L CALCULATIONS
# ══════════════════════════════════════════════════════════════════════════════

def calculate_current_pnl(position, current_price):
    """
    Estimate current P&L based on intrinsic value of the spread.

    For a Bull Call Spread:
        Intrinsic value = max(0, price - strike_low) - max(0, price - strike_high)
        Current value   = intrinsic value * contracts * 100
        Current P&L     = current value - total cost

    This is an approximation — actual value includes time value.
    For accurate value, use Thinkorswim.

    Returns:
        dict with keys:
            current_value   (float) — estimated current spread value
            gross_pnl       (float) — current gain/loss
            pnl_pct         (float) — % of premium paid
            max_profit      (float) — maximum possible profit
            profit_pct_of_max (float) — % of max profit achieved
            dte             (int | None) — days to expiration
    """
    strike_low  = float(position.get("strike_low") or 0)
    strike_high = float(position.get("strike_high") or 0)
    contracts   = int(position.get("contracts") or 1)
    total_cost  = float(position.get("total_cost") or 0)
    premium     = float(position.get("premium_paid") or 0)
    expiration  = position.get("expiration")

    # Intrinsic value of the spread
    low_leg  = max(0, current_price - strike_low)
    high_leg = max(0, current_price - strike_high)
    spread_value = (low_leg - high_leg) * contracts * 100

    # Max possible profit
    max_profit = (strike_high - strike_low - premium) * contracts * 100

    # P&L
    gross_pnl = spread_value - total_cost
    pnl_pct   = (gross_pnl / total_cost * 100) if total_cost != 0 else 0

    # % of max profit achieved
    profit_pct_of_max = (gross_pnl / max_profit) if max_profit > 0 else 0

    # Days to expiration
    dte = None
    if expiration:
        exp_date = expiration if isinstance(expiration, date) else expiration.date()
        dte = (exp_date - date.today()).days

    return {
        "current_value":      round(spread_value, 2),
        "gross_pnl":          round(gross_pnl, 2),
        "pnl_pct":            round(pnl_pct, 2),
        "max_profit":         round(max_profit, 2),
        "profit_pct_of_max":  round(profit_pct_of_max, 4),
        "dte":                dte,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ALERT LEVELS
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_alert_level(pnl_data):
    """
    Evaluate alert level based on P&L and DTE.

    Returns:
        tuple: (level: str, reasons: list[str])
        level: "NORMAL" | "WATCH" | "ACTION" | "URGENT"
    """
    gross_pnl        = pnl_data["gross_pnl"]
    pnl_pct          = pnl_data["pnl_pct"]
    profit_pct_of_max = pnl_data["profit_pct_of_max"]
    dte              = pnl_data["dte"]
    total_cost       = pnl_data.get("total_cost", 0)

    reasons = []
    level   = "NORMAL"

    # ── URGENT conditions ─────────────────────────────────────────────────────
    if profit_pct_of_max >= TAKE_PROFIT_MAX_PCT:
        reasons.append(f"Ganancia {profit_pct_of_max*100:.0f}% del máximo — no dejes escapar la ganancia")
        level = "URGENT"

    if pnl_pct <= -(STOP_LOSS_PCT * 100):
        reasons.append(f"Stop loss alcanzado — pérdida {pnl_pct:.1f}%")
        level = "URGENT"

    # ── ACTION conditions ─────────────────────────────────────────────────────
    if level != "URGENT":
        if TAKE_PROFIT_MIN_PCT <= profit_pct_of_max < TAKE_PROFIT_MAX_PCT:
            reasons.append(f"Take profit alcanzado — {profit_pct_of_max*100:.0f}% del máximo")
            level = "ACTION"

        if dte is not None and dte <= MIN_DTE:
            reasons.append(f"Solo {dte} días al vencimiento — Theta acelerando")
            level = "ACTION" if level != "URGENT" else level

    # ── WATCH conditions ──────────────────────────────────────────────────────
    if level == "NORMAL":
        if WATCH_PROFIT_PCT <= profit_pct_of_max < TAKE_PROFIT_MIN_PCT:
            reasons.append(f"Acercándose al objetivo — {profit_pct_of_max*100:.0f}% del máximo")
            level = "WATCH"

        if pnl_pct <= -(STOP_LOSS_WATCH_PCT * 100):
            reasons.append(f"Pérdida creciente — {pnl_pct:.1f}%")
            level = "WATCH"

        if dte is not None and MIN_DTE < dte <= WATCH_DTE:
            reasons.append(f"{dte} días al vencimiento — monitorear de cerca")
            level = "WATCH"

    if not reasons:
        reasons.append("Dentro de rangos normales")

    return level, reasons


def level_icon(level):
    return {
        "NORMAL": "🟢",
        "WATCH":  "🟡",
        "ACTION": "🟠",
        "URGENT": "🔴"
    }.get(level, "—")


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL ALERTS
# ══════════════════════════════════════════════════════════════════════════════

def send_email_alert(position, pnl_data, alert_level, reasons, current_price):
    """
    Send email alert for ACTION and URGENT levels.
    Uses Gmail SMTP with App Password.
    """
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD]):
        print("  ⚠️  Email no configurado — agrega EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD al .env")
        return False

    ticker     = position["ticker"]
    level_text = {"ACTION": "⚠️ ACCIÓN REQUERIDA", "URGENT": "🚨 URGENTE"}[alert_level]

    subject = f"{level_text} — {ticker} Bull Call Spread"

    body = f"""
{level_text}: {ticker}
{'═' * 50}

POSICIÓN:
  Ticker:      {ticker}
  Estrategia:  {position['strategy']}
  Strikes:     ${position['strike_low']} / ${position['strike_high']}
  Contratos:   {position['contracts']}
  Expiración:  {position['expiration']}
  Paper trade: {'Sí' if position['is_paper'] else 'No'}

SITUACIÓN ACTUAL:
  Precio acción:     ${current_price:.2f}
  Precio al entrar:  ${float(position['price_at_open'] or 0):.2f}
  Costo total:       ${float(position['total_cost'] or 0):.2f}
  Ganancia/Pérdida:  ${pnl_data['gross_pnl']:.2f} ({pnl_data['pnl_pct']:.1f}%)
  % del máximo:      {pnl_data['profit_pct_of_max']*100:.1f}%
  Días al venc.:     {pnl_data['dte']} días

ALERTAS:
{chr(10).join(f'  • {r}' for r in reasons)}

ACCIÓN SUGERIDA:
  {'🔴 CIERRA AHORA — stop loss o máximo alcanzado' if alert_level == 'URGENT' else '🟠 Considera cerrar — objetivo de ganancia alcanzado'}

Para cerrar la posición ejecuta:
  python trade.py --close

—
Monitor automático · {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""

    try:
        msg = MIMEMultipart()
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

        print(f"  📧 Email enviado a {EMAIL_TO}")
        return True

    except Exception as e:
        print(f"  ⚠️  Error enviando email: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# CONSOLE REPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_position_report(position, pnl_data, alert_level, reasons, scored=None):
    """Print detailed report for one position."""
    icon   = level_icon(alert_level)
    ticker = position["ticker"]

    print(f"\n{icon} {ticker} — {alert_level}")
    print(f"{'─' * 50}")
    print(f"  Strikes:        ${position['strike_low']} / ${position['strike_high']}")
    print(f"  Expiración:     {position['expiration']} ({pnl_data['dte']} días)")
    print(f"  Costo total:    ${float(position['total_cost'] or 0):.2f}")
    print(f"  Ganancia/Perd:  ${pnl_data['gross_pnl']:.2f} ({pnl_data['pnl_pct']:.1f}%)")
    print(f"  % del máximo:   {pnl_data['profit_pct_of_max']*100:.1f}%")
    print(f"  Ganancia máx:   ${pnl_data['max_profit']:.2f}")

    if scored:
        print(f"  Score actual:   {scored['score']}/{scored['score_max']} "
              f"({scored['score_pct']}%) — {scored['verdict']}")

    print(f"\n  Alertas:")
    for reason in reasons:
        print(f"    • {reason}")


# ══════════════════════════════════════════════════════════════════════════════
# AI INTERPRETATION (optional)
# ══════════════════════════════════════════════════════════════════════════════

def build_monitor_prompt(positions_data, context):
    """Build AI prompt with all positions data."""
    positions_str = ""
    for pd in positions_data:
        pos    = pd["position"]
        pnl    = pd["pnl_data"]
        level  = pd["alert_level"]
        scored = pd.get("scored")

        positions_str += f"\n{'─' * 40}\n"
        positions_str += f"TICKER: {pos['ticker']} | Alert: {level}\n"
        positions_str += f"Strikes: ${pos['strike_low']}/${pos['strike_high']} | "
        positions_str += f"Exp: {pos['expiration']} ({pnl['dte']} días)\n"
        positions_str += f"Costo: ${float(pos['total_cost'] or 0):.2f} | "
        positions_str += f"P&L: ${pnl['gross_pnl']:.2f} ({pnl['pnl_pct']:.1f}%)\n"
        positions_str += f"% del máximo: {pnl['profit_pct_of_max']*100:.1f}%\n"

        if scored:
            positions_str += f"Score actual: {scored['score']}/{scored['score_max']} — {scored['verdict']}\n"
            for criterion, data in scored["criteria_scores"].items():
                positions_str += f"  {criterion}: {data['label']} ({data['score']:+d})\n"

    prompt = f"""Eres un mentor experto en trading de opciones Bull Call Spread.
Tu estudiante tiene estas posiciones abiertas y necesita orientación específica.

CONTEXTO:
  Estrategia: {context['strategy']}
  Capital: ${context['capital']:,}

POSICIONES ABIERTAS:
{positions_str}

Por favor proporciona:

1. EVALUACIÓN DE CADA POSICIÓN — ¿Debe cerrar, mantener o ajustar?
2. PRIORIDADES — ¿Cuál requiere atención inmediata?
3. RIESGOS — ¿Qué podría salir mal si no actúa hoy?
4. RECOMENDACIÓN CONCRETA — Acción específica para cada posición

Sé directo y específico. Máximo 300 palabras.

Responde en {context.get('language', 'English')}.
"""
    return prompt


def get_ai_interpretation(positions_data, context):
    """
    Send all positions to Claude for comparative interpretation.

    Returns:
        tuple: (client, conversation_history, ai_text)
    """
    try:
        client  = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        prompt  = build_monitor_prompt(positions_data, context)

        conversation_history = [{"role": "user", "content": prompt}]

        print("\n⏳ Obteniendo interpretación AI...")

        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=AI_MAX_TOKENS,
            messages=conversation_history
        )

        ai_text = response.content[0].text
        conversation_history.append({"role": "assistant", "content": ai_text})

        return client, conversation_history, ai_text

    except Exception as e:
        return None, [], f"⚠️  AI no disponible: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# MAIN MONITOR RUN
# ══════════════════════════════════════════════════════════════════════════════

def run_monitor(ask_ai=True):
    """
    Run one full monitoring cycle.

    Pipeline:
        DB positions → current price → P&L → alert level
        → email if ACTION/URGENT → optional AI → conversation loop
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'═' * 60}")
    print(f"  MONITOR DE POSICIONES — {timestamp}")
    print(f"{'═' * 60}")

    # ── Load open positions ───────────────────────────────────────────────────
    positions = get_open_positions()

    if not positions:
        print("\n  No hay posiciones abiertas para monitorear.\n")
        return

    print(f"\n  Posiciones abiertas: {len(positions)}")

    positions_data  = []
    email_sent      = set()

    # ── Process each position ─────────────────────────────────────────────────
    for position in positions:
        ticker = position["ticker"]
        print(f"\n⏳ Revisando {ticker}...", end=" ", flush=True)

        try:
            # Get current market data
            criteria = get_all_criteria(ticker)
            if criteria is None:
                print(f"❌ Sin datos")
                continue

            current_price = criteria["price"]

            # Calculate P&L
            pnl_data = calculate_current_pnl(position, current_price)
            pnl_data["total_cost"] = float(position.get("total_cost") or 0)

            # Evaluate alert level
            alert_level, reasons = evaluate_alert_level(pnl_data)

            # Score current criteria
            scored = score_criteria(criteria)
            if scored:
                save_analysis(scored)

            print(f"{level_icon(alert_level)} {alert_level}")

            # Print console report
            print_position_report(position, pnl_data, alert_level, reasons, scored)

            # Send email for ACTION and URGENT
            if alert_level in ("ACTION", "URGENT") and ticker not in email_sent:
                send_email_alert(position, pnl_data, alert_level, reasons, current_price)
                email_sent.add(ticker)

            positions_data.append({
                "position":    position,
                "pnl_data":    pnl_data,
                "alert_level": alert_level,
                "reasons":     reasons,
                "scored":      scored,
            })

        except Exception as e:
            print(f"❌ Error: {e}")
            continue

    if not positions_data:
        print("\n  ❌ No se pudo analizar ninguna posición.\n")
        return

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    urgent = sum(1 for p in positions_data if p["alert_level"] == "URGENT")
    action = sum(1 for p in positions_data if p["alert_level"] == "ACTION")
    watch  = sum(1 for p in positions_data if p["alert_level"] == "WATCH")
    normal = sum(1 for p in positions_data if p["alert_level"] == "NORMAL")

    print(f"  🔴 URGENT: {urgent}  🟠 ACTION: {action}  "
          f"🟡 WATCH: {watch}  🟢 NORMAL: {normal}")
    print(f"{'═' * 60}")

    # ── Optional AI interpretation ────────────────────────────────────────────
    use_ai = False
    if ask_ai:
        use_ai = input("\n¿Quieres análisis AI de tus posiciones? (s/exit): ").strip().lower()
        use_ai = use_ai == "s"

    if use_ai:
        client, conversation_history, ai_text = get_ai_interpretation(
            positions_data, TRADING_CONTEXT
        )

        print(f"\n{'═' * 60}")
        print("  INTERPRETACIÓN AI")
        print(f"{'═' * 60}")
        print(ai_text)
        print(f"{'═' * 60}\n")

        # ── Conversation loop ─────────────────────────────────────────────────
        if client:
            print("💬 Puedes preguntarme sobre tus posiciones. Escribe 'exit' para terminar.\n")

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

    print(f"\n✅ Monitor completado — {timestamp}\n")


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULER — background loop
# ══════════════════════════════════════════════════════════════════════════════

def scheduled_run():
    """
    Run monitor automatically — no AI prompt in scheduled mode.
    Emails are still sent for ACTION/URGENT alerts.
    """
    if not is_market_open():
        print(f"[{datetime.now().strftime('%H:%M')}] Mercado cerrado — esperando...")
        return

    run_monitor(ask_ai=False)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Monitor de posiciones Bull Call Spread"
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Correr en background cada X minutos durante horario de mercado"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Intervalo en minutos para el loop (default: 30)"
    )
    args = parser.parse_args()

    if args.loop:
        print(f"\n🔄 Monitor en background — cada {args.interval} minutos")
        print(f"   Horario: {MARKET_OPEN_HOUR}:{MARKET_OPEN_MIN:02d} - "
              f"{MARKET_CLOSE_HOUR}:{MARKET_CLOSE_MIN:02d} ET")
        print(f"   Presiona Ctrl+C para detener\n")

        # Schedule recurring run
        schedule.every(args.interval).minutes.do(scheduled_run)

        # Run immediately on start
        scheduled_run()

        while True:
            schedule.run_pending()
            time.sleep(60)
    else:
        # Single run with AI option
        run_monitor(ask_ai=True)