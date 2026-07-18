"""
monitor.py
==========
Position monitoring script — multi-strategy support.

Strategies supported:
    Bull Call Spread    → spread width P&L
    Bear Put Spread     → spread width P&L
    Long Call           → delta-based P&L with yfinance live delta
    Long Put            → delta-based P&L with yfinance live delta
    Cash Secured Put    → premium decay P&L
    Covered Call        → premium decay P&L
    Long Straddle       → combined call+put P&L

Alert levels:
    NORMAL  → within expected ranges       → console + HTML
    WATCH   → approaching targets          → console + HTML + ntfy
    ACTION  → take profit reached          → console + HTML + ntfy
    URGENT  → stop loss / max profit       → console + HTML + ntfy

Usage:
    python monitor.py              → run once
    python monitor.py --loop       → run in adaptive loop

Dependencies:
    criteria.py  → current market data
    db.py        → positions
    .env         → DATABASE_URL, NTFY_TOPIC
"""

import os
import time
import asyncio
import argparse
import schedule
import yfinance as yf
from datetime import datetime, date

from dotenv import load_dotenv

from notify import send_push

from criteria import get_all_criteria
from db import get_open_positions

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

TAKE_PROFIT_MIN_PCT  = 0.50   # 50% of max profit → ACTION
TAKE_PROFIT_MAX_PCT  = 0.70   # 70% of max profit → URGENT
WATCH_PROFIT_PCT     = 0.30   # 30% of max → WATCH
MIN_DTE              = 7      # days → ACTION
WATCH_DTE            = 10     # days → WATCH

# Stop loss escalonado por DTE — más espacio cuando queda más tiempo
# >15 DTE: -65%  |  8-15 DTE: -55%  |  <8 DTE: -50%
STOP_LOSS_PCT_HIGH_DTE = 0.65   # >15 DTE
STOP_LOSS_PCT_MID_DTE  = 0.55   # 8-15 DTE
STOP_LOSS_PCT_LOW_DTE  = 0.50   # <8 DTE
STOP_LOSS_WATCH_PCT    = 0.30   # 30% loss → WATCH


def get_stop_loss_pct(dte):
    """Returns stop loss threshold based on DTE."""
    if dte is None or dte > 15:
        return STOP_LOSS_PCT_HIGH_DTE
    elif dte >= 8:
        return STOP_LOSS_PCT_MID_DTE
    else:
        return STOP_LOSS_PCT_LOW_DTE

MARKET_OPEN_HOUR     = 9
MARKET_OPEN_MIN      = 30
MARKET_CLOSE_HOUR    = 16
MARKET_CLOSE_MIN     = 0

INTERVAL_MARKET_OPEN    = 5
INTERVAL_PRE_MARKET     = 10
INTERVAL_MARKET_CLOSED  = 30


HEARTBEAT_INTERVAL_MIN = 60

_last_heartbeat_time = None
_market_close_sent   = False

REPORT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "reports", "monitor_report.html")

# Strategy type classification
DEBIT_SPREADS   = {"Bull Call Spread", "Bear Put Spread"}
LONG_OPTIONS    = {"Long Call", "Long Put"}
CREDIT_OPTIONS  = {"Cash Secured Put", "Covered Call"}
STRADDLES       = {"Long Straddle"}


# ══════════════════════════════════════════════════════════════════════════════
# MARKET HOURS
# ══════════════════════════════════════════════════════════════════════════════

