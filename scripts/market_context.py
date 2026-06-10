"""
market_context.py
=================
Daily macro analysis for the options trading system.

Analyzes:
    1. VIX level and trend
    2. SPY position vs SMAs and momentum
    3. Upcoming macro events this week (NFP, FOMC, CPI — fixed calendar)
    4. Upcoming earnings from sector watchlist (via Tastytrade API)
    5. Sector context (historical win rates — informational only)

Output:
    - reports/market_context_report.html  → visual dashboard
    - reports/market_context.json         → structured data for scanner.py

NO AI in this script — raw data only.
The AI interpretation happens once in scanner.py or auto_run.py.

Usage:
    python market_context.py

Dependencies:
    yfinance        → market data
    tastytrade API  → earnings dates per ticker
    db.py           → historical win rates (backtest)
    .env            → DATABASE_URL, TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN
"""

import os
import sys
import json
import asyncio
import warnings
import datetime
import io

import yfinance as yf
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

VIX_CALM     = 18
VIX_ELEVATED = 25
VIX_FEAR     = 35

# Reports directory (one level up from scripts/)
_BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPORTS    = os.path.join(_BASE_DIR, "reports")
REPORT_PATH     = os.path.join(_REPORTS, "market_context_report.html")
JSON_PATH       = os.path.join(_REPORTS, "market_context.json")
AI_SUMMARY_PATH = os.path.join(_REPORTS, "market_context_ai.md")
SP500_JSON  = os.path.join(os.path.dirname(__file__), "sp500_tickers.json")


def _open_browser(path):
    """Open in browser only when running locally — skip on Railway/server."""
    if os.getenv("RAILWAY_ENVIRONMENT"):
        return
    try:
        import webbrowser
        webbrowser.open(f"file:///{path.replace(os.sep, '/')}")
    except Exception:
        pass

# Sector earnings watchlist — barómetros por sector
# These are NOT trade candidates — they're tracked for contagion risk
EARNINGS_WATCHLIST = [
    # Semiconductors — sector leaders whose earnings move the whole sector
    "NVDA", "AVGO", "AMD", "TSM", "MU", "INTC",
    # Mega-cap Tech — broad market impact
    "AAPL", "MSFT", "GOOGL", "META", "AMZN",
    # Financials
    "JPM", "GS", "BAC",
    # Consumer
    "WMT", "COST", "HD",
    # Health
    "JNJ", "UNH", "LLY",
    # Energy
    "XOM", "CVX",
]

# Days ahead to look for earnings risk
EARNINGS_RISK_DAYS = 10

# Fixed macro calendar — recurring monthly events
# NFP: first Friday of month
# CPI: ~10th-12th of month (released for prior month)
# FOMC: 8 meetings per year, roughly every 6 weeks
MACRO_EVENTS_FIXED = {
    "NFP":  "Primer viernes del mes — Non-Farm Payrolls. Fuerte impacto en tasas esperadas.",
    "CPI":  "Semana del 10-12 del mes — Consumer Price Index. Determina política monetaria de la Fed.",
    "FOMC": "Reunión Fed cada ~6 semanas. Decisión de tasas — mayor event risk del mercado.",
    "PPI":  "Semana del 14-15 del mes — Producer Price Index. Indicador adelantado del CPI.",
}

# Known FOMC dates 2026
FOMC_DATES_2026 = [
    datetime.date(2026, 1, 27), datetime.date(2026, 1, 28),
    datetime.date(2026, 3, 17), datetime.date(2026, 3, 18),
    datetime.date(2026, 4, 28), datetime.date(2026, 4, 29),
    datetime.date(2026, 6, 16), datetime.date(2026, 6, 17),
    datetime.date(2026, 7, 28), datetime.date(2026, 7, 29),
    datetime.date(2026, 9, 15), datetime.date(2026, 9, 16),
    datetime.date(2026, 11, 3), datetime.date(2026, 11, 4),
    datetime.date(2026, 12, 15), datetime.date(2026, 12, 16),
]


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _silence():
    old = sys.stderr
    sys.stderr = io.StringIO()
    return old

