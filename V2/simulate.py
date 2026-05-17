"""
simulate.py
===========
Simulates trading outcomes for each snapshot in v2_snapshots.

OPTIMIZED: Downloads the full price history once per ticker,
then computes all outcomes locally without additional API calls.
This reduces API calls from ~139,000 to ~534 (one per ticker).

For each snapshot, sweeps the N days after the snapshot date
and evaluates whether the configured strategy would have been
successful based on actual historical price movement.

Supported strategies:
    PRICE_TARGET      — did price move X% in N days?
    BULL_CALL_SPREAD  — did price move enough to profit from a spread?
    LONG_CALL         — did price move enough to cover the call premium?

Usage:
    python simulate.py                                     # all in strategies/
    python simulate.py --strategy strategies/price_target.json
    python simulate.py --tickers AAPL MSFT                 # filter tickers
    python simulate.py --resume                            # skip already done

Dependencies:
    db.py       -> get_snapshots_grouped_by_ticker(), save_outcome()
    .env        -> DATABASE_URL
"""

import os
import sys
import json
import glob
import time
import argparse
from datetime import date, timedelta, datetime
from collections import defaultdict

import yfinance as yf
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

from db import get_connection, save_outcome

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

STRATEGIES_DIR = os.path.join(os.path.dirname(__file__), "strategies")
MAX_RETRIES    = 3
RETRY_DELAY    = 5
API_DELAY      = 0.3   # seconds between ticker downloads


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_strategy(path):
    """Load strategy definition from JSON file."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Strategy file not found: {path}")
    with open(path) as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════════
# PRICE DATA — one download per ticker
# ══════════════════════════════════════════════════════════════════════════════

def download_full_history(ticker, start_date, end_date):
    """
    Download full OHLCV history for a ticker covering the entire
    backtest period plus 35 days forward for outcome calculation.

    Returns:
        dict {date: float} — closing prices by date
        or empty dict if download failed
    """
    # Add 35 days beyond end_date to capture forward prices
    fetch_end = end_date + timedelta(days=50)

    for attempt in range(MAX_RETRIES):
        try:
            data = yf.download(
                ticker,
                start=start_date,
                end=fetch_end,
                interval="1d",
                progress=False,
                auto_adjust=True,
            )
            if not data.empty:
                closes = data["Close"].squeeze()
                return {
                    d.date(): float(closes.iloc[i])
                    for i, d in enumerate(data.index)
                }
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)

    return {}


# ══════════════════════════════════════════════════════════════════════════════
# SNAPSHOT LOADER — grouped by ticker
# ══════════════════════════════════════════════════════════════════════════════

def get_snapshots_by_ticker(strategy_name=None, tickers=None, resume=False):
    """
    Load all snapshots from DB grouped by ticker.
    Optionally filter by tickers and skip already-simulated snapshots.

    Returns:
        dict {ticker: [{"id": int, "backtest_date": date, "price": float}]}
    """
    conn = get_connection()
    cur  = conn.cursor()

    if resume and strategy_name:
        cur.execute("""
            SELECT s.id, s.ticker, s.backtest_date, s.price
            FROM v2_snapshots s
            LEFT JOIN v2_outcomes o
                ON o.snapshot_id = s.id AND o.strategy = %s
            WHERE o.id IS NULL
            ORDER BY s.ticker, s.backtest_date;
        """, (strategy_name,))
    else:
        cur.execute("""
            SELECT id, ticker, backtest_date, price
            FROM v2_snapshots
            ORDER BY ticker, backtest_date;
        """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    grouped = defaultdict(list)
    for row in rows:
        snapshot_id, ticker, backtest_date, price = row
        if tickers and ticker.upper() not in [t.upper() for t in tickers]:
            continue
        grouped[ticker].append({
            "id":           snapshot_id,
            "backtest_date": backtest_date,
            "price":        float(price),
        })

    return grouped


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY EVALUATORS
# ══════════════════════════════════════════════════════════════════════════════

def evaluate_price_target(entry_price, forward_prices, sorted_dates, params):
    """
    PRICE_TARGET: success if price rises target_pct% within max_days.
    Stop if price falls stop_loss_pct%.
    """
    target_pct    = params["target_pct"]
    stop_loss_pct = params["stop_loss_pct"]
    max_days      = params["max_days"]

    for day_num, d in enumerate(sorted_dates[:max_days], start=1):
        if d not in forward_prices:
            continue
        price      = forward_prices[d]
        pct_change = (price - entry_price) / entry_price * 100

        if pct_change >= target_pct:
            return {"was_successful": True,  "exit_day": day_num,
                    "exit_price": round(price, 4), "pct_change": round(pct_change, 4),
                    "exit_reason": "TARGET_REACHED"}

        if pct_change <= stop_loss_pct:
            return {"was_successful": False, "exit_day": day_num,
                    "exit_price": round(price, 4), "pct_change": round(pct_change, 4),
                    "exit_reason": "STOP_LOSS"}

    # Expired
    available = [d for d in sorted_dates[:max_days] if d in forward_prices]
    if available:
        last_price = forward_prices[available[-1]]
        pct_change = (last_price - entry_price) / entry_price * 100
        return {"was_successful": pct_change >= 0, "exit_day": len(available),
                "exit_price": round(last_price, 4), "pct_change": round(pct_change, 4),
                "exit_reason": "EXPIRED"}

    return {"was_successful": None, "exit_day": None, "exit_price": None,
            "pct_change": None, "exit_reason": "NO_DATA"}


def evaluate_bull_call_spread(entry_price, forward_prices, sorted_dates, params):
    """
    BULL_CALL_SPREAD: simplified spread simulation using price movement.
    Long ATM call + short call at ATM + spread_width.
    """
    spread_width         = params["spread_width"]
    premium_pct_of_width = params["premium_pct_of_width"]
    profit_target_pct    = params["profit_target_pct"]
    stop_loss_pct        = params["stop_loss_pct"]
    max_days             = params["max_days"]

    long_strike  = round(entry_price)
    short_strike = long_strike + spread_width
    premium      = spread_width * premium_pct_of_width
    max_profit   = spread_width - premium
    take_profit  = premium + (max_profit * profit_target_pct)
    stop_value   = premium * (1 - stop_loss_pct)

    for day_num, d in enumerate(sorted_dates[:max_days], start=1):
        if d not in forward_prices:
            continue
        price      = forward_prices[d]
        pct_change = (price - entry_price) / entry_price * 100

        if price <= long_strike:
            spread_value = 0.0
        elif price >= short_strike:
            spread_value = spread_width
        else:
            spread_value = price - long_strike

        if spread_value >= take_profit:
            return {"was_successful": True,  "exit_day": day_num,
                    "exit_price": round(price, 4), "pct_change": round(pct_change, 4),
                    "exit_reason": "TARGET_REACHED"}

        if spread_value <= stop_value:
            return {"was_successful": False, "exit_day": day_num,
                    "exit_price": round(price, 4), "pct_change": round(pct_change, 4),
                    "exit_reason": "STOP_LOSS"}

    available = [d for d in sorted_dates[:max_days] if d in forward_prices]
    if available:
        last_price   = forward_prices[available[-1]]
        pct_change   = (last_price - entry_price) / entry_price * 100
        if last_price <= long_strike:
            spread_value = 0.0
        elif last_price >= short_strike:
            spread_value = spread_width
        else:
            spread_value = last_price - long_strike
        return {"was_successful": spread_value > premium, "exit_day": len(available),
                "exit_price": round(last_price, 4), "pct_change": round(pct_change, 4),
                "exit_reason": "EXPIRED"}

    return {"was_successful": None, "exit_day": None, "exit_price": None,
            "pct_change": None, "exit_reason": "NO_DATA"}


def evaluate_long_call(entry_price, forward_prices, sorted_dates, params):
    """
    LONG_CALL: success if price rises above breakeven (premium + profit target).
    """
    premium_pct       = params["premium_pct"]
    profit_target_pct = params["profit_target_pct"]
    stop_loss_pct     = params["stop_loss_pct"]
    max_days          = params["max_days"]

    breakeven_pct = premium_pct + profit_target_pct

    for day_num, d in enumerate(sorted_dates[:max_days], start=1):
        if d not in forward_prices:
            continue
        price      = forward_prices[d]
        pct_change = (price - entry_price) / entry_price * 100

        if pct_change >= breakeven_pct:
            return {"was_successful": True,  "exit_day": day_num,
                    "exit_price": round(price, 4), "pct_change": round(pct_change, 4),
                    "exit_reason": "TARGET_REACHED"}

        if pct_change <= stop_loss_pct:
            return {"was_successful": False, "exit_day": day_num,
                    "exit_price": round(price, 4), "pct_change": round(pct_change, 4),
                    "exit_reason": "STOP_LOSS"}

    available = [d for d in sorted_dates[:max_days] if d in forward_prices]
    if available:
        last_price = forward_prices[available[-1]]
        pct_change = (last_price - entry_price) / entry_price * 100
        return {"was_successful": pct_change >= breakeven_pct, "exit_day": len(available),
                "exit_price": round(last_price, 4), "pct_change": round(pct_change, 4),
                "exit_reason": "EXPIRED"}

    return {"was_successful": None, "exit_day": None, "exit_price": None,
            "pct_change": None, "exit_reason": "NO_DATA"}


EVALUATORS = {
    "PRICE_TARGET":     evaluate_price_target,
    "BULL_CALL_SPREAD": evaluate_bull_call_spread,
    "LONG_CALL":        evaluate_long_call,
}


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SIMULATE
# ══════════════════════════════════════════════════════════════════════════════

def run_simulate(strategy_path, tickers=None, resume=False):
    """
    Main simulation loop — optimized for minimal API calls.

    For each ticker:
        1. Download full price history ONCE (covers all snapshot dates + 35 days forward)
        2. For each snapshot of that ticker:
            a. Extract forward prices from the downloaded data
            b. Evaluate strategy
            c. Save outcome to DB
    """
    strategy      = load_strategy(strategy_path)
    strategy_name = strategy["name"]
    strategy_type = strategy["type"]
    params        = strategy["params"]

    if strategy_type not in EVALUATORS:
        print(f"  Unknown strategy type: {strategy_type}")
        print(f"  Supported: {list(EVALUATORS.keys())}")
        return

    evaluator = EVALUATORS[strategy_type]

    print(f"\n{'=' * 60}")
    print(f"  SIMULATE — {strategy_name}")
    print(f"  Type:     {strategy_type}")
    print(f"  Mode:     {'RESUME' if resume else 'FULL'}")
    print(f"  Params:")
    for k, v in params.items():
        print(f"    {k}: {v}")
    print(f"{'=' * 60}\n")

    # Load all snapshots grouped by ticker
    print(f"  Loading snapshots from DB...", end=" ", flush=True)
    snapshots_by_ticker = get_snapshots_by_ticker(
        strategy_name=strategy_name,
        tickers=tickers,
        resume=resume,
    )
    total_tickers   = len(snapshots_by_ticker)
    total_snapshots = sum(len(v) for v in snapshots_by_ticker.values())
    print(f"{total_snapshots:,} snapshots across {total_tickers} tickers")

    if total_snapshots == 0:
        print("  Nothing to simulate.")
        return

    # Find date range for downloads
    all_dates  = [s["backtest_date"] for snaps in snapshots_by_ticker.values() for s in snaps]
    start_date = min(all_dates)
    end_date   = max(all_dates)

    print(f"  Period:   {start_date} -> {end_date}")
    print(f"  Download: {total_tickers} tickers (one each)\n")

    start_time      = time.time()
    total_saved     = 0
    total_no_data   = 0
    total_errors    = 0
    ticker_count    = 0

    for ticker, snapshots in snapshots_by_ticker.items():
        ticker_count += 1

        # Progress header per ticker
        elapsed  = time.time() - start_time
        rate     = ticker_count / elapsed if elapsed > 0 else 0
        eta_sec  = int((total_tickers - ticker_count) / rate) if rate > 0 else 0
        print(f"  [{ticker_count:>3}/{total_tickers}] {ticker:<8} "
              f"({len(snapshots)} snapshots) | "
              f"ETA {eta_sec//60}m{eta_sec%60}s", end=" ", flush=True)

        # Download full history for this ticker
        price_history = download_full_history(ticker, start_date, end_date)

        if not price_history:
            print(f"-> DOWNLOAD FAILED")
            total_no_data += len(snapshots)
            # Save NO_DATA for all snapshots of this ticker
            for snapshot in snapshots:
                try:
                    save_outcome(
                        snapshot_id=snapshot["id"],
                        strategy=strategy_name,
                        exit_day=None, exit_price=None,
                        pct_change=None, was_successful=None,
                        exit_reason="NO_DATA",
                    )
                except Exception:
                    pass
            continue

        # Pre-sort all dates in price history for efficient lookup
        all_price_dates = sorted(price_history.keys())

        # Process each snapshot for this ticker
        ticker_saved  = 0
        ticker_errors = 0

        for snapshot in snapshots:
            entry_date  = snapshot["backtest_date"]
            entry_price = snapshot["price"]
            snapshot_id = snapshot["id"]

            # Get trading days after entry_date (up to max_days + buffer)
            max_days     = params.get("max_days", 30)
            forward_dates = [
                d for d in all_price_dates
                if d > entry_date
            ][:max_days + 5]

            if not forward_dates:
                total_no_data += 1
                save_outcome(
                    snapshot_id=snapshot_id, strategy=strategy_name,
                    exit_day=None, exit_price=None,
                    pct_change=None, was_successful=None,
                    exit_reason="NO_DATA",
                )
                continue

            # Evaluate strategy using pre-downloaded data
            try:
                result = evaluator(entry_price, price_history, forward_dates, params)
            except Exception as e:
                ticker_errors += 1
                total_errors  += 1
                continue

            # Save outcome
            try:
                save_outcome(
                    snapshot_id=snapshot_id,
                    strategy=strategy_name,
                    exit_day=result["exit_day"],
                    exit_price=result["exit_price"],
                    pct_change=result["pct_change"],
                    was_successful=result["was_successful"],
                    exit_reason=result["exit_reason"],
                )
                ticker_saved += 1
                total_saved  += 1
            except Exception as e:
                ticker_errors += 1
                total_errors  += 1

        print(f"-> saved:{ticker_saved} errors:{ticker_errors}")
        time.sleep(API_DELAY)

    elapsed = int(time.time() - start_time)
    print(f"\n{'=' * 60}")
    print(f"  SIMULATION COMPLETE — {strategy_name}")
    print(f"{'=' * 60}")
    print(f"  Saved:    {total_saved:,}")
    print(f"  No data:  {total_no_data:,}")
    print(f"  Errors:   {total_errors:,}")
    print(f"  Duration: {elapsed//60}m {elapsed%60}s")
    print(f"{'=' * 60}\n")
    print(f"  Run audit to see results:")
    print(f"    python audit.py --strategy {strategy_name}\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="V2 Simulate — evaluate strategy outcomes (optimized)"
    )
    parser.add_argument("--strategy", default=None,
                        help="Strategy JSON file (default: all in strategies/)")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Filter by ticker")
    parser.add_argument("--resume", action="store_true",
                        help="Skip already simulated snapshots")

    args = parser.parse_args()

    if args.strategy:
        strategy_files = [args.strategy]
    else:
        strategy_files = sorted(glob.glob(os.path.join(STRATEGIES_DIR, "*.json")))

    if not strategy_files:
        print(f"  No strategy files found in {STRATEGIES_DIR}/")
        sys.exit(1)

    for path in strategy_files:
        try:
            run_simulate(
                strategy_path=path,
                tickers=args.tickers,
                resume=args.resume,
            )
        except KeyboardInterrupt:
            print(f"\n\n  Simulation interrupted.")
            print(f"  Resume with: python simulate.py --strategy {path} --resume\n")
            break