def get_market_status():
    """Returns: 'open' | 'pre' | 'closed'. Uses ET timezone."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except ImportError:
        import pytz
        now = datetime.now(pytz.timezone("America/New_York"))

    if now.weekday() >= 5:
        return "closed"

    t = now.hour * 60 + now.minute
    open_t  = MARKET_OPEN_HOUR  * 60 + MARKET_OPEN_MIN
    close_t = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    pre_t   = (MARKET_OPEN_HOUR - 1) * 60 + MARKET_OPEN_MIN

    if open_t <= t < close_t:
        return "open"
    elif pre_t <= t < open_t:
        return "pre"
    return "closed"


def is_market_open():
    return get_market_status() == "open"


def get_interval():
    status = get_market_status()
    if status == "open":
        return INTERVAL_MARKET_OPEN
    elif status == "pre":
        return INTERVAL_PRE_MARKET
    return INTERVAL_MARKET_CLOSED


# ══════════════════════════════════════════════════════════════════════════════
# OPTION DELTA — live from yfinance
# ══════════════════════════════════════════════════════════════════════════════

def get_live_delta(ticker, strike, expiration, option_type="call"):
    """
    Fetch live delta for a specific option from yfinance.
    Returns delta as float, or fallback value if unavailable.
    """
    fallback = 0.45 if option_type == "call" else -0.45
    try:
        tk = yf.Ticker(ticker)

        available = tk.options
        if not available:
            return fallback

        target = expiration
        closest = min(
            available,
            key=lambda e: abs((datetime.strptime(e, "%Y-%m-%d").date() - target).days)
        )

        chain = tk.option_chain(closest)
        df = chain.calls if option_type == "call" else chain.puts

        if df is None or df.empty:
            return fallback

        df = df.copy()
        df["strike_diff"] = (df["strike"] - strike).abs()
        row = df.sort_values("strike_diff").iloc[0]

        delta = row.get("delta", None)
        if delta is None or (hasattr(delta, "__class__") and delta != delta):
            return fallback

        return float(delta)

    except Exception:
        return fallback


def get_live_option_price(ticker, strike, expiration, option_type="call"):
    """
    Fetch live mid price for a specific option from yfinance.
    Returns mid price as float, or None if unavailable.
    """
    try:
        tk = yf.Ticker(ticker)

        available = tk.options
        if not available:
            return None

        closest = min(
            available,
            key=lambda e: abs((datetime.strptime(e, "%Y-%m-%d").date() - expiration).days)
        )

        chain = tk.option_chain(closest)
        df = chain.calls if option_type == "call" else chain.puts

        if df is None or df.empty:
            return None

        df = df.copy()
        df["strike_diff"] = (df["strike"] - strike).abs()
        row = df.sort_values("strike_diff").iloc[0]

        bid = row.get("bid", 0) or 0
        ask = row.get("ask", 0) or 0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        last = row.get("lastPrice", None)
        return float(last) if last else None

    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SPREAD VALUE — delega en pricing.py (fuente única)
# ══════════════════════════════════════════════════════════════════════════════

def get_spread_value_tastytrade(ticker, strike_low, strike_high, expiration,
                                option_type="call", session=None):
    """Delegado a pricing.get_spread_value — fuente única de pricing de spreads.
    El parámetro 'session' se ignora (compatibilidad de firma)."""
    import pricing
    return pricing.get_spread_value(ticker, strike_low, strike_high, expiration,
                                    option_type=option_type)


# ══════════════════════════════════════════════════════════════════════════════
# P&L CALCULATION — strategy-aware
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_alert_level(pnl_data):
    profit_pct_of_max = pnl_data["profit_pct_of_max"]
    pnl_pct           = pnl_data["pnl_pct"]
    dte               = pnl_data["dte"]
    strategy_type     = pnl_data.get("strategy_type", "debit_spread")
    reasons           = []
    level             = "NORMAL"

    # Stop loss threshold depends on DTE — more room when more time remains
    stop_loss_pct = get_stop_loss_pct(dte)

    # ── URGENT conditions ────────────────────────────────────────────────────
    if profit_pct_of_max >= TAKE_PROFIT_MAX_PCT:
        reasons.append(f"Ganancia {profit_pct_of_max*100:.0f}% del maximo — no dejes escapar")
        level = "URGENT"

    if pnl_pct <= -(stop_loss_pct * 100):
        reasons.append(
            f"Stop loss alcanzado — perdida {pnl_pct:.1f}% "
            f"(umbral {stop_loss_pct*100:.0f}% con {dte}d restantes)"
        )
        level = "URGENT"

    # ── ACTION conditions ────────────────────────────────────────────────────
    if level != "URGENT":
        if TAKE_PROFIT_MIN_PCT <= profit_pct_of_max < TAKE_PROFIT_MAX_PCT:
            reasons.append(f"Take profit alcanzado — {profit_pct_of_max*100:.0f}% del maximo")
            level = "ACTION"

        if dte is not None and dte <= MIN_DTE:
            reasons.append(f"Solo {dte} dias al vencimiento — Theta acelerando")
            level = "ACTION"

    # ── WATCH conditions ─────────────────────────────────────────────────────
    if level == "NORMAL":
        if WATCH_PROFIT_PCT <= profit_pct_of_max < TAKE_PROFIT_MIN_PCT:
            reasons.append(f"Acercandose al objetivo — {profit_pct_of_max*100:.0f}% del maximo")
            level = "WATCH"

        if pnl_pct <= -(STOP_LOSS_WATCH_PCT * 100):
            reasons.append(f"Perdida creciente — {pnl_pct:.1f}%")
            level = "WATCH"

        if dte is not None and MIN_DTE < dte <= WATCH_DTE:
            reasons.append(f"{dte} dias al vencimiento — monitorear de cerca")
            level = "WATCH"

    if not reasons:
        reasons.append("Dentro de rangos normales")

    return level, reasons


def level_icon(level):
    return {"NORMAL": "[OK]", "WATCH": "[!!]", "ACTION": "[ACT]", "URGENT": "[URG]"}.get(level, "---")


def level_icon_emoji(level):
    return {"NORMAL": "verde", "WATCH": "amarillo", "ACTION": "naranja", "URGENT": "rojo"}.get(level, "---")



def send_alert_notification(position, pnl_data, alert_level, reasons):
    ticker   = position["ticker"]
    strategy = position.get("strategy", "")
    pnl      = pnl_data["gross_pnl"]
    pct      = pnl_data["profit_pct_of_max"] * 100
    dte      = pnl_data["dte"]

    level_titles = {
        "URGENT": f"URGENTE — {ticker}",
        "ACTION": f"ACCION — {ticker}",
        "WATCH":  f"WATCH — {ticker}",
    }
    priorities = {"URGENT": "urgent", "ACTION": "high", "WATCH": "default"}

    title       = level_titles.get(alert_level, ticker)
    reason_text = "\n".join(f"- {r}" for r in reasons)
    message     = (
        f"{strategy} | ${ticker}\n"
        f"P&L: ${pnl:+.0f} ({pct:.0f}% del max) | {dte}d\n"
        f"{reason_text}"
    )

    send_push(title, message, priority=priorities.get(alert_level, "default"))


# ══════════════════════════════════════════════════════════════════════════════
# CONSOLE REPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_position_report(position, pnl_data, alert_level, reasons):
    icon     = level_icon(alert_level)
    ticker   = position["ticker"]
    strategy = position.get("strategy", "")

    print(f"\n{icon} {ticker} — {strategy} — {alert_level}")
    print(f"{'─' * 55}")
    print(f"  Strike(s):      ${position['strike_low']} / ${position['strike_high']}")
    print(f"  Expiracion:     {position['expiration']} ({pnl_data['dte']} dias)")
    print(f"  Precio accion:  ${pnl_data['current_price']:.2f}")

    if pnl_data.get("delta") is not None:
        print(f"  Delta actual:   {pnl_data['delta']:.3f}")

    print(f"  Costo total:    ${float(position.get('total_cost') or 0):.2f}")
    print(f"  Ganancia/Perd:  ${pnl_data['gross_pnl']:.2f} ({pnl_data['pnl_pct']:.1f}%)")
    print(f"  % del maximo:   {pnl_data['profit_pct_of_max']*100:.1f}%")
    print(f"  Ganancia max:   ${pnl_data['max_profit']:.2f}")

    print(f"\n  Alertas:")
    for reason in reasons:
        print(f"    - {reason}")


# ══════════════════════════════════════════════════════════════════════════════
# HTML REPORT
# ══════════════════════════════════════════════════════════════════════════════

def generate_html_report(positions_data, timestamp):
    """Generate monitor_report.html — mobile-friendly dashboard."""

    next_interval = get_interval()

    if not positions_data:
        cards_html = """
        <div style="text-align:center;padding:60px 20px;color:#6b7280;">
            <div style="font-size:48px;margin-bottom:16px;">📭</div>
            <div style="font-size:18px;font-weight:600;color:#9ca3af;">Sin posiciones abiertas</div>
            <div style="font-size:13px;margin-top:8px;">El scanner buscara oportunidades automaticamente</div>
        </div>"""
    else:
        cards = []
        for pd in positions_data:
            pos      = pd["position"]
            pnl      = pd["pnl_data"]
            level    = pd["alert_level"]
            reasons  = pd["reasons"]
            strategy = pos.get("strategy", "")

            level_colors = {
                "NORMAL": "#22c55e",
                "WATCH":  "#eab308",
                "ACTION": "#f97316",
                "URGENT": "#ef4444",
            }
            bar_color = level_colors.get(level, "#6b7280")
            pnl_color = "#22c55e" if pnl["gross_pnl"] >= 0 else "#ef4444"

            pct_of_max = pnl["profit_pct_of_max"] * 100
            bar_width  = max(0, min(100, pct_of_max))

            reasons_html = "".join(
                f'<div style="color:#9ca3af;font-size:12px;padding:3px 0;">'
                f'  {r}</div>'
                for r in reasons
            )

            delta_html = ""
            if pnl.get("delta") is not None:
                delta_html = f'<span>Delta: {pnl["delta"]:.3f}</span>'

            cards.append(f"""
            <div style="background:#111827;border-radius:16px;padding:16px;
                        margin-bottom:16px;border-left:4px solid {bar_color};">
                <div style="display:flex;justify-content:space-between;
                            align-items:center;margin-bottom:12px;">
                    <div>
                        <span style="font-size:22px;font-weight:900;color:#f9fafb;">
                            {pos['ticker']}
                        </span>
                        <span style="font-size:12px;color:#6b7280;margin-left:8px;">
                            {strategy}
                        </span>
                    </div>
                    <div style="background:{bar_color};color:white;padding:4px 10px;
                                border-radius:8px;font-size:12px;font-weight:700;">
                        {level}
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
                                    text-transform:uppercase;margin-bottom:4px;">DEL MAXIMO</div>
                        <div style="font-size:22px;font-weight:900;color:{bar_color};">
                            {pct_of_max:.0f}%
                        </div>
                        <div style="font-size:12px;color:#6b7280;">
                            max: ${pnl['max_profit']:.0f}
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
                    <span>Precio: ${pnl['current_price']:.2f}</span>
                    {delta_html}
                    <span>Costo: ${float(pos.get('total_cost') or 0):.0f}</span>
                    <span>{pnl['dte']}d restantes</span>
                </div>
            </div>""")

        cards_html = "\n".join(cards)

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="300">
    <title>Monitor — {timestamp}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: #0d1117;
            color: #f9fafb;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            max-width: 480px;
            margin: 0 auto;
            padding: 16px;
        }}
    </style>
</head>
<body>
    <div style="display:flex;justify-content:space-between;align-items:center;
                margin-bottom:20px;padding-bottom:12px;border-bottom:1px solid #1f2937;">
        <div>
            <div style="font-size:18px;font-weight:800;color:#f9fafb;">
                Monitor de Posiciones
            </div>
            <div style="font-size:11px;color:#6b7280;font-family:monospace;">
                {timestamp}
            </div>
        </div>
        <div style="text-align:right;font-size:11px;color:#6b7280;">
            {len(positions_data)} {'posicion' if len(positions_data) == 1 else 'posiciones'} abiertas
        </div>
    </div>

    {cards_html}

    <div style="text-align:center;color:#374151;font-size:11px;
                padding:16px 0;margin-top:8px;font-family:monospace;">
        Se actualiza cada {next_interval} min · Auto-refresh cada 5 min
    </div>
</body>
</html>"""

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)


