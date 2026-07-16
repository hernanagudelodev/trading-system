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

from notify import send_ntfy

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

def calculate_current_pnl(position, current_price):
    """
    Calculate P&L for any supported strategy.

    Strategy logic:
        Bull Call Spread / Bear Put Spread
            → real spread value from Tastytrade (includes time value)
              fallback: intrinsic-only estimate
        Long Call / Long Put
            → live option price from yfinance (fallback: delta approximation)
        Cash Secured Put / Covered Call
            → premium decay: profit = premium - current option value
        Long Straddle
            → combined call + put value

    Returns dict with: current_price, current_value, gross_pnl, pnl_pct,
                       profit_pct_of_max, max_profit, dte, spread_value,
                       strategy_type, delta (for long options)
    """
    strategy      = position.get("strategy", "Bull Call Spread")
    strike_low    = float(position["strike_low"])
    strike_high   = float(position["strike_high"])
    contracts     = int(position["contracts"])
    total_cost    = float(position.get("total_cost") or 0)
    expiration    = position["expiration"]
    ticker        = position["ticker"]
    price_at_open = float(position.get("price_at_open") or strike_low)

    # Parse expiration
    if isinstance(expiration, date):
        exp_date = expiration
    else:
        exp_date = datetime.strptime(str(expiration), "%Y-%m-%d").date()

    dte = (exp_date - date.today()).days

    # ── DEBIT SPREADS (Bull Call Spread, Bear Put Spread) ────────────────────
    if strategy in DEBIT_SPREADS:
        spread_width      = strike_high - strike_low
        premium_per_share = total_cost / (contracts * 100) if contracts > 0 else 0
        max_profit        = (spread_width - premium_per_share) * contracts * 100

        # Try real market value from Tastytrade first (includes time value)
        spread_value = get_spread_value_tastytrade(ticker, strike_low, strike_high, exp_date)

        if spread_value is None:
            # Fallback: intrinsic-only estimate (no time value)
            if strategy == "Bull Call Spread":
                if current_price >= strike_high:
                    spread_value = spread_width
                elif current_price <= strike_low:
                    spread_value = 0
                else:
                    spread_value = current_price - strike_low
            else:  # Bear Put Spread
                if current_price <= strike_low:
                    spread_value = spread_width
                elif current_price >= strike_high:
                    spread_value = 0
                else:
                    spread_value = strike_high - current_price

        current_value     = spread_value * contracts * 100
        gross_pnl         = current_value - total_cost
        pnl_pct           = (gross_pnl / total_cost * 100) if total_cost > 0 else 0
        profit_pct_of_max = (gross_pnl / max_profit) if max_profit > 0 else 0

        return {
            "current_price":      current_price,
            "current_value":      current_value,
            "gross_pnl":          gross_pnl,
            "pnl_pct":            pnl_pct,
            "profit_pct_of_max":  profit_pct_of_max,
            "max_profit":         max_profit,
            "dte":                dte,
            "spread_value":       spread_value,
            "strategy_type":      "debit_spread",
            "delta":              None,
        }

    # ── LONG OPTIONS (Long Call, Long Put) ───────────────────────────────────
    elif strategy in LONG_OPTIONS:
        option_type  = "call" if strategy == "Long Call" else "put"
        premium_paid = total_cost / (contracts * 100) if contracts > 0 else 0

        live_price = get_live_option_price(ticker, strike_low, exp_date, option_type)

        if live_price is not None:
            current_value = live_price * contracts * 100
            delta = get_live_delta(ticker, strike_low, exp_date, option_type)
        else:
            delta         = get_live_delta(ticker, strike_low, exp_date, option_type)
            price_change  = current_price - price_at_open
            estimated_val = max(0, premium_paid + (abs(delta) * price_change))
            current_value = estimated_val * contracts * 100

        max_profit        = total_cost * 2
        gross_pnl         = current_value - total_cost
        pnl_pct           = (gross_pnl / total_cost * 100) if total_cost > 0 else 0
        profit_pct_of_max = (gross_pnl / max_profit) if max_profit > 0 else 0

        return {
            "current_price":      current_price,
            "current_value":      current_value,
            "gross_pnl":          gross_pnl,
            "pnl_pct":            pnl_pct,
            "profit_pct_of_max":  profit_pct_of_max,
            "max_profit":         max_profit,
            "dte":                dte,
            "spread_value":       current_value / (contracts * 100) if contracts > 0 else 0,
            "strategy_type":      "long_option",
            "delta":              delta,
        }

    # ── CREDIT OPTIONS (Cash Secured Put, Covered Call) ──────────────────────
    elif strategy in CREDIT_OPTIONS:
        option_type      = "put" if strategy == "Cash Secured Put" else "call"
        premium_received = total_cost

        live_price = get_live_option_price(ticker, strike_low, exp_date, option_type)

        if live_price is not None:
            cost_to_close = live_price * contracts * 100
        else:
            delta        = get_live_delta(ticker, strike_low, exp_date, option_type)
            price_change = current_price - price_at_open
            remaining    = max(0, (premium_received / (contracts * 100)) - (abs(delta) * abs(price_change)))
            cost_to_close = remaining * contracts * 100

        max_profit        = premium_received
        gross_pnl         = premium_received - cost_to_close
        pnl_pct           = (gross_pnl / premium_received * 100) if premium_received > 0 else 0
        profit_pct_of_max = (gross_pnl / max_profit) if max_profit > 0 else 0

        return {
            "current_price":      current_price,
            "current_value":      cost_to_close,
            "gross_pnl":          gross_pnl,
            "pnl_pct":            pnl_pct,
            "profit_pct_of_max":  profit_pct_of_max,
            "max_profit":         max_profit,
            "dte":                dte,
            "spread_value":       cost_to_close / (contracts * 100) if contracts > 0 else 0,
            "strategy_type":      "credit_option",
            "delta":              None,
        }

    # ── LONG STRADDLE ────────────────────────────────────────────────────────
    elif strategy in STRADDLES:
        strike     = strike_low
        call_price = get_live_option_price(ticker, strike, exp_date, "call")
        put_price  = get_live_option_price(ticker, strike, exp_date, "put")

        if call_price is not None and put_price is not None:
            current_value = (call_price + put_price) * contracts * 100
        else:
            call_delta        = get_live_delta(ticker, strike, exp_date, "call")
            put_delta         = get_live_delta(ticker, strike, exp_date, "put")
            premium_per_share = total_cost / (contracts * 100) if contracts > 0 else 0
            price_change      = current_price - price_at_open
            call_val  = max(0, (premium_per_share / 2) + call_delta * price_change)
            put_val   = max(0, (premium_per_share / 2) - abs(put_delta) * price_change)
            current_value = (call_val + put_val) * contracts * 100

        max_profit        = total_cost * 3
        gross_pnl         = current_value - total_cost
        pnl_pct           = (gross_pnl / total_cost * 100) if total_cost > 0 else 0
        profit_pct_of_max = (gross_pnl / max_profit) if max_profit > 0 else 0

        return {
            "current_price":      current_price,
            "current_value":      current_value,
            "gross_pnl":          gross_pnl,
            "pnl_pct":            pnl_pct,
            "profit_pct_of_max":  profit_pct_of_max,
            "max_profit":         max_profit,
            "dte":                dte,
            "spread_value":       current_value / (contracts * 100) if contracts > 0 else 0,
            "strategy_type":      "straddle",
            "delta":              None,
        }

    # ── FALLBACK (unknown strategy) ───────────────────────────────────────────
    else:
        return {
            "current_price":      current_price,
            "current_value":      0,
            "gross_pnl":          0,
            "pnl_pct":            0,
            "profit_pct_of_max":  0,
            "max_profit":         total_cost,
            "dte":                dte,
            "spread_value":       0,
            "strategy_type":      "unknown",
            "delta":              None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# ALERT EVALUATION
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

    send_ntfy(title, message, priority=priorities.get(alert_level, "default"))


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

    send_ntfy(
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
        send_ntfy(
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

    send_ntfy(
        title=f"Cierre de mercado — {len(positions_data)} posicion(es)",
        message="\n".join(lines) + f"\n{timestamp}",
        priority="default",
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN MONITOR RUN
# ══════════════════════════════════════════════════════════════════════════════

def run_monitor(ask_ai=False):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'=' * 60}")
    print(f"  MONITOR DE POSICIONES — {timestamp}")

    market_status = get_market_status()
    status_label  = {"open": "ABIERTO", "pre": "PRE-MARKET", "closed": "CERRADO"}
    print(f"  Mercado: {status_label.get(market_status, '?')} | "
          f"Intervalo: {get_interval()}min")

    print(f"\n  Alertas:")
    print(f"{'=' * 60}")

    positions = get_open_positions()

    if not positions:
        print("\n  No hay posiciones abiertas.\n")
        generate_html_report([], timestamp)
        return

    print(f"\n  Posiciones abiertas: {len(positions)}")

    positions_data = []
    ntfy_sent      = set()

    for position in positions:
        ticker   = position["ticker"]
        strategy = position.get("strategy", "desconocida")
        print(f"\n  Revisando {ticker} ({strategy})...", end=" ", flush=True)

        try:
            criteria = get_all_criteria(ticker)
            if criteria is None:
                print("Sin datos de mercado")
                continue

            current_price = criteria["price"]
            pnl_data      = calculate_current_pnl(position, current_price)
            pnl_data["total_cost"] = float(position.get("total_cost") or 0)
            alert_level, reasons   = evaluate_alert_level(pnl_data)

            print(f"{level_icon(alert_level)} {alert_level}")
            print_position_report(position, pnl_data, alert_level, reasons)

            if alert_level in ("WATCH", "ACTION", "URGENT") and ticker not in ntfy_sent:
                # Solo notificar si el mercado está abierto o en pre-market
                # Con mercado cerrado no puedes actuar — silencio hasta apertura
                if get_market_status() in ("open", "pre"):
                    send_alert_notification(position, pnl_data, alert_level, reasons)
                    ntfy_sent.add(ticker)

            positions_data.append({
                "position":    position,
                "pnl_data":    pnl_data,
                "alert_level": alert_level,
                "reasons":     reasons,
            })

        except Exception as e:
            print(f"Error: {e}")
            continue

    generate_html_report(positions_data, timestamp)

    print(f"\n{'=' * 60}")
    urgent = sum(1 for p in positions_data if p["alert_level"] == "URGENT")
    action = sum(1 for p in positions_data if p["alert_level"] == "ACTION")
    watch  = sum(1 for p in positions_data if p["alert_level"] == "WATCH")
    normal = sum(1 for p in positions_data if p["alert_level"] == "NORMAL")
    print(f"  URGENT: {urgent}  ACTION: {action}  WATCH: {watch}  NORMAL: {normal}")
    print(f"{'=' * 60}")
    print(f"  Monitor completado — {timestamp}\n")

    if should_send_heartbeat():
        send_heartbeat(positions_data, timestamp)

    global _market_close_sent
    if get_market_status() == "closed" and not _market_close_sent:
        send_market_close_summary(positions_data, timestamp)
    elif get_market_status() == "open":
        _market_close_sent = False


# ══════════════════════════════════════════════════════════════════════════════
# PAPER MONITOR
# ══════════════════════════════════════════════════════════════════════════════

def run_paper_monitor():
    """
    Monitor paper positions — console only, no push notifications.
    Uses real Tastytrade prices for P&L calculation.
    """
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'=' * 60}")
    print(f"  PAPER MONITOR — {timestamp}")
    market_status = get_market_status()
    status_label  = {"open": "ABIERTO", "pre": "PRE-MARKET", "closed": "CERRADO"}
    print(f"  Mercado: {status_label.get(market_status, '?')} | "
          f"Intervalo: {get_interval()}min")
    print(f"{'=' * 60}\n")

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, ticker, strategy, strike_low, strike_high,
               expiration, contracts, total_cost, premium_paid,
               current_spread_value, gross_pnl, pnl_pct,
               profit_pct_of_max, opened_at
        FROM paper_positions
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

        print(f"  Revisando {ticker} (paper)...", end=" ", flush=True)

        # Determine option type for spread value fetch
        strategy     = pos.get("strategy", "Bull Call Spread")
        opt_type     = "put" if strategy == "Bull Put Spread" else "call"
        spread_value = get_spread_value_tastytrade(ticker, strike_low, strike_high,
                                                   expiration, opt_type)

        # Small delay to avoid overwhelming DXLinkStreamer
        time.sleep(0.5)

        price_fresh = True
        if spread_value is None:
            last_known = float(pos["current_spread_value"] or 0)
            if last_known <= 0:
                print(f"sin datos reales — omitiendo")
                print(f"\n  [--] {ticker} (PAPER) — {strategy} — SIN DATOS")
                print(f"  {'─' * 50}")
                print(f"  Strike(s):     ${strike_low} / ${strike_high}")
                print(f"  Expiracion:    {expiration} ({dte} dias)")
                print(f"  No se pudo obtener precio real del spread.")
                print()
                continue
            spread_value = last_known
            price_fresh  = False
            print(f"usando último valor conocido: ${spread_value:.2f} (NO se cerrará con precio viejo)")
        else:
            print(f"spread=${spread_value:.2f}")

        spread_width  = strike_high - strike_low
        strategy      = pos.get("strategy", "Bull Call Spread")
        is_put_spread = strategy == "Bull Put Spread"

        if is_put_spread:
            net_credit     = abs(premium)
            max_profit     = round(net_credit * 100, 2)
            max_loss       = round((spread_width - net_credit) * 100, 2)
            cost_to_close  = round(spread_value * 100, 2)
            current_value  = cost_to_close
            gross_pnl      = round(max_profit - cost_to_close, 2)
            pnl_pct        = round(gross_pnl / max_profit * 100, 2) if max_profit else 0
            profit_pct_max = round(gross_pnl / max_profit, 4) if max_profit else 0
            strategy_type  = "credit_spread"
        else:
            max_profit     = round((spread_width - premium) * 100, 2)
            current_value  = round(spread_value * 100, 2)
            gross_pnl      = round(current_value - total_cost, 2)
            pnl_pct        = round(gross_pnl / total_cost * 100, 2) if total_cost else 0
            profit_pct_max = round(gross_pnl / max_profit, 4) if max_profit else 0
            strategy_type  = "debit_spread"

        # Alert level
        pnl_data = {
            "profit_pct_of_max": profit_pct_max,
            "pnl_pct":           pnl_pct,
            "dte":               dte,
            "strategy_type":     strategy_type,
        }
        alert_level, reasons = evaluate_alert_level(pnl_data)
        icon = level_icon(alert_level)

        print(f"\n  {icon} {ticker} (PAPER) — {strategy} — {alert_level}")
        print(f"  {'─' * 50}")
        print(f"  Strike(s):     ${strike_low} / ${strike_high}")
        print(f"  Expiracion:    {expiration} ({dte} dias)")
        if is_put_spread:
            print(f"  Crédito rec:   ${abs(premium):.2f} (max ganancia ${max_profit:.2f})")
            print(f"  Costo cierre:  ${cost_to_close:.2f}")
        else:
            print(f"  Costo total:   ${total_cost:.2f}")
        print(f"  Spread actual: ${spread_value:.2f}")
        print(f"  Ganancia/Perd: ${gross_pnl:+.2f} ({pnl_pct:+.1f}%)")
        print(f"  % del maximo:  {profit_pct_max*100:.1f}%")
        print(f"  Ganancia max:  ${max_profit:.2f}")
        print(f"\n  Alertas:")
        for r in reasons:
            print(f"    - {r}")
        print()

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

        conn2 = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur2  = conn2.cursor()

        if close_reason:
            print(f"  → AUTO-CIERRE (paper): {close_reason}")
            cur2.execute("""
                UPDATE paper_positions SET
                    current_spread_value = %s, current_value = %s,
                    gross_pnl = %s, pnl_pct = %s, profit_pct_of_max = %s,
                    premium_received = %s, total_received = %s,
                    status = 'CLOSED', closed_at = NOW(),
                    close_reason = %s, last_synced_at = NOW()
                WHERE id = %s AND UPPER(status) = 'OPEN'
            """, (spread_value, current_value, gross_pnl, pnl_pct, profit_pct_max,
                  spread_value, current_value, close_reason, pos["id"]))
            conn2.commit()
            cur2.close()
            conn2.close()
            # Paper normalmente es silencioso; un cierre SÍ amerita aviso.
            send_ntfy(
                title=f"Paper auto-cierre: {ticker} ({close_reason})",
                message=(f"{ticker} {strategy} ${strike_low}/{strike_high}\n"
                         f"P&L ${gross_pnl:+.2f} ({pnl_pct:+.1f}%) | DTE {dte}\n"
                         f"Motivo: {close_reason}"),
                priority="default",
            )
            continue

        # Sin cierre — solo actualizar P&L (silencioso)
        cur2.execute("""
            UPDATE paper_positions SET
                current_spread_value = %s,
                current_value        = %s,
                gross_pnl            = %s,
                pnl_pct              = %s,
                profit_pct_of_max    = %s,
                last_synced_at       = NOW()
            WHERE id = %s
        """, (spread_value, current_value, gross_pnl, pnl_pct,
              profit_pct_max, pos["id"]))
        conn2.commit()
        cur2.close()
        conn2.close()

    print(f"{'=' * 60}")
    print(f"  Paper monitor completado — {timestamp}\n")
    print(f"  NOTA: Paper positions son silenciosas — sin push notifications.\n")


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULED RUN (Railway)
# ══════════════════════════════════════════════════════════════════════════════

def scheduled_run():
    run_monitor(ask_ai=False)
    # El worker ahora también vigila y cierra paper intradía (stops deterministas)
    try:
        run_paper_monitor()
    except Exception as e:
        print(f"  paper monitor error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor de posiciones")
    parser.add_argument("--loop",  action="store_true",
                        help="Correr en loop adaptativo (posiciones reales)")
    parser.add_argument("--paper", action="store_true",
                        help="Monitorear paper positions (sin push notifications)")
    args = parser.parse_args()

    if args.paper:
        run_paper_monitor()
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
        run_monitor(ask_ai=True)