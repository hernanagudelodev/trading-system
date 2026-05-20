"""
monitor.py
==========
Position monitoring script for Bull Call Spread analysis system.

Improvements vs previous version:
    - ntfy.sh push notifications (replaces broken email)
    - Adaptive frequency: 5min during market, 30min outside
    - Generates monitor_report.html for mobile viewing

Alert levels:
    🟢 NORMAL  → within expected ranges       → console + HTML
    🟡 WATCH   → approaching targets          → console + HTML + ntfy
    🟠 ACTION  → take profit reached          → console + HTML + ntfy
    🔴 URGENT  → stop loss / max profit       → console + HTML + ntfy

Usage:
    python monitor.py              → run once
    python monitor.py --loop       → run in adaptive loop

Dependencies:
    criteria.py  → current market data
    scoring.py   → current score
    db.py        → positions
    .env         → DATABASE_URL, NTFY_TOPIC
"""

import os
import time
import argparse
import schedule
import requests
from datetime import datetime, date

import anthropic
from dotenv import load_dotenv

from criteria import get_all_criteria
from scoring import score_criteria
from db import get_open_positions, save_analysis

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

TAKE_PROFIT_MIN_PCT  = 0.50
TAKE_PROFIT_MAX_PCT  = 0.70
STOP_LOSS_PCT        = 0.50
STOP_LOSS_WATCH_PCT  = 0.25
WATCH_PROFIT_PCT     = 0.30
MIN_DTE              = 7
WATCH_DTE            = 10

MARKET_OPEN_HOUR     = 9
MARKET_OPEN_MIN      = 30
MARKET_CLOSE_HOUR    = 16
MARKET_CLOSE_MIN     = 0

# Intervals
INTERVAL_MARKET_OPEN    = 5   # minutes — during market hours
INTERVAL_PRE_MARKET     = 10  # minutes — 9:00-9:30 ET
INTERVAL_MARKET_CLOSED  = 30  # minutes — outside market hours

# ntfy.sh configuration — set NTFY_TOPIC in .env
# Example: NTFY_TOPIC=mi-monitor-secreto-hal123
NTFY_TOPIC    = os.getenv("NTFY_TOPIC", "")
NTFY_BASE_URL = "https://ntfy.sh"

# Heartbeat — ntfy status every 60 min during market hours
HEARTBEAT_INTERVAL_MIN = 60

# Internal state (module-level, persists across scheduled_run calls)
_last_heartbeat_time = None
_market_close_sent   = False

# Report path
REPORT_PATH = os.path.join(os.path.dirname(__file__), "monitor_report.html")

# AI config
AI_MODEL      = "claude-opus-4-5"
AI_MAX_TOKENS = 1000

TRADING_CONTEXT = {
    "strategy": "Bull Call Spread",
    "capital":  15000,
    "language": "Spanish",
}


# ══════════════════════════════════════════════════════════════════════════════
# MARKET HOURS
# ══════════════════════════════════════════════════════════════════════════════

def get_market_status():
    """
    Returns: "open" | "pre" | "closed"
    Uses ET timezone explicitly — works on Railway (UTC).
    """
    from datetime import timezone, timedelta
    et_offset = timedelta(hours=-4)  # EDT (summer), use -5 for EST
    et_now    = datetime.now(timezone.utc) + et_offset

    hour    = et_now.hour
    minute  = et_now.minute
    weekday = et_now.weekday()

    if weekday >= 5:
        return "closed"

    market_open_mins  = MARKET_OPEN_HOUR  * 60 + MARKET_OPEN_MIN
    market_close_mins = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    current_mins      = hour * 60 + minute
    pre_market_mins   = (MARKET_OPEN_HOUR - 1) * 60  # 1 hour before open

    if current_mins >= market_open_mins and current_mins < market_close_mins:
        return "open"
    elif current_mins >= pre_market_mins and current_mins < market_open_mins:
        return "pre"
    return "closed"

def is_market_open():
    return get_market_status() == "open"

def get_interval():
    """Return appropriate polling interval based on market status."""
    status = get_market_status()
    return {
        "open":   INTERVAL_MARKET_OPEN,
        "pre":    INTERVAL_PRE_MARKET,
        "closed": INTERVAL_MARKET_CLOSED,
    }.get(status, INTERVAL_MARKET_CLOSED)


