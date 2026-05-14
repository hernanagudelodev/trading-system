"""
market_context.py
=================
Daily macro analysis for Bull Call Spread system.

Answers ONE question before running the scanner:
    Is it worth opening positions today?

Analyzes two levels:
    1. General market  — VIX level/trend, SPY position vs SMAs and momentum
    2. Sectors         — historical win rates from DB, which to prioritize today

Output:
    🟢 FAVORABLE   — good conditions, recommended sectors listed
    🟡 CAUTION     — uncertain market, be very selective
    🔴 DO NOT TRADE — adverse conditions, do not open new positions

Usage:
    python market_context.py

Dependencies:
    yfinance            → market data
    db.py               → historical win rates by sector
    sp500_tickers.json  → full S&P 500 ticker list with sectors
    .env                → DATABASE_URL
"""

import os
import sys
import json
import warnings
import datetime
import io

import yfinance as yf
import pandas as pd
from dotenv import load_dotenv

from db import get_connection

warnings.filterwarnings("ignore")
load_dotenv()


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# VIX thresholds
VIX_CALM        = 18   # below → calm market, favorable for spreads
VIX_ELEVATED    = 25   # between calm and elevated → caution
VIX_FEAR        = 35   # above → extreme fear, do not trade

# Sector win rate thresholds
SECTOR_WIN_RATE_PRIORITY = 58.0  # above → priority sector
SECTOR_WIN_RATE_OK       = 54.0  # between ok and priority → acceptable

# Minimum historical signals for a sector win rate to be reliable
SECTOR_MIN_SIGNALS = 5000

# Max tickers to recommend per sector
MAX_TICKERS_PER_SECTOR = 8

# Path to S&P 500 JSON
SP500_JSON = os.path.join(os.path.dirname(__file__), "sp500_tickers.json")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _silence_stderr():
    old = sys.stderr
    sys.stderr = io.StringIO()
    return old

def _restore_stderr(old):
    sys.stderr = old

def _sma(series, n):
    return series.rolling(window=n).mean().iloc[-1]

def _pct_change(series, n):
    if len(series) < n + 1:
        return None
    return (series.iloc[-1] - series.iloc[-n]) / series.iloc[-n] * 100


# ══════════════════════════════════════════════════════════════════════════════
# LEVEL 1 — GENERAL MARKET
# ══════════════════════════════════════════════════════════════════════════════

def get_vix():
    """
    Fetch current VIX level and 5-day trend.

    Returns:
        dict with keys: current, avg_5d, avg_10d, trend, level, score
        or None if data unavailable
    """
    old = _silence_stderr()
    try:
        data = yf.download("^VIX", period="20d", interval="1d",
                           progress=False, auto_adjust=True)
        _restore_stderr(old)

        if data.empty:
            return None

        closes  = data["Close"].squeeze().dropna()
        current = float(closes.iloc[-1])
        avg_5d  = float(closes.iloc[-5:].mean())
        avg_10d = float(closes.iloc[-10:].mean())

        if current < avg_5d * 0.95:
            trend = "FALLING ↓"
        elif current > avg_5d * 1.05:
            trend = "RISING ↑"
        else:
            trend = "STABLE →"

        if current < VIX_CALM:
            level = "CALM"
            score = 2
        elif current < VIX_ELEVATED:
            level = "ELEVATED"
            score = 1
        elif current < VIX_FEAR:
            level = "HIGH"
            score = -1
        else:
            level = "EXTREME"
            score = -2

        return {
            "current": round(current, 2),
            "avg_5d":  round(avg_5d, 2),
            "avg_10d": round(avg_10d, 2),
            "trend":   trend,
            "level":   level,
            "score":   score,
        }
    except Exception:
        _restore_stderr(old)
        return None


