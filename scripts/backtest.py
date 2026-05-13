"""
backtest.py
===========
Historical simulation of the Bull Call Spread analysis system.

Simulates 1 year of daily market analysis using historical price data
from yfinance. For each trading day, runs criteria + scoring as if
operating on that date, then checks 30 days later if the trade
would have been profitable.

Features:
    - Checkpoint system: saves progress to DB — resumable after interruption
    - Retry logic: handles network failures gracefully
    - Progress tracking: shows ETA and completion percentage
    - Detailed logging: errors saved to backtest_errors.log
    - Outcomes: automatically records what happened 30 days after each signal
    - S&P 500: reads full ticker list from sp500_tickers.json if present

Usage:
    python backtest.py                          → run full 1-year backtest
    python backtest.py --tickers AAPL MSFT      → specific tickers only
    python backtest.py --resume                 → continue interrupted backtest
    python backtest.py --days 180               → last 180 days only
    python backtest.py --summary                → show results without running

Dependencies:
    criteria.py         → market data calculation
    scoring.py          → scoring and verdict
    db.py               → save results to PostgreSQL
    sp500_tickers.json  → optional full S&P 500 list with sectors
    .env                → DATABASE_URL
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, date, timedelta

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from scoring import score_criteria
from db import (
    save_backtest_analysis,
    get_backtest_progress,
    get_backtest_results,
    save_outcome,
    get_connection
)

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_TICKERS = [
    # Tech
    "AAPL", "MSFT", "GOOGL", "META", "AMZN", "NVDA",
    # Financials
    "JPM", "BAC", "GS", "V", "MA",
    # Consumer
    "HD", "WMT", "COST", "MCD", "NKE",
    # Health
    "JNJ", "UNH",
    # Reference ETFs
    "SPY", "QQQ",
]

# Path to S&P 500 JSON (optional)
SP500_JSON = os.path.join(os.path.dirname(__file__), "sp500_tickers.json")

# How many days back to simulate
DEFAULT_LOOKBACK_DAYS = 365

# Days after signal to check outcome
OUTCOME_DAYS = 30

# Minimum data points needed for reliable indicators
MIN_DATA_POINTS = 60  # ~SMA200 + buffer

# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 5   # seconds between retries
API_DELAY   = 0.3 # seconds between yfinance calls (rate limiting)

# Log file
LOG_FILE = "backtest_errors.log"


# ══════════════════════════════════════════════════════════════════════════════
# S&P 500 LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_sp500_tickers():
    """
    Load ticker list from sp500_tickers.json.
    Returns dict { ticker: sector } for all tickers.
    Falls back to DEFAULT_TICKERS with sector=None if file not found.
    """
    if not os.path.exists(SP500_JSON):
        print(f"  ⚠️  sp500_tickers.json not found — using default {len(DEFAULT_TICKERS)} tickers")
        return {t: None for t in DEFAULT_TICKERS}

    with open(SP500_JSON, "r") as f:
        data = json.load(f)

    ticker_sector = {}
    for sector, tickers in data["by_sector"].items():
        for ticker in tickers:
            ticker_sector[ticker] = sector

    print(f"  ✅ Loaded {len(ticker_sector)} tickers from sp500_tickers.json")
    return ticker_sector


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def log_error(ticker, date_str, error):
    """Log error to file without crashing the script."""
    logging.error(f"{ticker} | {date_str} | {error}")


# ══════════════════════════════════════════════════════════════════════════════
# TRADING DAYS
# ══════════════════════════════════════════════════════════════════════════════

def get_trading_days(start_date, end_date):
    """
    Generate list of trading days (Mon-Fri) between two dates.
    Does not account for holidays — weekends only.

    Returns:
        list of date objects
    """
    days = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:  # Monday=0, Friday=4
            days.append(current)
        current += timedelta(days=1)
    return days


# ══════════════════════════════════════════════════════════════════════════════
# HISTORICAL CRITERIA — adapted from criteria.py for point-in-time analysis
# ══════════════════════════════════════════════════════════════════════════════

def get_historical_criteria(ticker, as_of_date, full_data):
    """
    Calculate all criteria for a ticker AS OF a specific historical date.

    Uses a slice of historical data ending on as_of_date to simulate
    what the indicators would have looked like on that day.

    Args:
        ticker      (str)           — stock symbol
        as_of_date  (date)          — the date we're simulating
        full_data   (pd.DataFrame)  — full year+ of OHLCV data

    Returns:
        dict — same structure as criteria.get_all_criteria()
        or None if insufficient data
    """
    from criteria import (
        get_trend_25d, get_moving_averages, get_rsi,
        get_52_week_position, get_support_resistance,
        get_candlestick_pattern, get_historical_volatility,
        get_volume
    )

    # Slice data up to and including as_of_date
    mask = full_data.index.date <= as_of_date
    data = full_data[mask].copy()

    if len(data) < MIN_DATA_POINTS:
        return None

    closes = data["Close"].squeeze()
    price  = float(closes.iloc[-1])

    if price <= 0:
        return None

    # ── Technical indicators ──────────────────────────────────────────────────
    try:
        trend  = get_trend_25d(closes)
        ma     = get_moving_averages(closes)
        rsi    = get_rsi(closes)
        week52 = get_52_week_position(closes, price)
        sr     = get_support_resistance(closes, price)
        candle = get_candlestick_pattern(data)
        hv     = get_historical_volatility(closes)
        vol    = get_volume(data)
    except Exception as e:
        raise ValueError(f"Technical indicator error: {e}")

    # ── Volatility — IV not available historically, use HV as proxy ──────────
    iv_approx = round(hv * 1.1, 2) if hv else None

    # IV percentile — calculated from rolling HV history
    iv_percentile = None
    try:
        returns    = closes.pct_change().dropna()
        hv_history = returns.rolling(window=30).std().dropna() * (252**0.5) * 100
        if len(hv_history) >= 30 and iv_approx:
            pct = (hv_history < iv_approx).sum() / len(hv_history) * 100
            iv_percentile = round(float(pct), 1)
    except Exception:
        pass

    # ── Fundamentals — use cached current values ──────────────────────────────
    fundamentals = _get_fundamentals_cached(ticker)

    # ── Beta — use cached current value ──────────────────────────────────────
    beta = _get_beta_cached(ticker)

    # ── PCR and OI — not available historically, score as neutral ─────────────
    # ── Earnings — check nearest date relative to as_of_date ─────────────────
    earnings_info = _get_earnings_near_date(ticker, as_of_date)

    return {
        "ticker":    ticker,
        "timestamp": datetime.combine(as_of_date, datetime.min.time()).isoformat(),
        "price":     round(price, 2),

        "technical": {
            "trend_25d":          trend,
            "moving_averages":    ma,
            "rsi":                rsi,
            "week_52":            week52,
            "support_resistance": sr,
            "candlestick":        candle,
        },

        "volatility": {
            "hv_30d":         hv,
            "implied":        {"iv": iv_approx, "expiration": None},
            "iv_percentile":  {"percentile": iv_percentile},
            "beta":           beta,
            "put_call_ratio": {"pcr": None, "call_volume": 0, "put_volume": 0},
            "open_interest":  {"oi": None},
        },

        "earnings":    earnings_info,
        "volume":      vol,
        "fundamental": fundamentals,
    }


# ── Caching helpers — avoid repeated API calls for static data ────────────────

_fundamentals_cache = {}
_beta_cache         = {}
_earnings_cache     = {}


def _get_fundamentals_cached(ticker):
    """Fetch fundamentals once per ticker and cache."""
    if ticker not in _fundamentals_cache:
        try:
            import sys, io
            old = sys.stderr
            sys.stderr = io.StringIO()
            tk   = yf.Ticker(ticker)
            info = tk.info
            sys.stderr = old

            eps_trailing = info.get("trailingEps")
            eps_forward  = info.get("forwardEps")
            eps_growth   = None
            if eps_trailing and eps_forward and eps_trailing != 0:
                eps_growth = round(
                    (eps_forward - eps_trailing) / abs(eps_trailing) * 100, 2
                )

            de_raw = info.get("debtToEquity")
            margin = info.get("profitMargins")

            _fundamentals_cache[ticker] = {
                "pe":                round(float(info.get("trailingPE") or 0), 2) or None,
                "eps_trailing":      eps_trailing,
                "eps_forward":       eps_forward,
                "eps_growth_pct":    eps_growth,
                "debt_to_equity":    round(float(de_raw) / 100, 2) if de_raw else None,
                "profit_margin_pct": round(float(margin) * 100, 2) if margin else None,
            }
        except Exception:
            _fundamentals_cache[ticker] = {
                "pe": None, "eps_trailing": None, "eps_forward": None,
                "eps_growth_pct": None, "debt_to_equity": None,
                "profit_margin_pct": None,
            }
    return _fundamentals_cache[ticker]


def _get_beta_cached(ticker):
    """Fetch beta once per ticker and cache."""
    if ticker not in _beta_cache:
        try:
            import sys, io
            old = sys.stderr
            sys.stderr = io.StringIO()
            tk   = yf.Ticker(ticker)
            beta = tk.info.get("beta")
            sys.stderr = old
            _beta_cache[ticker] = round(float(beta), 2) if beta else None
        except Exception:
            _beta_cache[ticker] = None
    return _beta_cache[ticker]


def _get_earnings_near_date(ticker, as_of_date):
    """
    Check earnings dates relative to as_of_date using yfinance calendar.
    Cached per ticker.
    """
    if ticker not in _earnings_cache:
        try:
            import sys, io
            old = sys.stderr
            sys.stderr = io.StringIO()
            tk       = yf.Ticker(ticker)
            info     = tk.info
            calendar = tk.calendar
            sys.stderr = old

            is_etf = info.get("quoteType", "") == "ETF"

            if is_etf:
                _earnings_cache[ticker] = {"is_etf": True, "dates": []}
            else:
                dates = []
                if calendar and "Earnings Date" in calendar:
                    raw = calendar["Earnings Date"]
                    if isinstance(raw, list):
                        dates = [d for d in raw if isinstance(d, date)]
                    elif isinstance(raw, date):
                        dates = [raw]
                _earnings_cache[ticker] = {"is_etf": False, "dates": dates}
        except Exception:
            _earnings_cache[ticker] = {"is_etf": False, "dates": []}

    cached = _earnings_cache[ticker]

    if cached["is_etf"]:
        return {"days_to_earnings": None, "is_etf": True}

    future_dates = [d for d in cached["dates"] if d > as_of_date]

    if not future_dates:
        return {"days_to_earnings": None, "is_etf": False}

    nearest = min(future_dates)
    days    = (nearest - as_of_date).days

    if days < 0 or days > 365:
        return {"days_to_earnings": None, "is_etf": False}

    return {"days_to_earnings": int(days), "is_etf": False}


# ══════════════════════════════════════════════════════════════════════════════
# OUTCOME CALCULATION
# ══════════════════════════════════════════════════════════════════════════════

def calculate_outcome(full_data, entry_date, entry_price):
    """
    Calculate what happened to the stock price 30 days after entry.

    Uses pre-downloaded historical data — no extra API calls needed.

    Args:
        full_data   (pd.DataFrame) — full historical OHLCV data
        entry_date  (date)         — date of the simulated trade
        entry_price (float)        — price at entry

    Returns:
        dict with keys:
            price_at_30d        (float | None)
            pct_change_30d      (float | None)
            would_have_profited (bool | None)
    """
    target_date = entry_date + timedelta(days=OUTCOME_DAYS)

    future_data = full_data[full_data.index.date >= target_date]

    if future_data.empty:
        return {
            "price_at_30d":        None,
            "pct_change_30d":      None,
            "would_have_profited": None
        }

    price_30d  = float(future_data["Close"].squeeze().iloc[0])
    pct_change = round((price_30d - entry_price) / entry_price * 100, 4)

    return {
        "price_at_30d":        round(price_30d, 2),
        "pct_change_30d":      pct_change,
        "would_have_profited": pct_change > 0
    }


# ══════════════════════════════════════════════════════════════════════════════
# PROGRESS DISPLAY
# ══════════════════════════════════════════════════════════════════════════════

def print_progress(current, total, ticker, current_date, start_time, errors):
    """Print a progress bar with ETA."""
    pct       = current / total * 100
    elapsed   = time.time() - start_time
    rate      = current / elapsed if elapsed > 0 else 0
    remaining = (total - current) / rate if rate > 0 else 0

    eta_min = int(remaining // 60)
    eta_sec = int(remaining % 60)

    bar_width = 30
    filled    = int(bar_width * current / total)
    bar       = "█" * filled + "░" * (bar_width - filled)

    sys.stdout.write(
        f"\r  [{bar}] {pct:.1f}% | "
        f"{current}/{total} | "
        f"{ticker} {current_date} | "
        f"ETA: {eta_min}m {eta_sec}s | "
        f"Errors: {errors}"
    )
    sys.stdout.flush()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(tickers, lookback_days=DEFAULT_LOOKBACK_DAYS, resume=False, ticker_sector=None):
    """
    Run the full historical simulation.

    Args:
        tickers       (list)       — tickers to simulate
        lookback_days (int)        — how many days back to simulate
        resume        (bool)       — skip already-processed dates
        ticker_sector (dict|None)  — map of ticker -> sector string
    """
    if ticker_sector is None:
        ticker_sector = {t: None for t in tickers}

    end_date   = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=lookback_days)

    # Need extra data for SMA200 calculation
    data_start = start_date - timedelta(days=MIN_DATA_POINTS + 30)

    trading_days = get_trading_days(start_date, end_date)
    total_steps  = len(tickers) * len(trading_days)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'═' * 65}")
    print(f"  BACKTEST — {timestamp}")
    print(f"  Period:   {start_date} → {end_date} ({len(trading_days)} trading days)")
    print(f"  Tickers:  {len(tickers)}")
    print(f"  Total:    {total_steps:,} analysis points")
    print(f"  Mode:     {'RESUME' if resume else 'FULL'}")
    print(f"  ETA:      ~{total_steps * 1.5 / 3600:.1f} hours estimated")
    print(f"{'═' * 65}\n")

    if sys.stdin.isatty():
        input("  Press Enter to start (Ctrl+C to stop at any time)...")
    else:
        print("  Running in non-interactive mode — starting automatically...")
    print()

    start_time    = time.time()
    current_step  = 0
    total_errors  = 0
    total_saved   = 0
    total_skipped = 0

    for ticker in tickers:
        sector = ticker_sector.get(ticker)
        sector_label = f" [{sector}]" if sector else ""
        print(f"\n\n  ── {ticker}{sector_label} {'─' * max(1, 48 - len(ticker) - len(sector_label))}")

        # ── Download full historical data ─────────────────────────────────────
        print(f"  📥 Downloading {ticker} historical data...", end=" ", flush=True)

        full_data = None
        for attempt in range(MAX_RETRIES):
            try:
                full_data = yf.download(
                    ticker,
                    start=data_start,
                    end=end_date + timedelta(days=OUTCOME_DAYS + 10),
                    interval="1d",
                    progress=False,
                    auto_adjust=True
                )
                if not full_data.empty:
                    break
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)

        if full_data is None or full_data.empty:
            print(f"❌ Failed to download data — skipping {ticker}")
            log_error(ticker, "DOWNLOAD", "Failed to download historical data")
            current_step += len(trading_days)
            total_errors += 1
            continue

        print(f"✅ {len(full_data)} days downloaded")

        # ── Find resume point ─────────────────────────────────────────────────
        resume_from = None
        if resume:
            last_date = get_backtest_progress(ticker)
            if last_date:
                resume_from = last_date
                skipped = sum(1 for d in trading_days if d <= last_date)
                total_skipped += skipped
                print(f"  ⏭️  Resuming from {last_date} ({skipped} days skipped)")

        # ── Pre-fetch static data once per ticker ─────────────────────────────
        print(f"  📊 Pre-fetching fundamentals and beta...", end=" ", flush=True)
        _get_fundamentals_cached(ticker)
        _get_beta_cached(ticker)
        _get_earnings_near_date(ticker, start_date)
        print("✅")

        # ── Process each trading day ──────────────────────────────────────────
        ticker_saved  = 0
        ticker_errors = 0

        for sim_date in trading_days:
            current_step += 1

            # Skip if resuming and already processed
            if resume_from and sim_date <= resume_from:
                continue

            print_progress(
                current_step, total_steps, ticker,
                sim_date, start_time, total_errors
            )

            # ── Calculate criteria for this date ──────────────────────────────
            criteria = None
            for attempt in range(MAX_RETRIES):
                try:
                    criteria = get_historical_criteria(ticker, sim_date, full_data)
                    if criteria is None:
                        print(f"\n  DEBUG: {ticker} {sim_date} → criteria=None (datos insuficientes)")
                        continue
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY)
                    else:
                        log_error(ticker, str(sim_date), f"criteria: {e}")
                        ticker_errors += 1

            if criteria is None:
                continue

            # ── Score criteria ────────────────────────────────────────────────
            scored = None
            try:
                scored = score_criteria(criteria)
            except Exception as e:
                log_error(ticker, str(sim_date), f"scoring: {e}")
                ticker_errors += 1
                continue

            if scored is None:
                continue

            # ── Save to DB ────────────────────────────────────────────────────
            analysis_id = None
            for attempt in range(MAX_RETRIES):
                try:
                    analysis_id = save_backtest_analysis(scored, sim_date, sector=sector)
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(2)
                    else:
                        log_error(ticker, str(sim_date), f"db save: {e}")
                        ticker_errors += 1

            if analysis_id is None:
                continue

            # ── Calculate and save outcome ────────────────────────────────────
            try:
                outcome = calculate_outcome(full_data, sim_date, criteria["price"])

                if outcome["price_at_30d"] is not None:
                    save_outcome(
                        analysis_id=analysis_id,
                        ticker=ticker,
                        price_at_analysis=criteria["price"],
                        price_at_30d=outcome["price_at_30d"],
                        price_at_expiry=outcome["price_at_30d"]
                    )
            except Exception as e:
                log_error(ticker, str(sim_date), f"outcome: {e}")

            ticker_saved += 1
            total_saved  += 1

            time.sleep(API_DELAY)

        total_errors += ticker_errors
        print(f"\n  ✅ {ticker}: {ticker_saved} days saved, {ticker_errors} errors")

    # ── Final summary ─────────────────────────────────────────────────────────
    elapsed_min = int((time.time() - start_time) // 60)
    elapsed_sec = int((time.time() - start_time) % 60)

    print(f"\n\n{'═' * 65}")
    print(f"  BACKTEST COMPLETE")
    print(f"{'═' * 65}")
    print(f"  Total saved:   {total_saved:,} analysis points")
    print(f"  Total skipped: {total_skipped:,} (resume mode)")
    print(f"  Total errors:  {total_errors}")
    print(f"  Duration:      {elapsed_min}m {elapsed_sec}s")
    if total_errors > 0:
        print(f"  Error log:     {LOG_FILE}")
    print(f"{'═' * 65}\n")

    print_summary()


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY — quick stats without running backtest
# ══════════════════════════════════════════════════════════════════════════════

def print_summary():
    """Print summary statistics from backtest results in DB."""
    print(f"\n{'═' * 65}")
    print(f"  BACKTEST RESULTS SUMMARY")
    print(f"{'═' * 65}")

    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM analysis WHERE is_backtest = TRUE;")
    total = cur.fetchone()[0]

    if total == 0:
        print("  No backtest data found. Run backtest first.")
        cur.close()
        conn.close()
        return

    print(f"  Total analysis points: {total:,}")

    # By verdict
    cur.execute("""
        SELECT verdict, COUNT(*) as cnt
        FROM analysis WHERE is_backtest = TRUE
        GROUP BY verdict ORDER BY cnt DESC;
    """)
    print(f"\n  BY VERDICT:")
    for row in cur.fetchall():
        print(f"    {row[0]:<15} {row[1]:>6,}")

    # VIABLE accuracy
    cur.execute("""
        SELECT
            COUNT(*) as total_viable,
            SUM(CASE WHEN o.would_have_profited = TRUE THEN 1 ELSE 0 END) as profitable,
            AVG(o.pct_change_30d) as avg_return
        FROM analysis a
        JOIN outcomes o ON o.analysis_id = a.id
        WHERE a.is_backtest = TRUE AND a.verdict = 'VIABLE';
    """)
    row = cur.fetchone()
    if row and row[0]:
        win_rate = (row[1] / row[0] * 100) if row[0] > 0 else 0
        avg_ret  = row[2] or 0
        print(f"\n  VIABLE ACCURACY:")
        print(f"    Total VIABLE signals:  {row[0]:,}")
        print(f"    Profitable (up 30d):   {row[1]:,} ({win_rate:.1f}%)")
        print(f"    Average 30d return:    {avg_ret:.2f}%")

    # CAUTION accuracy
    cur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN o.would_have_profited = TRUE THEN 1 ELSE 0 END) as profitable,
            AVG(o.pct_change_30d) as avg_return
        FROM analysis a
        JOIN outcomes o ON o.analysis_id = a.id
        WHERE a.is_backtest = TRUE AND a.verdict = 'CAUTION';
    """)
    row = cur.fetchone()
    if row and row[0]:
        win_rate = (row[1] / row[0] * 100) if row[0] > 0 else 0
        avg_ret  = row[2] or 0
        print(f"\n  CAUTION ACCURACY:")
        print(f"    Total CAUTION signals: {row[0]:,}")
        print(f"    Profitable (up 30d):   {row[1]:,} ({win_rate:.1f}%)")
        print(f"    Average 30d return:    {avg_ret:.2f}%")

    # Criteria average scores
    cur.execute("""
        SELECT
            cs.criterion,
            AVG(cs.score) as avg_score,
            COUNT(*) as appearances
        FROM criteria_scores cs
        JOIN analysis a ON a.id = cs.analysis_id
        WHERE a.is_backtest = TRUE
        GROUP BY cs.criterion
        ORDER BY avg_score DESC;
    """)
    rows = cur.fetchall()
    if rows:
        print(f"\n  CRITERIA AVERAGE SCORES (highest → most bullish signals):")
        for row in rows:
            print(f"    {row[0]:<25} avg: {row[1]:>+.2f}  ({row[2]:,} samples)")

    # By ticker
    cur.execute("""
        SELECT
            a.ticker,
            COUNT(*) as total,
            SUM(CASE WHEN a.verdict = 'VIABLE' THEN 1 ELSE 0 END) as viable_count,
            AVG(CASE WHEN a.verdict = 'VIABLE' AND o.would_have_profited IS NOT NULL
                     THEN CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END
                END) as viable_accuracy
        FROM analysis a
        LEFT JOIN outcomes o ON o.analysis_id = a.id
        WHERE a.is_backtest = TRUE
        GROUP BY a.ticker
        ORDER BY viable_accuracy DESC NULLS LAST;
    """)
    rows = cur.fetchall()
    if rows:
        print(f"\n  BY TICKER (VIABLE accuracy):")
        for row in rows:
            acc = f"{row[3]*100:.1f}%" if row[3] is not None else "N/A"
            print(f"    {row[0]:<8} total:{row[1]:>5,}  viable:{row[2]:>4,}  accuracy:{acc}")

    cur.close()
    conn.close()
    print(f"\n{'═' * 65}\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Historical backtest of Bull Call Spread analysis system"
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Tickers to backtest (default: full S&P 500 from sp500_tickers.json)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="How many days back to simulate (default: 365)"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume interrupted backtest from last checkpoint"
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show results summary without running backtest"
    )
    args = parser.parse_args()

    if args.summary:
        print_summary()
    else:
        # Cargar tickers
        if args.tickers:
            # Tickers manuales — buscar sector en JSON si existe
            sp500 = load_sp500_tickers()
            ticker_sector = {t: sp500.get(t) for t in args.tickers}
            tickers = args.tickers
        else:
            # Cargar desde JSON (S&P 500 completo o fallback a DEFAULT_TICKERS)
            ticker_sector = load_sp500_tickers()
            tickers = list(ticker_sector.keys())

        try:
            run_backtest(
                tickers=tickers,
                lookback_days=args.days,
                resume=args.resume,
                ticker_sector=ticker_sector
            )
        except KeyboardInterrupt:
            print(f"\n\n  ⚠️  Backtest interrupted by user.")
            print(f"  Progress saved to DB. Resume with: python backtest.py --resume")
            print(f"  Partial results available with:    python backtest.py --summary\n")