# ══════════════════════════════════════════════════════════════════════════════
# HEARTBEAT & MARKET CLOSE NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def should_send_heartbeat():
    global _last_heartbeat_time
    if not is_market_open():
        return False
    now = datetime.now()
    if _last_heartbeat_time is None:
        return True
    elapsed = (now - _last_heartbeat_time).total_seconds() / 60
    return elapsed >= HEARTBEAT_INTERVAL_MIN


def send_heartbeat(positions_data, timestamp):
    global _last_heartbeat_time
    _last_heartbeat_time = datetime.now()

    positions_with_alerts = [
        pd for pd in positions_data
        if pd["alert_level"] in ("WATCH", "ACTION", "URGENT")
    ]

    if not positions_with_alerts:
        return  # Todo OK — silencio total

    lines = []
    for pd in positions_with_alerts:
        pos   = pd["position"]
        pnl   = pd["pnl_data"]
        level = pd["alert_level"]
        icon  = level_icon(level)
        pct   = pnl["profit_pct_of_max"] * 100
        strat = pos.get("strategy", "")
        lines.append(
            f"{icon} {pos['ticker']} ({strat}) | "
            f"P&L: ${pnl['gross_pnl']:+.0f} ({pct:.0f}% max) | {pnl['dte']}d"
        )

    send_push(
        title=f"Monitor — {len(positions_with_alerts)} posicion(es) requieren atencion",
        message="\n".join(lines) + f"\n{timestamp}",
        priority="default",
    )