def get_spy_context():
    """
    Analyze SPY: trend, position vs SMAs, SMA50 direction.

    Returns:
        dict with keys: price, sma50, sma200, sma_status, sma50_dir,
                        trend, pct_25d, score
        or None if data unavailable
    """
    old = _silence_stderr()
    try:
        data = yf.download("SPY", period="1y", interval="1d",
                           progress=False, auto_adjust=True)
        _restore_stderr(old)

        if data.empty or len(data) < 50:
            return None

        closes = data["Close"].squeeze().dropna()
        price  = float(closes.iloc[-1])

        sma50  = float(_sma(closes, 50))
        sma200 = float(_sma(closes, 200)) if len(closes) >= 200 else None

        # 25-day price trend
        pct_25d = _pct_change(closes, 25)

        # Position vs SMAs
        above_50  = price > sma50
        above_200 = price > sma200 if sma200 is not None else None

        if above_50 and above_200:
            sma_status = "ABOVE BOTH"
            sma_score  = 2
        elif above_50:
            sma_status = "ABOVE SMA50 ONLY"
            sma_score  = 1
        elif above_200:
            sma_status = "BELOW SMA50, ABOVE SMA200"
            sma_score  = 0
        else:
            sma_status = "BELOW BOTH"
            sma_score  = -2

        # SMA50 direction over last 5 days
        sma50_series = closes.rolling(50).mean()
        if len(sma50_series.dropna()) >= 5:
            sma50_5d_ago = float(sma50_series.dropna().iloc[-5])
            if sma50 > sma50_5d_ago * 1.001:
                sma50_dir   = "RISING ↑"
                sma50_score = 1
            elif sma50 < sma50_5d_ago * 0.999:
                sma50_dir   = "FALLING ↓"
                sma50_score = -1
            else:
                sma50_dir   = "FLAT →"
                sma50_score = 0
        else:
            sma50_dir   = "UNKNOWN"
            sma50_score = 0

        # Overall trend
        if pct_25d is not None:
            if pct_25d > 2:
                trend       = f"BULLISH ({pct_25d:+.1f}% 25d)"
                trend_score = 1
            elif pct_25d < -2:
                trend       = f"BEARISH ({pct_25d:+.1f}% 25d)"
                trend_score = -1
            else:
                trend       = f"SIDEWAYS ({pct_25d:+.1f}% 25d)"
                trend_score = 0
        else:
            trend       = "UNKNOWN"
            trend_score = 0

        total_score = sma_score + sma50_score + trend_score

        return {
            "price":      round(price, 2),
            "sma50":      round(sma50, 2),
            "sma200":     round(sma200, 2) if sma200 is not None else None,
            "sma_status": sma_status,
            "sma50_dir":  sma50_dir,
            "trend":      trend,
            "pct_25d":    round(pct_25d, 2) if pct_25d is not None else None,
            "score":      total_score,
        }
    except Exception:
        _restore_stderr(old)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# LEVEL 2 — SECTORS
# ══════════════════════════════════════════════════════════════════════════════

def get_sector_win_rates():
    """
    Read historical win rates by sector from DB.
    Only includes sectors with enough signals to be statistically reliable.

    Returns:
        list of dicts with keys: sector, win_rate, avg_return, viable, total, priority
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT
            a.sector,
            COUNT(*)                                                          AS total,
            ROUND(AVG(CASE WHEN o.would_have_profited
                THEN 1.0 ELSE 0.0 END) * 100, 1)                             AS win_rate,
            ROUND(AVG(o.pct_change_30d)::numeric, 2)                         AS avg_return,
            SUM(CASE WHEN a.verdict = 'VIABLE' THEN 1 ELSE 0 END)            AS viable_signals
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
    for sector, total, win_rate, avg_return, viable in rows:
        win_rate   = float(win_rate)
        avg_return = float(avg_return)

        if win_rate >= SECTOR_WIN_RATE_PRIORITY:
            priority = "PRIORITY"
        elif win_rate >= SECTOR_WIN_RATE_OK:
            priority = "ACCEPTABLE"
        else:
            priority = "AVOID"

        sectors.append({
            "sector":     sector,
            "win_rate":   win_rate,
            "avg_return": avg_return,
            "viable":     int(viable),
            "total":      int(total),
            "priority":   priority,
        })

    return sectors


def get_recommended_tickers(max_per_sector=MAX_TICKERS_PER_SECTOR):
    """
    Build a recommended ticker list based on priority sectors.

    Reads sp500_tickers.json to get the full sector → ticker mapping,
    then queries the DB to rank tickers within each priority sector
    by their historical VIABLE accuracy.

    Only includes tickers with at least 3 VIABLE signals historically
    so the accuracy metric is meaningful.

    Args:
        max_per_sector (int) — max tickers to include per sector

    Returns:
        list of ticker strings ordered by sector priority then accuracy,
        or None if data unavailable
    """
    if not os.path.exists(SP500_JSON):
        return None

    with open(SP500_JSON, "r") as f:
        sp500 = json.load(f)

    # Get priority sectors from DB
    sectors = get_sector_win_rates()
    priority_sectors = [s["sector"] for s in sectors if s["priority"] == "PRIORITY"]

    if not priority_sectors:
        return None

    # Query DB for tickers in priority sectors ranked by VIABLE accuracy
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT
            a.ticker,
            a.sector,
            SUM(CASE WHEN a.verdict = 'VIABLE' THEN 1 ELSE 0 END)             AS viable_count,
            ROUND(AVG(CASE WHEN a.verdict = 'VIABLE'
                          AND o.would_have_profited IS NOT NULL
                THEN CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END
                END) * 100, 1)                                                 AS viable_accuracy
        FROM analysis a
        LEFT JOIN outcomes o ON o.analysis_id = a.id
        WHERE a.is_backtest = TRUE
          AND a.sector = ANY(%s)
        GROUP BY a.ticker, a.sector
        HAVING SUM(CASE WHEN a.verdict = 'VIABLE' THEN 1 ELSE 0 END) >= 3
        ORDER BY viable_accuracy DESC NULLS LAST, viable_count DESC;
    """, (priority_sectors,))

    rows = cur.fetchall()
    cur.close()
    conn.close()

    # Pick top N per sector preserving priority order
    tickers_by_sector = {s: [] for s in priority_sectors}
    for ticker, sector, viable_count, viable_accuracy in rows:
        if sector in tickers_by_sector:
            if len(tickers_by_sector[sector]) < max_per_sector:
                tickers_by_sector[sector].append(ticker)

    # Flatten in sector priority order, deduplicate
    recommended = []
    seen = set()
    for sector in priority_sectors:
        for ticker in tickers_by_sector.get(sector, []):
            if ticker not in seen:
                seen.add(ticker)
                recommended.append(ticker)

    return recommended if recommended else None


