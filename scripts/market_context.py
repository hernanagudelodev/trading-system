"""
market_context.py
=================
Daily macro analysis for the options trading system.

Analyzes two levels:
    1. General market  — VIX level/trend, SPY position vs SMAs and momentum
    2. Sectors         — historical win rates from DB, which to prioritize today

Output:
    - market_context_report.html  — visual dashboard, opens in browser automatically
    - market_context.json         — structured raw data for scanner.py to consume

NO AI in this script — raw data only.
The scanner reads market_context.json and passes everything to its AI
for a single coherent interpretation.

Usage:
    python market_context.py

Dependencies:
    yfinance    → market data
    db.py       → historical win rates by sector
    .env        → DATABASE_URL
"""

import os
import sys
import json
import warnings
import datetime
import io
import webbrowser

import yfinance as yf
import pandas as pd
from dotenv import load_dotenv

from db import get_connection

warnings.filterwarnings("ignore")
load_dotenv()


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

VIX_CALM        = 18
VIX_ELEVATED    = 25
VIX_FEAR        = 35

SECTOR_WIN_RATE_PRIORITY = 58.0
SECTOR_WIN_RATE_OK       = 54.0
SECTOR_MIN_SIGNALS       = 5000
MAX_TICKERS_PER_SECTOR   = 8

SP500_JSON  = os.path.join(os.path.dirname(__file__), "sp500_tickers.json")
REPORT_PATH = os.path.join(os.path.dirname(__file__), "market_context_report.html")
JSON_PATH   = os.path.join(os.path.dirname(__file__), "market_context.json")


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
# MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════

def get_vix():
    old = _silence()
    try:
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
        sma50_dir, sma50_score = "FLAT", 0
        if len(sma50_series.dropna()) >= 5:
            sma50_5d = float(sma50_series.dropna().iloc[-5])
            if sma50 > sma50_5d * 1.001:
                sma50_dir, sma50_score = "RISING", 1
            elif sma50 < sma50_5d * 0.999:
                sma50_dir, sma50_score = "FALLING", -1

        if pct_25d and pct_25d > 2:
            trend, trend_score = "BULLISH", 1
        elif pct_25d and pct_25d < -2:
            trend, trend_score = "BEARISH", -1
        else:
            trend, trend_score = "SIDEWAYS", 0

        return {
            "price":      round(price, 2),
            "sma50":      round(sma50, 2),
            "sma200":     round(sma200, 2) if sma200 else None,
            "sma_status": sma_status,
            "sma50_dir":  sma50_dir,
            "trend":      trend,
            "pct_25d":    round(pct_25d, 2) if pct_25d else None,
            "score":      sma_score + sma50_score + trend_score,
        }
    except Exception:
        _restore(old)
        return None


def get_sector_win_rates():
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT a.sector,
               COUNT(*) AS total,
               ROUND(AVG(CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END)*100,1) AS win_rate,
               ROUND(AVG(o.pct_change_30d)::numeric,2) AS avg_return
        FROM analysis a
        JOIN outcomes o ON o.analysis_id = a.id
        WHERE a.is_backtest = TRUE AND a.sector IS NOT NULL
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
        wr = float(win_rate)
        priority = "PRIORITY"   if wr >= SECTOR_WIN_RATE_PRIORITY else \
                   "ACCEPTABLE" if wr >= SECTOR_WIN_RATE_OK       else "AVOID"
        sectors.append({"sector": sector, "win_rate": wr,
                        "avg_return": float(avg_return), "priority": priority})
    return sectors


