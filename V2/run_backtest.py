"""
run_backtest.py
===============
Railway wrapper for v2/backtest.py.

Runs once and exits cleanly (exit code 0).
Railway does not restart workers that exit with code 0.

Usage (Railway start command):
    python v2/run_backtest.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from backtest import run_backtest, print_summary, load_tickers

print("Starting v2 backtest on Railway...")

ticker_sector = load_tickers()
tickers       = list(ticker_sector.keys())

print(f"Loaded {len(tickers)} tickers. Starting backtest...")

run_backtest(
    tickers=tickers,
    ticker_sector=ticker_sector,
    lookback_days=365,
    resume=True,
)

print("Backtest complete. Exiting.")
sys.exit(0)