def send_market_close_summary(positions_data, timestamp):
    global _market_close_sent
    if _market_close_sent:
        return
    _market_close_sent = True

    if not positions_data:
        send_push(
            title="Mercado cerrado - Sin posiciones",
            message=f"Mercado cerrado.\n{timestamp}",
            priority="low",
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
            f"({pct:.0f}% max) | {pnl['dte']}d al venc."
        )

    send_push(
        title=f"Cierre de mercado — {len(positions_data)} posicion(es)",
        message="\n".join(lines) + f"\n{timestamp}",
        priority="default",
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN MONITOR RUN
# ══════════════════════════════════════════════════════════════════════════════

def run_position_monitor():
    """
    Vigila las posiciones abiertas del libro activo, y las CIERRA cuando toca.

    Antes se llamaba run_paper_monitor y estaba clavada a `paper_positions`.
    Eso significaba que en live NO existía cierre automático de ningún tipo:
    run_monitor() vigila `positions` pero sólo alerta — nunca cerró nada. Los
    stops de -66% de paper los ejecutó esta función, escribiendo una tabla.
    Con plata real no había nada que cerrara una posición sola.

    LO QUE CAMBIA RESPECTO DE LA VERSIÓN ANTERIOR
      - La tabla sale de TRADING_MODE, no está clavada.
      - El cierre NO se hace con UPDATE: pasa por executor.close_position().
        En paper eso llama a cmd_paper_close (mismo resultado que antes); en
        live manda una orden real al broker. La DECISIÓN es idéntica para los
        dos libros; lo único que difiere es la ejecución — que es exactamente
        para lo que existe el Executor.
      - El `reason` viaja hasta la DB: monitor -> executor -> close_reason.

    EL PRECIO DE LA DECISIÓN NO ES EL PRECIO DEL CIERRE
        Acá se pricea para decidir; el executor pricea de nuevo al cerrar. Son
        segundos de diferencia y el segundo es más fresco, así que está bien.
        Pero si ese segundo pricing falla, close_position devuelve False y la
        posición sigue abierta pese a que el stop disparó. No se pierde: el
        worker vuelve en 5 minutos y lo reintenta. Se registra como error, no
        como cierre.
    """
    import psycopg2
    from dotenv import load_dotenv
    from executor import current_mode, get_executor
    load_dotenv()

    mode  = current_mode()
    TABLE = "positions" if mode == "live" else "paper_positions"
    ex    = get_executor()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'=' * 60}")
    print(f"  MONITOR DE POSICIONES [{mode}] — {timestamp}")
    market_status = get_market_status()
    status_label  = {"open": "ABIERTO", "pre": "PRE-MARKET", "closed": "CERRADO"}
    print(f"  Tabla: {TABLE} | Mercado: {status_label.get(market_status, '?')} | "
          f"Intervalo: {get_interval()}min")
    print(f"{'=' * 60}\n")

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur  = conn.cursor()
    cur.execute(f"""
        SELECT id, ticker, strategy, strike_low, strike_high,
               expiration, contracts, total_cost, premium_paid,
               current_spread_value, gross_pnl, pnl_pct,
               profit_pct_of_max, opened_at
        FROM {TABLE}
        WHERE UPPER(status) = 'OPEN'
        ORDER BY opened_at
    """)
    cols      = [d[0] for d in cur.description]
    positions = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()

    if not positions:
        print("  No hay paper positions abiertas.\n")
        return

    print(f"  Paper positions abiertas: {len(positions)}\n")

    for pos in positions:
        ticker      = pos["ticker"]
        strike_low  = float(pos["strike_low"])
        strike_high = float(pos["strike_high"])
        total_cost  = float(pos["total_cost"])
        premium     = float(pos["premium_paid"])
        expiration  = pos["expiration"]
        dte         = (expiration - date.today()).days

        strategy = pos.get("strategy", "Bull Call Spread")
        is_long  = strategy in ("Long Call", "Long Put")

        print(f"  Revisando {ticker} [{mode}]...", end=" ", flush=True)

        # El precio viene de fuentes distintas según la estrategia:
        #   spreads   -> valor del spread por la cadena de Tastytrade
        #   Long Call -> precio de la opción sola por yfinance
        # Son campos separados en spread_pnl porque son mediciones distintas.
        spread_value = None
        long_value   = None
        if is_long:
            opt_type   = "call" if strategy == "Long Call" else "put"
            long_value = get_live_option_price(ticker, strike_low, expiration, opt_type)
        else:
            opt_type     = "put" if premium < 0 else "call"   # el signo manda
            spread_value = get_spread_value_tastytrade(ticker, strike_low, strike_high,
                                                       expiration, opt_type)

        time.sleep(0.5)   # no saturar DXLinkStreamer

        # `precio` es el valor actual: spread_value para spreads, long_value para
        # longs. price_fresh gobierna si se puede CERRAR — nunca con precio viejo.
        precio      = long_value if is_long else spread_value
        price_fresh = True
        if precio is None:
            last_known = float(pos["current_spread_value"] or 0)
            if last_known <= 0:
                print(f"sin datos reales — omitiendo")
                print(f"\n  [--] {ticker} [{mode}] — {strategy} — SIN DATOS")
                print(f"  {'─' * 50}")
                print(f"  Strike(s):     ${strike_low} / ${strike_high}")
                print(f"  Expiracion:    {expiration} ({dte} dias)")
                print(f"  No se pudo obtener precio real.")
                print()
                continue
            precio      = last_known
            price_fresh = False
            print(f"usando último valor conocido: ${precio:.2f} (NO se cerrará con precio viejo)")
        else:
            print(f"{'opción' if is_long else 'spread'}=${precio:.2f}")

        if is_long:
            spread_value = None
            long_value   = precio
        else:
            spread_value = precio

        # P&L: fuente ÚNICA en option_selector.spread_pnl, que ahora maneja las
        # tres estrategias (Bull Call, Bull Put, Long Call). Reemplazó también a
        # calculate_current_pnl, que sólo sabía debit spreads y longs — nunca
        # Bull Put, que es la mitad de la cartera.
        from option_selector import spread_pnl

        contracts     = int(pos.get("contracts") or 1)
        is_put_spread = (not is_long) and premium < 0    # el signo manda

        r = spread_pnl(strike_low, strike_high, premium, contracts, spread_value,
                       strategy=strategy, long_value=long_value)
        max_profit     = r["max_profit"]
        max_loss       = r["max_loss"]
        current_value  = r["current_value"]
        cost_to_close  = r["current_value"]
        gross_pnl      = r["gross_pnl"]
        pnl_pct        = r["pnl_pct"] if r["pnl_pct"] is not None else 0
        profit_pct_max = r["profit_pct_of_max"] if r["profit_pct_of_max"] is not None else 0
        strategy_type  = r["strategy_type"]

        pnl_data = {
            "profit_pct_of_max": profit_pct_max,
            "pnl_pct":           pnl_pct,
            "dte":               dte,
            "strategy_type":     strategy_type,
        }
        alert_level, reasons = evaluate_alert_level(pnl_data)
        icon = level_icon(alert_level)

        print(f"\n  {icon} {ticker} [{mode}] — {strategy} — {alert_level}")
        print(f"  {'─' * 50}")
        print(f"  Strike(s):     ${strike_low} / ${strike_high}")
        print(f"  Expiracion:    {expiration} ({dte} dias)")
        if is_put_spread:
            print(f"  Crédito rec:   ${abs(premium):.2f} (max ganancia ${max_profit:.2f})")
            print(f"  Costo cierre:  ${cost_to_close:.2f}")
        else:
            print(f"  Costo total:   ${total_cost:.2f}")
        print(f"  Valor actual:  ${precio:.2f}")
        print(f"  Ganancia/Perd: ${gross_pnl:+.2f} ({pnl_pct:+.1f}%)")
        print(f"  % del maximo:  {profit_pct_max*100:.1f}%")
        if max_profit is not None:
            print(f"  Ganancia max:  ${max_profit:.2f}")
        print(f"\n  Alertas:")
        for motivo in reasons:
            print(f"    - {motivo}")
        print()

        # ── ALERTA (lo que antes hacía run_monitor) ───────────────────────────
        # Con mercado cerrado no podés actuar: silencio hasta apertura, para no
        # despertar el teléfono de noche por algo que no se puede tocar.
        if alert_level in ("WATCH", "ACTION", "URGENT") and price_fresh:
            if get_market_status() in ("open", "pre"):
                send_alert_notification(pos, {**pnl_data, "gross_pnl": gross_pnl},
                                        alert_level, reasons)

        # ── Cierre determinista — el worker es el ÚNICO dueño de stops de paper ─
        # Usa las mismas constantes canónicas que el monitor real (sin inventar
        # un cuarto criterio): stop escalonado por DTE, target 70%, DTE mínimo.
        close_reason = None
        if price_fresh:
            if dte is not None and dte <= MIN_DTE:
                close_reason = "TIME_EXPIRED"
            elif profit_pct_max >= TAKE_PROFIT_MAX_PCT:
                close_reason = "TARGET_REACHED"
            elif pnl_pct <= -(get_stop_loss_pct(dte) * 100):
                close_reason = "STOP_LOSS"

        # ── EL CIERRE PASA POR EL EXECUTOR ────────────────────────────────────
        # Antes esto era un UPDATE directo, y por eso el cierre automático NO
        # existía en live: escribir una tabla no le dice nada al broker.
        # Ahora la decisión es la misma para los dos libros y la ejecución la
        # resuelve el executor — paper escribe la DB, live manda una orden real.
        if close_reason:
            print(f"  → AUTO-CIERRE [{mode}]: {close_reason}")
            try:
                cerrada = ex.close_position(ticker, close_reason)
            except Exception as e:
                cerrada = False
                print(f"  ⛔ {ticker}: el cierre reventó: {e}")

            if cerrada:
                send_push(
                    title=f"Auto-cierre [{mode}]: {ticker} ({close_reason})",
                    message=(f"{ticker} {strategy} ${strike_low}/{strike_high}\n"
                             f"P&L ${gross_pnl:+.2f} ({pnl_pct:+.1f}%) | DTE {dte}\n"
                             f"Motivo: {close_reason}"),
                    priority="default",
                )
                continue

            # No cerró. La posición SIGUE ABIERTA aunque el stop haya disparado.
            # No se pierde: el worker vuelve en 5 minutos y reintenta. Pero si es
            # un stop, cada ciclo que pasa es dinero, así que se avisa fuerte.
            print(f"  ⛔ {ticker}: {close_reason} disparó y NO se pudo cerrar — "
                  f"sigue ABIERTA")
            send_push(
                title=f"NO se pudo cerrar {ticker} ({close_reason})",
                message=(f"{ticker} {strategy} ${strike_low}/{strike_high}\n"
                         f"P&L ${gross_pnl:+.2f} ({pnl_pct:+.1f}%) | DTE {dte}\n\n"
                         f"El {close_reason} disparó y la posición SIGUE ABIERTA.\n"
                         f"El worker reintenta en 5 min."),
                priority="urgent" if close_reason == "STOP_LOSS" else "high",
            )
            # Cae al UPDATE de P&L: la posición sigue viva y su estado importa.

        # Sin cierre (o cierre fallido) — solo actualizar P&L
        conn2 = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur2  = conn2.cursor()
        cur2.execute(f"""
            UPDATE {TABLE} SET
                current_spread_value = %s,
                current_value        = %s,
                gross_pnl            = %s,
                pnl_pct              = %s,
                profit_pct_of_max    = %s,
                last_synced_at       = NOW()
            WHERE id = %s AND UPPER(status) = 'OPEN'
        """, (spread_value, current_value, gross_pnl, pnl_pct,
              profit_pct_max, pos["id"]))
        conn2.commit()
        cur2.close()
        conn2.close()

    print(f"{'=' * 60}")
    print(f"  Monitor [{mode}] completado — {timestamp}\n")


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULED RUN (Railway)
# ══════════════════════════════════════════════════════════════════════════════

