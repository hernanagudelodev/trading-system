"""
run_monitor.py
==============
Railway wrapper for monitor.py.

Uses adaptive frequency:
    - Market open (9:30-16:00 ET):  every 5 minutes
    - Pre-market (8:30-9:30 ET):    every 10 minutes
    - Market closed:                every 30 minutes

Heartbeat (inside monitor.py):
    - Every 60 min during market hours → ntfy status notification
    - At market close → one summary notification

Never exits — Railway keeps this worker alive indefinitely.

Start command in Railway:
    python scripts/run_monitor.py

Environment variables required:
    DATABASE_URL
    ANTHROPIC_API_KEY
    NTFY_TOPIC
"""

import sys
import os
import time
import schedule
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from monitor import (
    scheduled_run,
    get_interval,
    INTERVAL_MARKET_OPEN,
    INTERVAL_PRE_MARKET,
    INTERVAL_MARKET_CLOSED,
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MIN,
    MARKET_CLOSE_HOUR,
    MARKET_CLOSE_MIN,
)

print(f"\n{'═' * 55}")
print(f"  MONITOR — Railway Worker")
print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"  Market open:   every {INTERVAL_MARKET_OPEN}min")
print(f"  Pre-market:    every {INTERVAL_PRE_MARKET}min")
print(f"  Market closed: every {INTERVAL_MARKET_CLOSED}min")
print(f"  Hours: {MARKET_OPEN_HOUR}:{MARKET_OPEN_MIN:02d} - "
      f"{MARKET_CLOSE_HOUR}:{MARKET_CLOSE_MIN:02d} ET")
print(f"  Heartbeat: every 60min during market hours (ntfy)")
print(f"{'═' * 55}\n")

# Run immediately on start
scheduled_run()

# Set initial schedule based on current market status
current_interval = get_interval()
schedule.every(current_interval).minutes.do(scheduled_run)
print(f"  ⏱  Initial interval: {current_interval}min\n")

# Keep alive — check every minute if interval needs updating
while True:
    schedule.run_pending()

    new_interval = get_interval()
    if new_interval != current_interval:
        schedule.clear()
        schedule.every(new_interval).minutes.do(scheduled_run)
        current_interval = new_interval
        print(f"  ⏱  Interval updated → {current_interval}min "
              f"({datetime.now().strftime('%H:%M')})")

    time.sleep(60)