# ══════════════════════════════════════════════════════════════════════════════
# NTFY NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def send_ntfy(title, message, priority="default", tags=None):
    """
    Send push notification via ntfy.sh.
    Free, no account needed — install ntfy app on Android/iOS.

    Priority: max | urgent | high | default | low | min
    Tags: list of emoji names e.g. ["warning", "chart_increasing"]
    """
    if not NTFY_TOPIC:
        print("  ⚠️  NTFY_TOPIC no configurado — agrega NTFY_TOPIC al .env")
        return False

    headers = {
        "Title":    title,
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    try:
        resp = requests.post(
            f"{NTFY_BASE_URL}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"  📲 Notificación enviada → ntfy/{NTFY_TOPIC}")
            return True
        else:
            print(f"  ⚠️  ntfy error: {resp.status_code}")
            return False
    except Exception as e:
        print(f"  ⚠️  ntfy error: {e}")
        return False


def send_alert_notification(position, pnl_data, alert_level, reasons):
    """Send formatted ntfy notification for ACTION/URGENT/WATCH alerts."""
    ticker = position["ticker"]
    pnl    = pnl_data["gross_pnl"]
    pct    = pnl_data["profit_pct_of_max"] * 100
    dte    = pnl_data["dte"]

    if alert_level == "URGENT":
        title    = f"🔴 URGENTE — {ticker}"
        priority = "urgent"
        tags     = ["rotating_light", "chart_increasing"]
    elif alert_level == "ACTION":
        title    = f"🟠 ACCIÓN — {ticker}"
        priority = "high"
        tags     = ["warning", "moneybag"]
    else:  # WATCH
        title    = f"🟡 WATCH — {ticker}"
        priority = "default"
        tags     = ["eyes"]

    reason_text = "\n".join(f"• {r}" for r in reasons)
    message = (
        f"${ticker} | P&L: ${pnl:+.0f} ({pct:.0f}% del máx) | {dte}d\n"
        f"{reason_text}"
    )

    send_ntfy(title, message, priority=priority, tags=tags)


# ══════════════════════════════════════════════════════════════════════════════
# P&L CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

def calculate_current_pnl(position, current_price):
    strike_low  = float(position["strike_low"])
    strike_high = float(position["strike_high"])
    contracts   = int(position["contracts"])
    total_cost  = float(position.get("total_cost") or 0)
    expiration  = position["expiration"]

    spread_width = strike_high - strike_low
    premium_per_share = total_cost / (contracts * 100) if contracts > 0 else 0
    max_profit = (spread_width - premium_per_share) * contracts * 100

    if current_price >= strike_high:
        spread_value = spread_width
    elif current_price <= strike_low:
        spread_value = 0
    else:
        spread_value = current_price - strike_low

    current_value = spread_value * contracts * 100
    gross_pnl     = current_value - total_cost
    pnl_pct       = (gross_pnl / total_cost * 100) if total_cost > 0 else 0
    profit_pct_of_max = (gross_pnl / max_profit) if max_profit > 0 else 0

    exp_date = datetime.strptime(str(expiration), "%Y-%m-%d").date()
    dte      = (exp_date - date.today()).days

    return {
        "current_price":      current_price,
        "current_value":      current_value,
        "gross_pnl":          gross_pnl,
        "pnl_pct":            pnl_pct,
        "profit_pct_of_max":  profit_pct_of_max,
        "max_profit":         max_profit,
        "dte":                dte,
        "spread_value":       spread_value,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ALERT EVALUATION
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_alert_level(pnl_data):
    profit_pct_of_max = pnl_data["profit_pct_of_max"]
    pnl_pct           = pnl_data["pnl_pct"]
    dte               = pnl_data["dte"]
    reasons           = []
    level             = "NORMAL"

    if profit_pct_of_max >= TAKE_PROFIT_MAX_PCT:
        reasons.append(f"Ganancia {profit_pct_of_max*100:.0f}% del máximo — no dejes escapar")
        level = "URGENT"

    if pnl_pct <= -(STOP_LOSS_PCT * 100):
        reasons.append(f"Stop loss alcanzado — pérdida {pnl_pct:.1f}%")
        level = "URGENT"

    if level != "URGENT":
        if TAKE_PROFIT_MIN_PCT <= profit_pct_of_max < TAKE_PROFIT_MAX_PCT:
            reasons.append(f"Take profit alcanzado — {profit_pct_of_max*100:.0f}% del máximo")
            level = "ACTION"

        if dte is not None and dte <= MIN_DTE:
            reasons.append(f"Solo {dte} días al vencimiento — Theta acelerando")
            level = "ACTION" if level != "URGENT" else level

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
    return {"NORMAL": "🟢", "WATCH": "🟡", "ACTION": "🟠", "URGENT": "🔴"}.get(level, "—")


# ══════════════════════════════════════════════════════════════════════════════
# HTML REPORT
# ══════════════════════════════════════════════════════════════════════════════

def generate_html_report(positions_data, timestamp):
    """
    Generate monitor_report.html — mobile-friendly dashboard.
    Saved to scripts/monitor_report.html.
    """
    level_colors = {
        "URGENT": "#ef4444",
        "ACTION": "#f97316",
        "WATCH":  "#f59e0b",
        "NORMAL": "#22c55e",
    }

    cards = ""
    for pd in positions_data:
        pos   = pd["position"]
        pnl   = pd["pnl_data"]
        level = pd["alert_level"]
        reasons = pd["reasons"]
        color = level_colors.get(level, "#6b7280")
        icon  = level_icon(level)

        pnl_color  = "#22c55e" if pnl["gross_pnl"] >= 0 else "#ef4444"
        pct_of_max = pnl["profit_pct_of_max"] * 100
        bar_width  = min(100, max(0, pct_of_max))
        bar_color  = "#22c55e" if pct_of_max >= 50 else "#f59e0b" if pct_of_max >= 30 else "#6b7280"

        reasons_html = "".join(
            f'<div style="display:flex;gap:8px;margin:4px 0;">'
            f'<span style="color:{color};">›</span>'
            f'<span style="color:#cbd5e1;font-size:13px;">{r}</span></div>'
            for r in reasons
        )

        cards += f"""
        <div style="background:#111;border:2px solid {color}44;border-radius:16px;
                    padding:20px;margin-bottom:16px;">
            <div style="display:flex;justify-content:space-between;
                        align-items:center;margin-bottom:14px;">
                <div>
                    <span style="font-size:28px;font-weight:900;color:#f9fafb;">
                        {pos['ticker']}
                    </span>
                    <span style="font-size:13px;color:#6b7280;margin-left:8px;">
                        ${pos['strike_low']}/{pos['strike_high']} · {pnl['dte']}d
                    </span>
                </div>
                <div style="background:{color}22;border:1px solid {color};
                            border-radius:8px;padding:6px 14px;">
                    <span style="color:{color};font-weight:700;font-size:14px;">
                        {icon} {level}
                    </span>
                </div>
            </div>

            <div style="display:grid;grid-template-columns:1fr 1fr;
                        gap:12px;margin-bottom:14px;">
                <div style="background:#1a1a1a;border-radius:10px;padding:12px;">
                    <div style="font-size:10px;color:#6b7280;letter-spacing:2px;
                                text-transform:uppercase;margin-bottom:4px;">P&L</div>
                    <div style="font-size:22px;font-weight:900;color:{pnl_color};">
                        ${pnl['gross_pnl']:+.0f}
                    </div>
                    <div style="font-size:12px;color:{pnl_color};">
                        {pnl['pnl_pct']:+.1f}%
                    </div>
                </div>
                <div style="background:#1a1a1a;border-radius:10px;padding:12px;">
                    <div style="font-size:10px;color:#6b7280;letter-spacing:2px;
                                text-transform:uppercase;margin-bottom:4px;">DEL MÁXIMO</div>
                    <div style="font-size:22px;font-weight:900;color:{bar_color};">
                        {pct_of_max:.0f}%
                    </div>
                    <div style="font-size:12px;color:#6b7280;">
                        máx: ${pnl['max_profit']:.0f}
                    </div>
                </div>
            </div>

            <div style="background:#1a1a1a;border-radius:8px;
                        height:8px;margin-bottom:14px;">
                <div style="background:{bar_color};width:{bar_width:.1f}%;
                            height:8px;border-radius:8px;transition:width 0.5s;"></div>
            </div>

            <div style="background:#0d1117;border-radius:10px;padding:12px;">
                {reasons_html}
            </div>

            <div style="margin-top:10px;display:flex;justify-content:space-between;
                        font-size:11px;color:#4b5563;font-family:monospace;">
                <span>Precio actual: ${pnl['current_price']:.2f}</span>
                <span>Costo: ${float(pos.get('total_cost') or 0):.0f}</span>
                <span>Exp: {pos['expiration']}</span>
            </div>
        </div>"""

    market_status = get_market_status()
    market_color  = "#22c55e" if market_status == "open" else \
                    "#f59e0b" if market_status == "pre"  else "#6b7280"
    market_label  = {"open": "ABIERTO", "pre": "PRE-MARKET", "closed": "CERRADO"}.get(market_status)
    next_interval = get_interval()

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
    <meta http-equiv="refresh" content="300">
    <title>Monitor — {timestamp}</title>
    <style>
        * {{ box-sizing:border-box; margin:0; padding:0; }}
        body {{
            background:#0a0a0a; color:#f9fafb;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            padding:16px; max-width:480px; margin:0 auto;
        }}
    </style>
</head>
<body>
    <div style="display:flex;justify-content:space-between;align-items:center;
                margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid #1e1e1e;">
        <div>
            <div style="font-size:18px;font-weight:900;color:#f9fafb;">
                📊 Monitor de Posiciones
            </div>
            <div style="font-size:12px;color:#6b7280;font-family:monospace;margin-top:2px;">
                {timestamp}
            </div>
        </div>
        <div style="background:{market_color}22;border:1px solid {market_color};
                    border-radius:8px;padding:6px 12px;text-align:center;">
            <div style="color:{market_color};font-size:11px;font-weight:700;
                        letter-spacing:1px;">{market_label}</div>
            <div style="color:{market_color}88;font-size:10px;">
                cada {next_interval}min
            </div>
        </div>
    </div>

    {cards if cards else
        '<div style="text-align:center;padding:40px;color:#6b7280;">No hay posiciones abiertas</div>'}

    <div style="text-align:center;color:#374151;font-size:11px;
                padding:16px 0;margin-top:8px;font-family:monospace;">
        Se actualiza cada {next_interval} min · Auto-refresh cada 5 min
    </div>
</body>
</html>"""

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)


# ══════════════════════════════════════════════════════════════════════════════
# CONSOLE REPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_position_report(position, pnl_data, alert_level, reasons, scored=None):
    icon   = level_icon(alert_level)
    ticker = position["ticker"]

    print(f"\n{icon} {ticker} — {alert_level}")
    print(f"{'─' * 50}")
    print(f"  Strikes:        ${position['strike_low']} / ${position['strike_high']}")
    print(f"  Expiración:     {position['expiration']} ({pnl_data['dte']} días)")
    print(f"  Costo total:    ${float(position.get('total_cost') or 0):.2f}")
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
# MAIN MONITOR RUN
# ══════════════════════════════════════════════════════════════════════════════

def run_monitor(ask_ai=False):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'═' * 60}")
    print(f"  MONITOR DE POSICIONES — {timestamp}")

    market_status = get_market_status()
    status_label  = {"open": "ABIERTO", "pre": "PRE-MARKET", "closed": "CERRADO"}
    print(f"  Mercado: {status_label.get(market_status, '?')} | "
          f"Intervalo: {get_interval()}min")

    alerts = []
    print(f"\n  Alertas:")
    print(f"{'═' * 60}")

    positions = get_open_positions()

    if not positions:
        print("\n  No hay posiciones abiertas.\n")
        generate_html_report([], timestamp)
        return

    print(f"\n  Posiciones abiertas: {len(positions)}")

    positions_data = []
    ntfy_sent      = set()

    for position in positions:
        ticker = position["ticker"]
        print(f"\n⏳ Revisando {ticker}...", end=" ", flush=True)

        try:
            criteria = get_all_criteria(ticker)
            if criteria is None:
                print("❌ Sin datos")
                continue

            current_price = criteria["price"]
            pnl_data      = calculate_current_pnl(position, current_price)
            pnl_data["total_cost"] = float(position.get("total_cost") or 0)
            alert_level, reasons   = evaluate_alert_level(pnl_data)

            scored = score_criteria(criteria)
            if scored:
                save_analysis(scored)

            print(f"{level_icon(alert_level)} {alert_level}")
            print_position_report(position, pnl_data, alert_level, reasons, scored)

            # Send ntfy notification for WATCH/ACTION/URGENT
            if alert_level in ("WATCH", "ACTION", "URGENT") and ticker not in ntfy_sent:
                send_alert_notification(position, pnl_data, alert_level, reasons)
                ntfy_sent.add(ticker)

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

    # Generate HTML report (always, even with no alerts)
    generate_html_report(positions_data, timestamp)

    # Summary
    print(f"\n{'═' * 60}")
    urgent = sum(1 for p in positions_data if p["alert_level"] == "URGENT")
    action = sum(1 for p in positions_data if p["alert_level"] == "ACTION")
    watch  = sum(1 for p in positions_data if p["alert_level"] == "WATCH")
    normal = sum(1 for p in positions_data if p["alert_level"] == "NORMAL")
    print(f"  🔴 URGENT: {urgent}  🟠 ACTION: {action}  "
          f"🟡 WATCH: {watch}  🟢 NORMAL: {normal}")
    print(f"{'═' * 60}")
    print(f"✅ Monitor completado — {timestamp}\n")

    # Heartbeat — hourly status during market hours
    if should_send_heartbeat():
        send_heartbeat(positions_data, timestamp)

    # Market close summary — once per day when market closes
    global _market_close_sent
    if get_market_status() == "closed" and not _market_close_sent:
        send_market_close_summary(positions_data, timestamp)
    elif get_market_status() == "open":
        _market_close_sent = False  # reset flag for next trading day



# ══════════════════════════════════════════════════════════════════════════════
# HEARTBEAT & MARKET CLOSE NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def should_send_heartbeat():
    """Return True if it's time to send an hourly status notification."""
    global _last_heartbeat_time
    if not is_market_open():
        return False
    now = datetime.now()
    if _last_heartbeat_time is None:
        return True
    elapsed = (now - _last_heartbeat_time).total_seconds() / 60
    return elapsed >= HEARTBEAT_INTERVAL_MIN


def send_heartbeat(positions_data, timestamp):
    """Send hourly status notification — all clear or summary of positions."""
    global _last_heartbeat_time
    _last_heartbeat_time = datetime.now()

    if not positions_data:
        send_ntfy(
            title="📊 Monitor OK — Sin posiciones",
            message=f"Todo en orden. Sin posiciones abiertas.\n{timestamp}",
            priority="low",
            tags=["white_check_mark"],
        )
        return

    lines = []
    for pd in positions_data:
        pos   = pd["position"]
        pnl   = pd["pnl_data"]
        level = pd["alert_level"]
        icon  = level_icon(level)
        pct   = pnl["profit_pct_of_max"] * 100
        lines.append(
            f"{icon} {pos['ticker']} | P&L: ${pnl['gross_pnl']:+.0f} "
            f"({pct:.0f}% máx) | {pnl['dte']}d"
        )

    send_ntfy(
        title=f"📊 Monitor OK — {len(positions_data)} posición(es)",
        message="\n".join(lines) + f"\n{timestamp}",
        priority="low",
        tags=["white_check_mark", "chart_increasing"],
    )


def send_market_close_summary(positions_data, timestamp):
    """Send one notification when market closes with end-of-day summary."""
    global _market_close_sent
    if _market_close_sent:
        return

    _market_close_sent = True

    if not positions_data:
        send_ntfy(
            title="🔔 Mercado cerrado — Sin posiciones",
            message=f"Mercado cerrado. Sin posiciones abiertas.\n{timestamp}",
            priority="low",
            tags=["bell"],
        )
        return

    lines = []
    for pd in positions_data:
        pos   = pd["position"]
        pnl   = pd["pnl_data"]
        level = pd["alert_level"]
        icon  = level_icon(level)
        pct   = pnl["profit_pct_of_max"] * 100
        lines.append(
            f"{icon} {pos['ticker']} ${pnl['gross_pnl']:+.0f} "
            f"({pct:.0f}% máx) | {pnl['dte']}d al venc."
        )

    send_ntfy(
        title=f"🔔 Cierre de mercado — {len(positions_data)} posición(es)",
        message="\n".join(lines) + f"\n{timestamp}",
        priority="default",
        tags=["bell", "chart_increasing"],
    )

# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULED RUN (Railway)
# ══════════════════════════════════════════════════════════════════════════════

def scheduled_run():
    run_monitor(ask_ai=False)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor de posiciones")
    parser.add_argument("--loop", action="store_true",
                        help="Correr en loop adaptativo")
    args = parser.parse_args()

    if args.loop:
        print(f"\n🔄 Monitor en loop adaptativo")
        print(f"   Mercado abierto:  cada {INTERVAL_MARKET_OPEN}min")
        print(f"   Pre-market:       cada {INTERVAL_PRE_MARKET}min")
        print(f"   Mercado cerrado:  cada {INTERVAL_MARKET_CLOSED}min\n")
        scheduled_run()
        current_interval = get_interval()
        schedule.every(current_interval).minutes.do(scheduled_run)

        while True:
            schedule.run_pending()
            new_interval = get_interval()
            if new_interval != current_interval:
                schedule.clear()
                schedule.every(new_interval).minutes.do(scheduled_run)
                current_interval = new_interval
                print(f"  ⏱  Intervalo ajustado → {current_interval}min")
            time.sleep(60)
    else:
        run_monitor(ask_ai=True)