# ══════════════════════════════════════════════════════════════════════════════
# FINAL VERDICT
# ══════════════════════════════════════════════════════════════════════════════

def get_verdict(vix, spy):
    """
    Combine VIX and SPY scores into a final trading verdict.

    Hard veto conditions override score:
        - VIX >= VIX_FEAR → DO NOT TRADE
        - SPY below both SMAs + VIX elevated → DO NOT TRADE

    Returns:
        (verdict_str, detail_str)
    """
    if vix is None or spy is None:
        return "⚠️  INSUFFICIENT DATA", "Could not retrieve market data."

    # Hard veto conditions
    if vix["current"] >= VIX_FEAR:
        return "🔴 DO NOT TRADE", f"Extreme VIX ({vix['current']:.1f}) — market in panic mode."

    if spy["sma_status"] == "BELOW BOTH" and vix["current"] > VIX_ELEVATED:
        return "🔴 DO NOT TRADE", "SPY below SMA50 and SMA200 with elevated VIX — confirmed downtrend."

    # Score-based verdict
    total_score = vix["score"] + spy["score"]

    if total_score >= 3:
        return "🟢 FAVORABLE", "Strong macro conditions for Bull Call Spreads."
    elif total_score >= 1:
        return "🟡 CAUTION", "Mixed market — be selective, stick to priority sectors only."
    elif total_score >= -1:
        return "🟡 CAUTION", "Uncertain conditions — reduce position size."
    else:
        return "🔴 DO NOT TRADE", "Adverse macro conditions — wait for better setup."


# ══════════════════════════════════════════════════════════════════════════════
# REPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_report():
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'═' * 62}")
    print(f"  MARKET CONTEXT — {timestamp}")
    print(f"{'═' * 62}")

    # ── VIX ──────────────────────────────────────────────────────────────────
    print(f"\n  VIX")
    print(f"  {'─' * 42}")

    vix = get_vix()
    if vix:
        icon = "✅" if vix["score"] > 0 else "⚠️ " if vix["score"] == 0 else "❌"
        print(f"  {icon} Level:  {vix['current']:.1f} — {vix['level']}")
        print(f"     Trend:  {vix['trend']}  (5d avg: {vix['avg_5d']:.1f}  |  10d avg: {vix['avg_10d']:.1f})")
    else:
        print("  ⚠️  Could not retrieve VIX data")

    # ── SPY ───────────────────────────────────────────────────────────────────
    print(f"\n  SPY")
    print(f"  {'─' * 42}")

    spy = get_spy_context()
    if spy:
        icon = "✅" if spy["score"] > 0 else "⚠️ " if spy["score"] == 0 else "❌"
        sma200_str = f"${spy['sma200']:.2f}" if spy["sma200"] else "N/A"
        print(f"  {icon} Price:    ${spy['price']:.2f}")
        print(f"     Trend:    {spy['trend']}")
        print(f"     SMAs:     {spy['sma_status']}")
        print(f"     SMA50:    ${spy['sma50']:.2f} ({spy['sma50_dir']})  |  SMA200: {sma200_str}")
    else:
        print("  ⚠️  Could not retrieve SPY data")

    # ── SECTORS ──────────────────────────────────────────────────────────────
    print(f"\n  SECTORS  (historical win rate from backtest DB)")
    print(f"  {'─' * 42}")
    print(f"  {'SECTOR':<35} {'WIN RATE':>9}  {'AVG RET':>8}  PRIORITY")
    print(f"  {'─' * 42}")

    sectors = get_sector_win_rates()

    if sectors:
        for s in sectors:
            icon = "✅" if s["priority"] == "PRIORITY" else \
                   "⚠️ " if s["priority"] == "ACCEPTABLE" else "❌"
            print(f"  {icon} {s['sector']:<33} {s['win_rate']:>8.1f}%  {s['avg_return']:>+7.2f}%  {s['priority']}")
    else:
        print("  No sector data found in DB.")

    # ── VERDICT ───────────────────────────────────────────────────────────────
    verdict, detail = get_verdict(vix, spy)

    print(f"\n{'═' * 62}")
    print(f"  VERDICT:  {verdict}")
    print(f"  {detail}")

    if "DO NOT TRADE" not in verdict:
        recommended = get_recommended_tickers()

        if recommended:
            print(f"\n  Recommended tickers for scanner ({len(recommended)} total):")
            for i in range(0, len(recommended), 8):
                chunk = recommended[i:i + 8]
                print(f"    {' '.join(chunk)}")
            print(f"\n  Run scanner with:")
            print(f"    python scanner.py --context")
            print(f"    python scanner.py --tickers {' '.join(recommended)}")
        else:
            print(f"\n  ⚠️  Could not build ticker list from DB — run scanner manually")

    print(f"\n{'═' * 62}\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print_report()