def healthcheck_ping():
    """
    Ping al dead-man's switch (healthchecks.io). Lo llama el LOOP de
    run_monitor.py, no este módulo.

    POR QUÉ ESTO Y NO UN PUSH POR CICLO
        El monitor corre cada 5 minutos: avisar "estoy vivo" serían 78 pushes
        por día, y un canal que se ignora es un canal que no existe — el aviso
        urgente del stop se perdería entre el ruido.
        Y sobre todo: un proceso muerto NO PUEDE avisar que murió. El 17-jul el
        auto_run se saltó medio día y te enteraste porque no sonó el teléfono.
        La ausencia como señal sólo funciona si estás mirando.
        Acá el aviso viene de AFUERA: si el worker deja de pegar, healthchecks
        te avisa. Es el único que funciona con el worker caído.

    RESPONDE UNA SOLA PREGUNTA: ¿el proceso está vivo?
        No "¿el ciclo funcionó?". El loop pinga cada 60s pase lo que pase,
        mientras que scheduled_run corre cada 5min con el mercado abierto y cada
        30 con el mercado cerrado — un check atado al ciclo gritaría todas las
        noches y todos los fines de semana, y un check que da falsa alarma dos
        veces al día se ignora en una semana.

    LO QUE NO CUBRE, Y HAY QUE SABERLO
        Un ciclo que revienta SIEMPRE, con el loop girando, pinga "sano". Esa
        brecha hoy sólo se ve en los logs de Railway. Se intentó cubrir con un
        /fail desde scheduled_run, pero el OK del minuto siguiente lo borraba:
        dos preguntas distintas no entran en un solo check.

    Sin HEALTHCHECK_URL no hace nada y no molesta: es una red de seguridad
    opcional, no una dependencia.
    """
    url = os.getenv("HEALTHCHECK_URL", "").strip()
    if not url:
        return
    try:
        import requests
        requests.get(url.rstrip("/"), timeout=5)
    except Exception as e:
        # Que el ping falle NO puede tumbar el ciclo. Si healthchecks no es
        # alcanzable, va a avisar solo por la ausencia — que es su trabajo.
        print(f"  healthcheck ping falló: {e}")


def scheduled_run():
    # Un solo monitor: precia una vez, alerta y cierra, sobre el libro del modo.
    # Antes había dos (run_monitor sólo alertaba, run_position_monitor cerraba),
    # que priceaban lo mismo con segundos de diferencia. Fusionados en B.
    try:
        run_position_monitor()
    except Exception as e:
        print(f"  monitor de posiciones error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor de posiciones")
    parser.add_argument("--loop",  action="store_true",
                        help="Correr en loop adaptativo (posiciones reales)")
    parser.add_argument("--paper", action="store_true",
                        help="Correr el monitor de posiciones del libro activo "
                             "(TRADING_MODE decide la tabla)")
    args = parser.parse_args()

    if args.paper:
        run_position_monitor()
    elif args.loop:
        print(f"\n  Monitor en loop adaptativo")
        print(f"  Mercado abierto:  cada {INTERVAL_MARKET_OPEN}min")
        print(f"  Pre-market:       cada {INTERVAL_PRE_MARKET}min")
        print(f"  Mercado cerrado:  cada {INTERVAL_MARKET_CLOSED}min\n")
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
                print(f"  Intervalo ajustado: {current_interval}min")
            time.sleep(60)
    else:
        run_position_monitor()