def _restore(old):
    sys.stderr = old

def _sma(series, n):
    return series.rolling(window=n).mean().iloc[-1]

def _pct_change(series, n):
    if len(series) < n + 1:
        return None
    return (series.iloc[-1] - series.iloc[-n]) / series.iloc[-n] * 100


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DATA — VIX & SPY
# ══════════════════════════════════════════════════════════════════════════════

def get_vix():
    old = _silence()
    try:
        import yfinance as yf
        data = yf.download("^VIX", period="20d", interval="1d",
                           progress=False, auto_adjust=True)
        _restore(old)
        if data.empty:
            return None

        closes  = data["Close"].squeeze().dropna()
        current = float(closes.iloc[-1])
        avg_5d  = float(closes.iloc[-5:].mean())
        avg_10d = float(closes.iloc[-10:].mean())

        trend = "FALLING" if current < avg_5d * 0.95 else \
                "RISING"  if current > avg_5d * 1.05 else "STABLE"

        if current < VIX_CALM:
            level, score = "CALM", 2
        elif current < VIX_ELEVATED:
            level, score = "ELEVATED", 1
        elif current < VIX_FEAR:
            level, score = "HIGH", -1
        else:
            level, score = "EXTREME", -2

        return {"current": round(current, 2), "avg_5d": round(avg_5d, 2),
                "avg_10d": round(avg_10d, 2), "trend": trend,
                "level": level, "score": score}
    except Exception:
        _restore(old)
        return None


def get_spy_context():
    old = _silence()
    try:
        import yfinance as yf
        data = yf.download("SPY", period="1y", interval="1d",
                           progress=False, auto_adjust=True)
        _restore(old)
        if data.empty or len(data) < 50:
            return None

        closes  = data["Close"].squeeze().dropna()
        price   = float(closes.iloc[-1])
        sma50   = float(_sma(closes, 50))
        sma200  = float(_sma(closes, 200)) if len(closes) >= 200 else None
        pct_25d = _pct_change(closes, 25)

        above_50  = price > sma50
        above_200 = (price > sma200) if sma200 else None

        if above_50 and above_200:
            sma_status, sma_score = "ABOVE BOTH", 2
        elif above_50:
            sma_status, sma_score = "ABOVE SMA50", 1
        elif above_200:
            sma_status, sma_score = "BELOW SMA50", 0
        else:
            sma_status, sma_score = "BELOW BOTH", -2

        sma50_series = closes.rolling(50).mean()
        sma50_10d    = float(sma50_series.iloc[-10])
        if sma50 > sma50_10d * 1.001:
            sma50_dir = "RISING"
        elif sma50 < sma50_10d * 0.999:
            sma50_dir = "FALLING"
        else:
            sma50_dir = "FLAT"

        trend = "BULLISH" if (above_50 and pct_25d and pct_25d > 0) else \
                "BEARISH" if (not above_50 and pct_25d and pct_25d < 0) else "NEUTRAL"

        spy_score = sma_score + (1 if pct_25d and pct_25d > 2 else
                                  -1 if pct_25d and pct_25d < -2 else 0)

        return {"price": round(price, 2), "sma50": round(sma50, 2),
                "sma200": round(sma200, 2) if sma200 else None,
                "pct_25d": round(pct_25d, 1) if pct_25d else None,
                "sma_status": sma_status, "sma50_dir": sma50_dir,
                "trend": trend, "score": spy_score,
                "above_sma50": above_50, "above_sma200": above_200}
    except Exception:
        _restore(old)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# MACRO CALENDAR — upcoming events this week
# ══════════════════════════════════════════════════════════════════════════════

