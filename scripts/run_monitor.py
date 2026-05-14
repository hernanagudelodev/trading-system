"""
run_monitor.py
==============
Railway wrapper for monitor.py.

Runs monitor in loop mode every 30 minutes during market hours.
Railway keeps this process alive indefinitely as a long-running worker.

Unlike run_backtest.py, this script never exits — it runs forever
so Railway keeps the worker alive.

Start command in Railway:
    python scripts/run_monitor.py

Environment variables required:
    DATABASE_URL
    ANTHROPIC_API_KEY
    EMAIL_FROM
    EMAIL_TO
    EMAIL_PASSWORD
"""

import sys
import os
import time
import schedule
from datetime import datetime

# Ensure scripts directory is in path
sys.path.insert(0, os.path.dirname(__file__))

from monitor import scheduled_run, is_market_open, MARKET_OPEN_HOUR, MARKET_OPEN_MIN, MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN

INTERVAL_MINUTES = 30

print(f"\n{'═' * 55}")
print(f"  MONITOR — Railway Worker")
print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"  Interval: every {INTERVAL_MINUTES} min during market hours")
print(f"  Market hours: {MARKET_OPEN_HOUR}:{MARKET_OPEN_MIN:02d} - {MARKET_CLOSE_HOUR}:{MARKET_CLOSE_MIN:02d} ET")
print(f"{'═' * 55}\n")

# Schedule recurring run
schedule.every(INTERVAL_MINUTES).minutes.do(scheduled_run)

# Run immediately on start
scheduled_run()

# Keep alive forever — Railway will restart if this crashes
while True:
    schedule.run_pending()
    time.sleep(60)