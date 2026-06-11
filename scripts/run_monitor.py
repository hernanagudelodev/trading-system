"""
run_monitor.py
==============
Railway wrapper — monitor de posiciones + auto_run diario.

Corre dos procesos en el mismo worker:
    1. Monitor de posiciones (loop adaptativo, siempre activo)
    2. Auto_run (2 veces al día en horario de mercado)

Horarios del auto_run (ET):
    - 10:00am ET (30min después de apertura)
    - 2:30pm ET  (90min antes del cierre)

Start command en Railway:
    python scripts/run_monitor.py

Variables de entorno requeridas:
    DATABASE_URL
    ANTHROPIC_API_KEY
    TASTYTRADE_CLIENT_SECRET
    TASTYTRADE_REFRESH_TOKEN
    NTFY_TOPIC
"""

import sys
import os
import time
import schedule
from datetime import datetime, timezone

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

# ══════════════════════════════════════════════════════════════════════════════
# AUTO RUN SCHEDULING
# ══════════════════════════════════════════════════════════════════════════════

# Track last auto_run to avoid double-runs
_last_auto_run_date  = None
_last_auto_run_slot  = None  # 'morning' or 'afternoon'

AUTO_RUN_SLOTS = [
    {"name": "morning",   "hour_et": 10, "minute_et": 0},
    {"name": "afternoon", "hour_et": 14, "minute_et": 30},
]

def _get_et_time():
    """Get current time in ET (UTC-4 in summer, UTC-5 in winter)."""
    import datetime as dt
    utc_now = dt.datetime.now(dt.timezone.utc)
    # Simple DST approximation: EDT (UTC-4) Mar-Nov, EST (UTC-5) Nov-Mar
    month = utc_now.month
    offset = -4 if 3 <= month <= 11 else -5
    et_now = utc_now + dt.timedelta(hours=offset)
    return et_now


def should_run_auto():
    """
    Returns the slot name if it's time to run auto_run, else None.
    Runs once per slot per day — prevents double-runs from loop timing.
    """
    global _last_auto_run_date, _last_auto_run_slot

    et_now   = _get_et_time()
    today    = et_now.date()
    weekday  = et_now.weekday()

    # Only on weekdays
    if weekday >= 5:
        return None

    for slot in AUTO_RUN_SLOTS:
        h, m = slot["hour_et"], slot["minute_et"]

        # Window: exactly on the minute (loop checks every 60s)
        if et_now.hour == h and et_now.minute == m:
            # Check we haven't already run this slot today
            if _last_auto_run_date == today and _last_auto_run_slot == slot["name"]:
                return None
            return slot["name"]

    return None


def run_auto():
    """Import and run auto_run.main() in-process."""
    global _last_auto_run_date, _last_auto_run_slot

    et_now = _get_et_time()
    slot   = should_run_auto()
    if not slot:
        return

    _last_auto_run_date = et_now.date()
    _last_auto_run_slot = slot

    print(f"\n{'═' * 55}")
    print(f"  AUTO RUN ({slot}) — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═' * 55}\n")

    try:
        os.environ["AUTO_RUN_SLOT"] = slot
        import auto_run
        import importlib
        importlib.reload(auto_run)
        auto_run.main()
    except Exception as e:
        import traceback
        print(f"\n  AUTO RUN ERROR: {e}")
        traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{'═' * 55}")
print(f"  MONITOR + AUTO RUN — Railway Worker")
print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"{'═' * 55}")
print(f"  Monitor: every {INTERVAL_MARKET_OPEN}min (open) / "
      f"{INTERVAL_PRE_MARKET}min (pre) / "
      f"{INTERVAL_MARKET_CLOSED}min (closed)")
print(f"  Auto run: 10:00am ET + 2:30pm ET (weekdays only)")
print(f"{'═' * 55}\n")

# Run monitor immediately on start
scheduled_run()

# Set initial schedule
current_interval = get_interval()
schedule.every(current_interval).minutes.do(scheduled_run)
print(f"  ⏱  Initial interval: {current_interval}min\n")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

while True:
    schedule.run_pending()

    # Check if auto_run should fire
    run_auto()

    # Adaptive interval update
    new_interval = get_interval()
    if new_interval != current_interval:
        schedule.clear()
        schedule.every(new_interval).minutes.do(scheduled_run)
        current_interval = new_interval
        print(f"  ⏱  Interval updated → {current_interval}min "
              f"({datetime.now().strftime('%H:%M')})")

    time.sleep(60)