def get_macro_events():
    """
    Detect upcoming macro events in the next 7 days based on fixed calendar.
    Returns list of dicts with event name, days_away, and description.
    """
    today    = datetime.date.today()
    events   = []
    look_days = 7

    for d in range(look_days + 1):
        check = today + datetime.timedelta(days=d)
        day   = check.day
        weekday = check.weekday()  # 0=Monday, 4=Friday

        # NFP — first Friday of the month
        if weekday == 4 and day <= 7:
            events.append({
                "event":       "NFP — Non-Farm Payrolls",
                "date":        str(check),
                "days_away":   d,
                "impact":      "HIGH",
                "description": "Dato de empleo. Si supera estimados → Fed sin razón para bajar tasas → presión bajista en acciones."
            })

        # CPI — ~10th-12th of month (release for prior month)
        if day in [10, 11, 12] and weekday not in [5, 6]:
            events.append({
                "event":       "CPI — Consumer Price Index",
                "date":        str(check),
                "days_away":   d,
                "impact":      "HIGH",
                "description": "Inflación al consumidor. Dato caliente → Fed sube tasas → mercado cae."
            })

        # PPI — ~14th-15th of month
        if day in [14, 15] and weekday not in [5, 6]:
            events.append({
                "event":       "PPI — Producer Price Index",
                "date":        str(check),
                "days_away":   d,
                "impact":      "MEDIUM",
                "description": "Inflación al productor. Indicador adelantado del CPI."
            })

        # FOMC — from known dates list
        if check in FOMC_DATES_2026:
            events.append({
                "event":       "FOMC — Fed Meeting",
                "date":        str(check),
                "days_away":   d,
                "impact":      "VERY_HIGH",
                "description": "Decisión de tasas de la Fed. Mayor event risk del año."
            })

    # Remove duplicates by event name
    seen   = set()
    unique = []
    for e in events:
        if e["event"] not in seen:
            seen.add(e["event"])
            unique.append(e)

    return sorted(unique, key=lambda x: x["days_away"])


# ══════════════════════════════════════════════════════════════════════════════
# EARNINGS WATCHLIST — via Tastytrade API
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_earnings_async(tickers):
    """Fetch earnings dates for watchlist tickers via Tastytrade."""
    from tastytrade import Session
    from tastytrade.metrics import get_market_metrics

    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")

    try:
        session = Session(client_secret, refresh_token)
        metrics = await get_market_metrics(session, tickers)
        return {m.symbol: m for m in metrics}
    except Exception as e:
        print(f"  Earnings fetch error: {e}")
        return {}


def get_upcoming_earnings():
    """
    Get earnings dates for sector watchlist tickers.
    Returns list of tickers reporting in the next EARNINGS_RISK_DAYS days,
    grouped by sector impact.
    """
    today    = datetime.date.today()
    upcoming = []

    try:
        metrics_map = asyncio.run(_fetch_earnings_async(EARNINGS_WATCHLIST))
    except Exception:
        metrics_map = {}

    TICKER_SECTOR_MAP = {
        "NVDA": "Semiconductors", "AVGO": "Semiconductors",
        "AMD":  "Semiconductors", "TSM":  "Semiconductors",
        "MU":   "Semiconductors", "INTC": "Semiconductors",
        "AAPL": "Mega-cap Tech",  "MSFT": "Mega-cap Tech",
        "GOOGL":"Mega-cap Tech",  "META": "Mega-cap Tech",
        "AMZN": "Mega-cap Tech",
        "JPM":  "Financials",     "GS":   "Financials",   "BAC": "Financials",
        "WMT":  "Consumer",       "COST": "Consumer",     "HD":  "Consumer",
        "JNJ":  "Health",         "UNH":  "Health",       "LLY": "Health",
        "XOM":  "Energy",         "CVX":  "Energy",
    }

    for ticker, m in metrics_map.items():
        try:
            earnings = getattr(m, "earnings", None)
            if not earnings:
                continue
            exp_date = getattr(earnings, "expected_report_date", None)
            if not exp_date:
                continue
            days_away = (exp_date - today).days
            if 0 <= days_away <= EARNINGS_RISK_DAYS:
                upcoming.append({
                    "ticker":    ticker,
                    "sector":    TICKER_SECTOR_MAP.get(ticker, "Other"),
                    "date":      str(exp_date),
                    "days_away": days_away,
                    "warning":   f"⚠️ {ticker} ({TICKER_SECTOR_MAP.get(ticker, '?')}) reporta en {days_away}d — riesgo de contagio sectorial"
                })
        except Exception:
            continue

    return sorted(upcoming, key=lambda x: x["days_away"])


