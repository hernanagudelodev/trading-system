"""
backtest.py
===========
Historical data collection for the v2 Bull Call Spread system.

This script does ONE thing: for each ticker, for each trading day,
it calculates raw criteria values and saves them to the database.

It does NOT:
    - Call scoring.py
    - Calculate labels or interpret values
    - Determine VIABLE / CAUTION / DO_NOT_TRADE
    - Calculate outcomes (that is simulate.py's job)

The result is a clean dataset of raw market measurements
that audit.py can score with any set of parameters.

Usage:
    python backtest.py                           # S&P 500 from sp500_tickers.json
    python backtest.py --tickers AAPL MSFT JPM   # specific tickers
    python backtest.py --days 365                # lookback period
    python backtest.py --resume                  # skip already-processed dates
    python backtest.py --summary                 # show DB stats without running

Dependencies:
    criteria.py         -> get_raw_criteria()
    db.py               -> save_snapshot(), save_criteria()
    sp500_tickers.json  -> ticker list with sectors
    .env                -> DATABASE_URL
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import date, timedelta, datetime

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

# Allow running from v2/ directory or project root
sys.path.insert(0, os.path.dirname(__file__))

from criteria import get_raw_criteria
from db import (
    get_connection,
    save_snapshot,
    save_criteria,
    get_last_backtest_date,
)

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "META", "AMZN", "NVDA",
    "JPM", "BAC", "GS", "V", "MA",
    "HD", "WMT", "COST", "MCD", "NKE",
    "JNJ", "UNH", "SPY", "QQQ",
]

SP500_JSON           = os.path.join(os.path.dirname(__file__), "data", "sp500_tickers.json")
DEFAULT_LOOKBACK     = 365   # days
MIN_DATA_POINTS      = 60    # minimum candles needed
MAX_RETRIES          = 3
RETRY_DELAY          = 5     # seconds
API_DELAY            = 0.2   # seconds between yfinance calls
LOG_FILE             = os.path.join(os.path.dirname(__file__), "backtest.log")

# How many extra days to download beyond lookback
# (needed for SMA200 and 52-week calculations)
DOWNLOAD_BUFFER = 260


# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def log_error(ticker, date_str, error):
    logging.error(f"{ticker} | {date_str} | {error}")


# ══════════════════════════════════════════════════════════════════════════════
# TICKER LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_tickers():
    """
    Load tickers from sp500_tickers.json.
    Returns dict {ticker: sector}.
    Falls back to DEFAULT_TICKERS if file not found.
    """
    if not os.path.exists(SP500_JSON):
        print(f"  sp500_tickers.json not found — using {len(DEFAULT_TICKERS)} default tickers")
        return {t: None for t in DEFAULT_TICKERS}

    with open(SP500_JSON) as f:
        data = json.load(f)

    ticker_sector = {}
    for sector, tickers in data["by_sector"].items():
        for ticker in tickers:
            ticker_sector[ticker] = sector

    print(f"  Loaded {len(ticker_sector)} tickers from sp500_tickers.json")
    return ticker_sector


# ══════════════════════════════════════════════════════════════════════════════
# TRADING DAYS
# ══════════════════════════════════════════════════════════════════════════════

def get_trading_days(start, end):
    """Return weekdays between start and end (approximate trading days)."""
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


# ══════════════════════════════════════════════════════════════════════════════
# PROGRESS DISPLAY
# ══════════════════════════════════════════════════════════════════════════════

def print_progress(current, total, ticker, sim_date, start_time, errors):
    pct       = current / total * 100
    elapsed   = time.time() - start_time
    rate      = current / elapsed if elapsed > 0 else 0
    remaining = (total - current) / rate if rate > 0 else 0

    eta_min = int(remaining // 60)
    eta_sec = int(remaining % 60)

    bar_width = 25
    filled    = int(bar_width * current / total)
    bar       = "#" * filled + "." * (bar_width - filled)

    sys.stdout.write(
        f"\r  [{bar}] {pct:.1f}% | {current}/{total} | "
        f"{ticker} {sim_date} | ETA {eta_min}m{eta_sec}s | Err:{errors}"
    )
    sys.stdout.flush()


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def print_summary():
    """Show DB stats for v2 tables."""
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM v2_snapshots;")
    n_snapshots = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM v2_criteria;")
    n_criteria = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT ticker) FROM v2_snapshots;")
    n_tickers = cur.fetchone()[0]

    cur.execute("SELECT MIN(backtest_date), MAX(backtest_date) FROM v2_snapshots;")
    date_min, date_max = cur.fetchone()

    cur.execute("""
        SELECT ticker, COUNT(*) as days
        FROM v2_snapshots
        GROUP BY ticker
        ORDER BY days DESC
        LIMIT 10;
    """)
    top_tickers = cur.fetchall()

    cur.close()
    conn.close()

    print(f"\n{'=' * 55}")
    print(f"  V2 BACKTEST SUMMARY")
    print(f"{'=' * 55}")
    print(f"  Snapshots:  {n_snapshots:,}")
    print(f"  Criteria:   {n_criteria:,}")
    print(f"  Tickers:    {n_tickers}")
    print(f"  Period:     {date_min} -> {date_max}")
    print(f"\n  Top tickers by days collected:")
    for ticker, days in top_tickers:
        print(f"    {ticker:<8} {days:>4} days")
    print(f"{'=' * 55}\n")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(tickers, ticker_sector, lookback_days, resume):
    """
    Main backtest loop.

    For each ticker:
        1. Download full OHLCV history (lookback + buffer for indicators)
        2. For each trading day in the lookback period:
            a. Call get_raw_criteria(data, as_of_date)
            b. Save snapshot (ticker, date, price, sector) to v2_snapshots
            c. Save each criterion's raw value to v2_criteria
        3. Log progress

    Does NOT calculate outcomes — that is simulate.py's job.
    Does NOT call scoring.py — no labels, no scores, no verdicts.
    """
    end_date   = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=lookback_days)

    # Download extra history for SMA200 and 52-week calculations
    download_start = start_date - timedelta(days=DOWNLOAD_BUFFER)

    trading_days = get_trading_days(start_date, end_date)
    total_steps  = len(tickers) * len(trading_days)

    print(f"\n{'=' * 60}")
    print(f"  V2 BACKTEST — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Period:   {start_date} -> {end_date} ({len(trading_days)} trading days)")
    print(f"  Tickers:  {len(tickers)}")
    print(f"  Points:   {total_steps:,} (ticker/day combinations)")
    print(f"  Mode:     {'RESUME' if resume else 'FULL'}")
    print(f"{'=' * 60}\n")

    if sys.stdin.isatty():
        input("  Press Enter to start (Ctrl+C to stop anytime)...")
    else:
        print("  Starting automatically (non-interactive mode)...")
    print()

    start_time    = time.time()
    current_step  = 0
    total_saved   = 0
    total_skipped = 0
    total_errors  = 0

    for ticker in tickers:
        sector = ticker_sector.get(ticker)
        print(f"\n\n  -- {ticker} [{sector or 'Unknown'}] --")

        # ── Download full historical data ─────────────────────────────────────
        print(f"  Downloading {ticker}...", end=" ", flush=True)

        full_data = None
        for attempt in range(MAX_RETRIES):
            try:
                full_data = yf.download(
                    ticker,
                    start=download_start,
                    end=end_date + timedelta(days=1),
                    interval="1d",
                    progress=False,
                    auto_adjust=True,
                )
                if not full_data.empty:
                    break
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)

        if full_data is None or full_data.empty:
            print(f"FAILED — skipping")
            log_error(ticker, "DOWNLOAD", "Failed to download data")
            current_step += len(trading_days)
            total_errors += 1
            continue

        print(f"OK ({len(full_data)} days)")

        # ── Find resume point ─────────────────────────────────────────────────
        resume_from = None
        if resume:
            last_date = get_last_backtest_date(ticker)
            if last_date:
                resume_from = last_date
                skipped = sum(1 for d in trading_days if d <= last_date)
                total_skipped += skipped
                print(f"  Resuming from {last_date} ({skipped} days skipped)")

        # ── Process each trading day ──────────────────────────────────────────
        ticker_saved  = 0
        ticker_errors = 0

        for sim_date in trading_days:
            current_step += 1

            if resume_from and sim_date <= resume_from:
                continue

            print_progress(current_step, total_steps, ticker, sim_date, start_time, total_errors)

            # Calculate raw criteria
            raw = None
            for attempt in range(MAX_RETRIES):
                try:
                    raw = get_raw_criteria(full_data, sim_date)
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY)
                    else:
                        log_error(ticker, str(sim_date), f"criteria error: {e}")
                        ticker_errors += 1

            if raw is None:
                # Insufficient data for this date (normal at start of period)
                continue

            # Save to DB
            try:
                snapshot_id = save_snapshot(
                    ticker=ticker,
                    backtest_date=sim_date,
                    price=raw["price"],
                    sector=sector,
                )
                save_criteria(snapshot_id, raw)
                ticker_saved  += 1
                total_saved   += 1
            except Exception as e:
                log_error(ticker, str(sim_date), f"db error: {e}")
                ticker_errors += 1

            time.sleep(API_DELAY)

        total_errors += ticker_errors
        print(f"\n  {ticker}: {ticker_saved} days saved, {ticker_errors} errors")

    # ── Final report ──────────────────────────────────────────────────────────
    elapsed = int(time.time() - start_time)
    print(f"\n\n{'=' * 60}")
    print(f"  BACKTEST COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Saved:    {total_saved:,} snapshots")
    print(f"  Skipped:  {total_skipped:,} (resume mode)")
    print(f"  Errors:   {total_errors}")
    print(f"  Duration: {elapsed // 60}m {elapsed % 60}s")
    if total_errors > 0:
        print(f"  Log:      {LOG_FILE}")
    print(f"{'=' * 60}\n")

    print_summary()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V2 Backtest — raw criteria collection")

    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Specific tickers (default: sp500_tickers.json)")
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK,
                        help=f"Lookback days (default: {DEFAULT_LOOKBACK})")
    parser.add_argument("--resume", action="store_true",
                        help="Skip already-processed dates")
    parser.add_argument("--summary", action="store_true",
                        help="Show DB stats without running")

    args = parser.parse_args()

    if args.summary:
        print_summary()
    else:
        # Load tickers
        if args.tickers:
            all_tickers   = load_tickers()
            ticker_sector = {t: all_tickers.get(t) for t in args.tickers}
            tickers       = args.tickers
        else:
            ticker_sector = load_tickers()
            tickers       = list(ticker_sector.keys())

        try:
            run_backtest(
                tickers=tickers,
                ticker_sector=ticker_sector,
                lookback_days=args.days,
                resume=args.resume,
            )
        except KeyboardInterrupt:
            print(f"\n\n  Backtest interrupted.")
            print(f"  Resume with: python backtest.py --resume")
            print(f"  Partial results: python backtest.py --summary\n")