def get_recommended_tickers(sectors, max_per_sector=MAX_TICKERS_PER_SECTOR):
    if not os.path.exists(SP500_JSON):
        return []

    priority_sectors = [s["sector"] for s in sectors if s["priority"] == "PRIORITY"]
    if not priority_sectors:
        return []

    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT a.ticker, a.sector,
               SUM(CASE WHEN a.verdict='VIABLE' THEN 1 ELSE 0 END) AS viable_count,
               ROUND(AVG(CASE WHEN a.verdict='VIABLE' AND o.would_have_profited IS NOT NULL
                   THEN CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END END)*100,1) AS accuracy
        FROM analysis a
        LEFT JOIN outcomes o ON o.analysis_id = a.id
        WHERE a.is_backtest = TRUE AND a.sector = ANY(%s)
        GROUP BY a.ticker, a.sector
        HAVING SUM(CASE WHEN a.verdict='VIABLE' THEN 1 ELSE 0 END) >= 3
        ORDER BY accuracy DESC NULLS LAST, viable_count DESC;
    """, (priority_sectors,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    by_sector = {s: [] for s in priority_sectors}
    for ticker, sector, viable, accuracy in rows:
        if sector in by_sector and len(by_sector[sector]) < max_per_sector:
            by_sector[sector].append(ticker)

    recommended = []
    seen = set()
    for sector in priority_sectors:
        for t in by_sector.get(sector, []):
            if t not in seen:
                seen.add(t)
                recommended.append(t)
    return recommended


def get_verdict(vix, spy):
    if vix is None or spy is None:
        return "INSUFFICIENT_DATA", "Could not retrieve market data."
    if vix["current"] >= VIX_FEAR:
        return "DO_NOT_TRADE", f"Extreme VIX ({vix['current']:.1f}) — market in panic."
    if spy["sma_status"] == "BELOW BOTH" and vix["current"] > VIX_ELEVATED:
        return "DO_NOT_TRADE", "SPY below SMA50 and SMA200 with elevated VIX."

    total = vix["score"] + spy["score"]
    if total >= 3:    return "FAVORABLE",   "Strong macro conditions for options trading."
    elif total >= 1:  return "CAUTION",     "Mixed market — be selective."
    elif total >= -1: return "CAUTION",     "Uncertain conditions — reduce position size."
    else:             return "DO_NOT_TRADE", "Adverse macro conditions — wait."


# ══════════════════════════════════════════════════════════════════════════════
# JSON OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def save_json(vix, spy, sectors, verdict, detail, recommended):
    priority   = [s["sector"] for s in sectors if s["priority"] == "PRIORITY"]
    acceptable = [s["sector"] for s in sectors if s["priority"] == "ACCEPTABLE"]
    avoid      = [s["sector"] for s in sectors if s["priority"] == "AVOID"]

    data = {
        "timestamp":           datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "verdict":             verdict,
        "verdict_detail":      detail,
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
        "sectors": [
            {"sector": s["sector"], "win_rate": s["win_rate"],
             "avg_return": s["avg_return"], "priority": s["priority"]}
            for s in sectors
        ],
        "priority_sectors":    priority,
        "acceptable_sectors":  acceptable,
        "avoid_sectors":       avoid,
        "recommended_tickers": recommended,
    }

    with open(JSON_PATH, "w") as f:
        json.dump(data, f, indent=2)

    return data


# ══════════════════════════════════════════════════════════════════════════════
# HTML REPORT
# ══════════════════════════════════════════════════════════════════════════════

def generate_html(data):
    verdict     = data["verdict"]
    vix         = data["vix"]
    spy         = data["spy"]
    sectors     = data["sectors"]
    recommended = data["recommended_tickers"]
    timestamp   = data["timestamp"]

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
    sma200_str = f"${spy['sma200']:.0f}" if spy["sma200"] else "N/A"

    # Sector rows
    sector_rows = ""
    for s in sectors:
        c    = "#22c55e" if s["priority"] == "PRIORITY"   else \
               "#f59e0b" if s["priority"] == "ACCEPTABLE" else "#6b7280"
        icon = "✅" if s["priority"] == "PRIORITY"   else \
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
                <td style="padding:10px 16px;color:#9ca3af;font-size:13px;">
                    {s['avg_return']:+.2f}%
                </td>
                <td style="padding:10px 16px;">
                    <span style="background:{c}22;color:{c};border-radius:4px;
                                 padding:2px 8px;font-size:11px;font-weight:700;">
                        {s['priority']}
                    </span>
                </td>
            </tr>"""

    # Ticker chips
    chips = "".join(f"""
        <span style="background:#1e2d1e;border:1px solid #22c55e44;color:#22c55e;
                     border-radius:6px;padding:4px 10px;font-size:12px;
                     font-weight:700;font-family:monospace;">{t}</span>"""
        for t in recommended) or '<span style="color:#6b7280;">No tickers available</span>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>Market Context — {timestamp}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;700;900&display=swap" rel="stylesheet">
    <style>
        *{{box-sizing:border-box;margin:0;padding:0}}
        body{{background:#0a0a0a;color:#f9fafb;font-family:'DM Sans',sans-serif;
              min-height:100vh;padding:32px 24px}}
        .wrap{{max-width:960px;margin:0 auto}}
        .card{{background:#111;border:1px solid #1e1e1e;border-radius:12px;
               padding:24px;margin-bottom:20px}}
        .label{{font-size:10px;font-weight:700;letter-spacing:3px;color:#6b7280;
                text-transform:uppercase;margin-bottom:14px;padding-bottom:10px;
                border-bottom:1px solid #1e1e1e}}
        .big{{font-size:40px;font-weight:900;letter-spacing:-2px;line-height:1}}
        table{{width:100%;border-collapse:collapse}}
        th{{padding:8px 16px;text-align:left;color:#6b7280;font-size:10px;
            letter-spacing:2px;border-bottom:1px solid #1e1e1e}}
    </style>
</head>
<body>
<div class="wrap">

    <!-- Header -->
    <div style="display:flex;justify-content:space-between;align-items:center;
                margin-bottom:28px;padding-bottom:20px;border-bottom:1px solid #1e1e1e;
                flex-wrap:wrap;gap:16px;">
        <div>
            <div style="font-size:11px;color:#6b7280;letter-spacing:3px;
                        text-transform:uppercase;margin-bottom:6px;">
                Options Trading System
            </div>
            <h1 style="font-size:32px;font-weight:900;letter-spacing:-1.5px;">
                Market Context
            </h1>
            <p style="color:#4b5563;font-size:13px;margin-top:4px;
                      font-family:'DM Mono',monospace;">
                {timestamp}
            </p>
        </div>
        <div style="background:{v_bg};border:2px solid {v_color};
                    border-radius:12px;padding:16px 28px;text-align:center;
                    min-width:180px;">
            <div style="font-size:28px;margin-bottom:4px;">{v_emoji}</div>
            <div style="color:{v_color};font-weight:900;font-size:16px;
                        letter-spacing:2px;">{verdict}</div>
            <div style="color:{v_color}88;font-size:11px;margin-top:4px;
                        max-width:160px;line-height:1.4;">
                {data['verdict_detail']}
            </div>
        </div>
    </div>

    <!-- VIX + SPY -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px;">

        <div class="card" style="border-color:{vix_color}44;">
            <div class="label">VIX — Volatility Index</div>
            <div style="display:flex;align-items:flex-end;gap:16px;margin-bottom:16px;">
                <div class="big" style="color:{vix_color};">{vix['current']:.1f}</div>
                <div style="margin-bottom:4px;">
                    <div style="color:{vix_color};font-weight:700;font-size:15px;">
                        {vix['level']} {vix_arrow}
                    </div>
                    <div style="color:#6b7280;font-size:12px;margin-top:2px;
                                font-family:'DM Mono',monospace;">
                        5d: {vix['avg_5d']:.1f} &nbsp;|&nbsp; 10d: {vix['avg_10d']:.1f}
                    </div>
                </div>
            </div>
            <div style="background:#1a1a1a;border-radius:8px;padding:10px 14px;
                        display:flex;justify-content:space-between;font-size:11px;
                        font-family:'DM Mono',monospace;">
                <span style="color:#22c55e;">CALM &lt;{VIX_CALM}</span>
                <span style="color:#f59e0b;">ELEVATED &lt;{VIX_ELEVATED}</span>
                <span style="color:#ef4444;">EXTREME &gt;{VIX_FEAR}</span>
            </div>
        </div>

        <div class="card" style="border-color:{spy_color}44;">
            <div class="label">SPY — Market Trend</div>
            <div style="display:flex;align-items:flex-end;gap:16px;margin-bottom:16px;">
                <div class="big" style="color:{spy_color};">{spy_pct}</div>
                <div style="margin-bottom:4px;">
                    <div style="color:{spy_color};font-weight:700;font-size:15px;">
                        {spy['trend']} (25d)
                    </div>
                    <div style="color:#6b7280;font-size:12px;margin-top:2px;
                                font-family:'DM Mono',monospace;">
                        ${spy['price']:.2f}
                    </div>
                </div>
            </div>
            <div style="background:#1a1a1a;border-radius:8px;padding:10px 14px;
                        display:flex;justify-content:space-between;font-size:11px;
                        font-family:'DM Mono',monospace;color:#9ca3af;">
                <span>SMA50: ${spy['sma50']:.0f} ({spy['sma50_dir']})</span>
                <span>SMA200: {sma200_str}</span>
                <span style="color:#f9fafb;font-weight:700;">{spy['sma_status']}</span>
            </div>
        </div>
    </div>

    <!-- Sectors -->
    <div class="card" style="margin-bottom:20px;">
        <div class="label">Sectors — Historical Win Rate (backtest DB)</div>
        <table>
            <thead>
                <tr>
                    <th>SECTOR</th>
                    <th>WIN RATE</th>
                    <th>AVG RETURN</th>
                    <th>PRIORITY</th>
                </tr>
            </thead>
            <tbody>{sector_rows}</tbody>
        </table>
    </div>

    <!-- Recommended tickers -->
    <div class="card">
        <div class="label">
            Recommended Tickers — {len(recommended)} from priority sectors
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px;">
            {chips}
        </div>
        <div style="background:#1a1a1a;border-radius:8px;padding:14px 16px;">
            <div style="color:#4b5563;font-size:10px;letter-spacing:2px;
                        text-transform:uppercase;margin-bottom:8px;">
                Run scanner
            </div>
            <code style="color:#22c55e;font-size:13px;font-family:'DM Mono',monospace;">
                python scanner.py --context
            </code>
        </div>
    </div>

    <div style="text-align:center;color:#374151;font-size:11px;
                padding:24px 0;border-top:1px solid #1e1e1e;margin-top:8px;
                font-family:'DM Mono',monospace;">
        {timestamp} &nbsp;·&nbsp; Raw data only &nbsp;·&nbsp;
        AI interpretation runs in scanner.py
    </div>

</div>
</body>
</html>"""

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    return REPORT_PATH


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run():
    vix     = get_vix()
    spy     = get_spy_context()
    sectors = get_sector_win_rates()

    verdict, detail = get_verdict(vix, spy)
    recommended = get_recommended_tickers(sectors) if "DO_NOT_TRADE" not in verdict else []

    data        = save_json(vix, spy, sectors, verdict, detail, recommended)
    report_path = generate_html(data)

    # Terminal summary
    print(f"\n{'=' * 50}")
    print(f"  MARKET CONTEXT — {data['timestamp']}")
    print(f"{'=' * 50}")
    print(f"  Verdict:  {verdict}")
    if vix:
        print(f"  VIX:      {vix['current']:.1f} ({vix['level']})")
    if spy:
        print(f"  SPY:      {spy['trend']} ({spy['pct_25d']:+.1f}% 25d)")
    print(f"  Priority: {', '.join(data['priority_sectors']) or 'None'}")
    print(f"  Tickers:  {len(recommended)} recommended")
    print(f"  Report:   {report_path}")
    print(f"  JSON:     {JSON_PATH}")
    print(f"{'=' * 50}\n")

    webbrowser.open(f"file:///{report_path.replace(os.sep, '/')}")

    return data


if __name__ == "__main__":
    run()