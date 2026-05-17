"""
run_simulate.py
===============
Railway wrapper for v2/simulate.py.

Runs all strategies in strategies/ against all 534 tickers.
Exits cleanly with code 0 when done.

Usage (Railway start command):
    python v2/run_simulate.py
"""

import sys
import os
import glob

sys.path.insert(0, os.path.dirname(__file__))

from simulate import run_simulate

STRATEGIES_DIR = os.path.join(os.path.dirname(__file__), "strategies")

strategy_files = sorted(glob.glob(os.path.join(STRATEGIES_DIR, "*.json")))

if not strategy_files:
    print("No strategy files found in strategies/")
    sys.exit(1)

print(f"Starting simulation for {len(strategy_files)} strategies...")

for path in strategy_files:
    print(f"\nRunning: {os.path.basename(path)}")
    run_simulate(
        strategy_path=path,
        tickers=None,   # all tickers
        resume=True,    # skip already simulated
    )

print("\nAll simulations complete. Exiting.")
sys.exit(0)