# ══════════════════════════════════════════════════════════════════════════════
# SECTOR WIN RATES — from backtest DB (historical, informational only)
# ══════════════════════════════════════════════════════════════════════════════

SECTOR_WIN_RATE_PRIORITY = 58.0
SECTOR_WIN_RATE_OK       = 54.0
SECTOR_MIN_SIGNALS       = 5000

def get_sector_win_rates():
    """
    Query DB for historical win rates by sector.
    NOTE: These are from the backtest — informational only, not predictive.
    """
    try:
        from db import get_connection
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT a.sector,
                   COUNT(*) AS total,
                   ROUND(AVG(CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END)*100, 1) AS win_rate,
                   ROUND(AVG(o.pct_change_30d), 2) AS avg_return
            FROM analysis a
            JOIN outcomes o ON o.analysis_id = a.id
            WHERE a.is_backtest = TRUE
              AND a.sector IS NOT NULL
              AND o.would_have_profited IS NOT NULL
            GROUP BY a.sector
            HAVING COUNT(*) >= %s
            ORDER BY win_rate DESC;
        """, (SECTOR_MIN_SIGNALS,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        sectors = []
        for sector, total, win_rate, avg_return in rows:
            if win_rate is None:
                continue
            wr       = float(win_rate)
            priority = "PRIORITY"   if wr >= SECTOR_WIN_RATE_PRIORITY else \
                       "ACCEPTABLE" if wr >= SECTOR_WIN_RATE_OK       else "AVOID"
            sectors.append({
                "sector":     sector,
                "win_rate":   wr,
                "avg_return": float(avg_return) if avg_return else 0.0,
                "total":      int(total),
                "priority":   priority,
            })
        return sectors
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# VERDICT — dynamic based on VIX + SPY + macro events
# ══════════════════════════════════════════════════════════════════════════════

def get_verdict(vix, spy, macro_events, upcoming_earnings):
    """
    Dynamic verdict that considers VIX, SPY, AND upcoming macro events.

    Unlike the old version which only looked at VIX + SPY, this version
    penalizes when there are high-impact macro events in the next 3 days.
    """
    if vix is None or spy is None:
        return "INSUFFICIENT_DATA", "Could not retrieve market data."

    if vix["current"] >= VIX_FEAR:
        return "DO_NOT_TRADE", f"VIX extremo ({vix['current']:.1f}) — mercado en pánico."

    if spy["sma_status"] == "BELOW BOTH" and vix["current"] > VIX_ELEVATED:
        return "DO_NOT_TRADE", "SPY debajo de SMA50 y SMA200 con VIX elevado."

    # Check for imminent high-impact macro events (next 3 days)
    imminent_events = [e for e in macro_events
                       if e["days_away"] <= 3 and e["impact"] in ("HIGH", "VERY_HIGH")]

    # Check for imminent sector barómetro earnings (next 3 days)
    imminent_earnings = [e for e in upcoming_earnings if e["days_away"] <= 3]

    base_score = vix["score"] + spy["score"]

    if imminent_events or imminent_earnings:
        events_str = ", ".join([e["event"].split("—")[0].strip()
                                for e in imminent_events[:2]])
        earn_str   = ", ".join([e["ticker"] for e in imminent_earnings[:3]])
        warnings   = []
        if events_str:
            warnings.append(f"evento macro inminente ({events_str})")
        if earn_str:
            warnings.append(f"earnings próximos ({earn_str})")

        warning_text = " | ".join(warnings)

        if base_score >= 3:
            return "CAUTION", f"Macro favorable pero {warning_text} — ser selectivo."
        else:
            return "CAUTION", f"Condiciones mixtas con {warning_text} — reducir exposición."

    # Normal verdict without imminent events
    if base_score >= 3:
        return "FAVORABLE", "Condiciones macro fuertes para trading de opciones."
    elif base_score >= 1:
        return "CAUTION", "Mercado mixto — ser selectivo."
    elif base_score >= -1:
        return "CAUTION", "Condiciones inciertas — reducir tamaño de posición."
    else:
        return "DO_NOT_TRADE", "Condiciones macro adversas — esperar."


# ══════════════════════════════════════════════════════════════════════════════
# JSON OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def save_json(vix, spy, sectors, verdict, detail,
              macro_events, upcoming_earnings, recommended):

    os.makedirs(_REPORTS, exist_ok=True)

    priority   = [s["sector"] for s in sectors if s["priority"] == "PRIORITY"]
    acceptable = [s["sector"] for s in sectors if s["priority"] == "ACCEPTABLE"]
    avoid      = [s["sector"] for s in sectors if s["priority"] == "AVOID"]

    data = {
        "timestamp":          datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "verdict":            verdict,
        "verdict_detail":     detail,
        "vix": {
            "current":  vix["current"]  if vix else None,
            "level":    vix["level"]    if vix else None,
            "trend":    vix["trend"]    if vix else None,
            "avg_5d":   vix["avg_5d"]   if vix else None,
            "avg_10d":  vix["avg_10d"]  if vix else None,
        },
        "spy": {
            "price":      spy["price"]      if spy else None,
            "trend":      spy["trend"]      if spy else None,
            "pct_25d":    spy["pct_25d"]    if spy else None,
            "sma_status": spy["sma_status"] if spy else None,
            "sma50_dir":  spy["sma50_dir"]  if spy else None,
            "sma50":      spy["sma50"]      if spy else None,
            "sma200":     spy["sma200"]     if spy else None,
        },
        # Macro events this week
        "macro_events":       macro_events,
        # Earnings from sector watchlist in next 10 days
        "upcoming_earnings":  upcoming_earnings,
        # Historical sector win rates (informational — from backtest)
        "sectors": [
            {"sector":     s["sector"],
             "win_rate":   s["win_rate"],
             "avg_return": s["avg_return"],
             "priority":   s["priority"],
             "note":       "historical backtest data — informational only"}
            for s in sectors
        ],
        "priority_sectors":   priority,
        "acceptable_sectors": acceptable,
        "avoid_sectors":      avoid,
        "recommended_tickers": recommended,
    }

    with open(JSON_PATH, "w") as f:
        json.dump(data, f, indent=2)

    return data


# ══════════════════════════════════════════════════════════════════════════════
# HTML REPORT
# ══════════════════════════════════════════════════════════════════════════════

def generate_html(data):
    verdict           = data["verdict"]
    vix               = data["vix"]
    spy               = data["spy"]
    sectors           = data["sectors"]
    macro_events      = data.get("macro_events", [])
    upcoming_earnings = data.get("upcoming_earnings", [])
    timestamp         = data["timestamp"]

    v_color = "#22c55e" if verdict == "FAVORABLE" else \
              "#f59e0b" if verdict == "CAUTION"   else "#ef4444"
    v_emoji = "🟢" if verdict == "FAVORABLE" else \
              "🟡" if verdict == "CAUTION"   else "🔴"
    v_bg    = "#052e16" if verdict == "FAVORABLE" else \
              "#451a03" if verdict == "CAUTION"   else "#450a0a"

    vix_color = "#22c55e" if vix["level"] == "CALM"     else \
                "#f59e0b" if vix["level"] == "ELEVATED" else "#ef4444"
    vix_arrow = "↓" if vix["trend"] == "FALLING" else \
                "↑" if vix["trend"] == "RISING"  else "→"

    spy_color = "#22c55e" if spy["trend"] == "BULLISH" else \
                "#ef4444" if spy["trend"] == "BEARISH" else "#f59e0b"
    spy_pct   = f"{spy['pct_25d']:+.1f}%" if spy["pct_25d"] else "N/A"

    # Macro events HTML
    macro_html = ""
    if macro_events:
        for e in macro_events:
            impact_color = "#ef4444" if e["impact"] in ("HIGH", "VERY_HIGH") else "#f59e0b"
            macro_html += f"""
            <div style="background:#1a1a1a;border-radius:8px;padding:12px 14px;
                        margin-bottom:8px;border-left:3px solid {impact_color};">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span style="color:#f9fafb;font-weight:700;font-size:13px;">
                        {e['event']}
                    </span>
                    <span style="color:{impact_color};font-size:11px;font-weight:700;">
                        en {e['days_away']}d — {e['date']}
                    </span>
                </div>
                <div style="color:#6b7280;font-size:11px;margin-top:4px;line-height:1.5;">
                    {e['description']}
                </div>
            </div>"""
    else:
        macro_html = '<div style="color:#22c55e;font-size:13px;">✅ Sin eventos macro de alto impacto esta semana</div>'

    # Earnings HTML
    earnings_html = ""
    if upcoming_earnings:
        for e in upcoming_earnings:
            earnings_html += f"""
            <div style="background:#1a1a1a;border-radius:8px;padding:10px 14px;
                        margin-bottom:6px;border-left:3px solid #f59e0b;">
                <div style="display:flex;justify-content:space-between;">
                    <span style="color:#f9fafb;font-weight:700;">{e['ticker']}</span>
                    <span style="color:#9ca3af;font-size:11px;">{e['sector']}</span>
                    <span style="color:#f59e0b;font-size:11px;">en {e['days_away']}d — {e['date']}</span>
                </div>
            </div>"""
    else:
        earnings_html = '<div style="color:#22c55e;font-size:13px;">✅ Sin earnings de riesgo sectorial esta semana</div>'

    # Sector rows HTML
    sector_rows = ""
    for s in sectors:
        c    = "#22c55e" if s["priority"] == "PRIORITY"   else \
               "#f59e0b" if s["priority"] == "ACCEPTABLE" else "#6b7280"
        icon = "✅" if s["priority"] == "PRIORITY" else \
               "⚠️" if s["priority"] == "ACCEPTABLE" else "❌"
        bar  = min(100, int(s["win_rate"]))
        sector_rows += f"""
            <tr style="border-bottom:1px solid #1a1a1a;">
                <td style="padding:10px 16px;color:#f9fafb;font-size:13px;">
                    {icon}&nbsp;&nbsp;{s['sector']}
                </td>
                <td style="padding:10px 16px;">
                    <div style="display:flex;align-items:center;gap:8px;">
                        <div style="background:#1e1e1e;border-radius:4px;
                                    height:6px;width:80px;flex-shrink:0;">
                            <div style="background:{c};width:{bar}%;
                                        height:6px;border-radius:4px;"></div>
                        </div>
                        <span style="color:{c};font-weight:700;font-size:13px;">
                            {s['win_rate']:.1f}%
                        </span>
                    </div>
                </td>
                <td style="padding:10px 16px;color:#6b7280;font-size:12px;">
                    {s['priority']}
                </td>
            </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>Market Context — {timestamp}</title>
    <style>
        *{{box-sizing:border-box;margin:0;padding:0}}
        body{{background:#0d1117;color:#f9fafb;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             max-width:900px;margin:0 auto;padding:24px}}
        .card{{background:#111;border:1px solid #1e1e1e;border-radius:12px;
               padding:20px;margin-bottom:20px}}
        .label{{font-size:11px;color:#4b5563;letter-spacing:3px;
                text-transform:uppercase;margin-bottom:14px}}
        .big{{font-size:42px;font-weight:900;letter-spacing:-2px}}
        table{{width:100%;border-collapse:collapse}}
        th{{text-align:left;padding:8px 16px;font-size:10px;color:#4b5563;
            letter-spacing:2px;text-transform:uppercase}}
    </style>
</head>
<body>

<div style="display:flex;justify-content:space-between;align-items:center;
            margin-bottom:32px;padding-bottom:24px;border-bottom:1px solid #1e1e1e;">
    <div>
        <h1 style="font-size:32px;font-weight:900;letter-spacing:-1.5px;">Market Context</h1>
        <p style="color:#4b5563;font-size:13px;margin-top:4px;font-family:monospace;">{timestamp}</p>
    </div>
    <div style="background:{v_bg};border:2px solid {v_color};border-radius:12px;
                padding:16px 28px;text-align:center;">
        <div style="font-size:28px;margin-bottom:4px;">{v_emoji}</div>
        <div style="color:{v_color};font-weight:900;font-size:16px;">{verdict}</div>
        <div style="color:{v_color}88;font-size:11px;margin-top:4px;max-width:200px;line-height:1.4;">
            {data['verdict_detail']}
        </div>
    </div>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px;">
    <div class="card" style="border-color:{vix_color}44;">
        <div class="label">VIX — Volatility Index</div>
        <div class="big" style="color:{vix_color};">{vix['current']:.1f}</div>
        <div style="color:{vix_color};font-weight:700;margin-top:8px;">
            {vix['level']} {vix_arrow}
        </div>
        <div style="color:#6b7280;font-size:11px;margin-top:4px;font-family:monospace;">
            5d: {vix['avg_5d']:.1f} | 10d: {vix['avg_10d']:.1f}
        </div>
    </div>
    <div class="card" style="border-color:{spy_color}44;">
        <div class="label">SPY — Market Trend (25d)</div>
        <div class="big" style="color:{spy_color};">{spy_pct}</div>
        <div style="color:{spy_color};font-weight:700;margin-top:8px;">{spy['trend']}</div>
        <div style="color:#6b7280;font-size:11px;margin-top:4px;font-family:monospace;">
            ${spy['price']:.2f} | SMA50: ${spy['sma50']:.0f} ({spy['sma50_dir']}) | {spy['sma_status']}
        </div>
    </div>
</div>

<div class="card" style="border-color:#ef444444;">
    <div class="label">⚡ Eventos Macro Esta Semana</div>
    {macro_html}
</div>

<div class="card" style="border-color:#f59e0b44;">
    <div class="label">📅 Earnings de Riesgo Sectorial (próximos {EARNINGS_RISK_DAYS}d)</div>
    {earnings_html}
</div>

<div class="card">
    <div class="label">Sectores — Win Rate Histórico (backtest — solo referencia)</div>
    <div style="color:#4b5563;font-size:11px;margin-bottom:12px;">
        ⚠️ Estos datos vienen del backtest histórico. Son informativos, no predictivos.
        El análisis real lo hace Claude al interpretar el scanner report.
    </div>
    <table>
        <thead>
            <tr>
                <th>SECTOR</th>
                <th>WIN RATE</th>
                <th>PRIORIDAD</th>
            </tr>
        </thead>
        <tbody>{sector_rows}</tbody>
    </table>
</div>

<div style="text-align:center;color:#374151;font-size:11px;
            padding:24px 0;border-top:1px solid #1e1e1e;font-family:monospace;">
    {timestamp} · Market Context · Options Trading System
</div>

</body>
</html>"""

    os.makedirs(_REPORTS, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    return REPORT_PATH


def generate_ai_summary(data):
    """
    Generate ultra-compact AI-friendly market context summary.
    Target: ~15 lines. Saved to reports/market_context_ai.md
    """
    verdict  = data["verdict"]
    detail   = data["verdict_detail"]
    vix      = data["vix"]
    spy      = data["spy"]
    events   = data.get("macro_events", [])
    earnings = data.get("upcoming_earnings", [])
    ts       = data["timestamp"]

    lines = [f"MARKET CONTEXT — {ts}", ""]

    lines.append(f"VERDICT: {verdict} — {detail}")
    lines.append(
        f"VIX: {vix['current']:.1f} {vix['level']} {vix['trend']} "
        f"(5d avg: {vix['avg_5d']:.1f})"
    )
    lines.append(
        f"SPY: ${spy['price']:.2f} | {spy['trend']} {spy['pct_25d']:+.1f}% 25d | "
        f"{spy['sma_status']} | SMA50 {spy['sma50_dir']}"
    )
    lines.append("")

    if events:
        lines.append("MACRO EVENTS THIS WEEK:")
        for e in events:
            lines.append(
                f"  [{e['impact']}] {e['event']} — en {e['days_away']}d ({e['date']})"
            )
            lines.append(f"  → {e['description']}")
    else:
        lines.append("MACRO EVENTS: ninguno de alto impacto esta semana ✅")

    lines.append("")

    if earnings:
        lines.append("EARNINGS RISK (próximos 10d):")
        for e in earnings:
            lines.append(
                f"  {e['ticker']} ({e['sector']}) — en {e['days_away']}d ({e['date']})"
            )
    else:
        lines.append("EARNINGS RISK: ninguno en los próximos 10d ✅")

    lines.append("")
    lines.append(f"---")
    lines.append(f"Generated {ts}")

    content = "\n".join(lines)
    os.makedirs(_REPORTS, exist_ok=True)
    with open(AI_SUMMARY_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    return AI_SUMMARY_PATH


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run():
    print(f"\n  Fetching VIX...", end=" ", flush=True)
    vix = get_vix()
    print("OK" if vix else "FAILED")

    print(f"  Fetching SPY...", end=" ", flush=True)
    spy = get_spy_context()
    print("OK" if spy else "FAILED")

    print(f"  Checking macro calendar...", end=" ", flush=True)
    macro_events = get_macro_events()
    print(f"{len(macro_events)} event(s)")

    print(f"  Fetching earnings watchlist...", end=" ", flush=True)
    upcoming_earnings = get_upcoming_earnings()
    print(f"{len(upcoming_earnings)} upcoming in {EARNINGS_RISK_DAYS}d")

    print(f"  Fetching sector win rates...", end=" ", flush=True)
    sectors = get_sector_win_rates()
    print(f"{len(sectors)} sectors")

    verdict, detail = get_verdict(vix, spy, macro_events, upcoming_earnings)

    # Recommended tickers (legacy — from backtest context mode)
    recommended = []

    data        = save_json(vix, spy, sectors, verdict, detail,
                            macro_events, upcoming_earnings, recommended)
    report_path = generate_html(data)
    ai_path     = generate_ai_summary(data)

    print(f"\n{'=' * 55}")
    print(f"  MARKET CONTEXT — {data['timestamp']}")
    print(f"{'=' * 55}")
    print(f"  Verdict:  {verdict}")
    print(f"  Detail:   {detail}")
    if vix:
        print(f"  VIX:      {vix['current']:.1f} ({vix['level']})")
    if spy:
        print(f"  SPY:      {spy['trend']} ({spy['pct_25d']:+.1f}% 25d)")
    if macro_events:
        print(f"  ⚠️  Macro eventos esta semana:")
        for e in macro_events:
            print(f"      - {e['event']} en {e['days_away']}d ({e['impact']})")
    if upcoming_earnings:
        print(f"  ⚠️  Earnings de riesgo:")
        for e in upcoming_earnings:
            print(f"      - {e['ticker']} ({e['sector']}) en {e['days_away']}d")
    print(f"  Report:   {report_path}")
    print(f"  JSON:     {JSON_PATH}")
    print(f"  AI:       {ai_path}")
    print(f"{'=' * 55}\n")

    _open_browser(report_path)

    return data


if __name__ == "__main__":
    run()