"""
run_monitor.py  (v2-bidi)
=========================
Wrapper de Railway — un worker que corre dos cosas en el mismo proceso:
    1. Monitor de posiciones (loop adaptativo, siempre activo): 5min mercado
       abierto / 10 pre-market / 30 cerrado. Precia, evalúa stop/target/DTE,
       alerta por Telegram, y toma snapshots de capital para el dashboard.
    2. Auto_run — el ciclo completo (scanner→selection→opener→resumen), 4 veces
       al día en horario de mercado.

Slots del auto_run (ET): 10:00, 12:00, 14:00, 15:30 — post-apertura, mediodía,
tarde, pre-cierre. Solo días hábiles.

Start command en Railway:
    python scripts/run_monitor.py

Variables de entorno (en .env.v2):
    DATABASE_URL, TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MONITOR_AUTO_CLOSE
    (opcional: HEALTHCHECK_URL para el dead-man's switch externo)
"""
import os
import sys
import time
import subprocess
import schedule
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_THIS_DIR  = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_THIS_DIR))

_ENV_NAME  = os.getenv("ENV_FILE", ".env.v2")
_ENV_PATH  = Path(_ENV_NAME)
if not _ENV_PATH.is_absolute():
    _ENV_PATH = _REPO_ROOT / _ENV_PATH
load_dotenv(_ENV_PATH)

from monitor import (
    scheduled_run,
    healthcheck_ping,
    get_interval,
    INTERVAL_MARKET_OPEN,
    INTERVAL_PRE_MARKET,
    INTERVAL_MARKET_CLOSED,
)

# ══════════════════════════════════════════════════════════════════════════════
# AUTO RUN SCHEDULING — 4 slots (ET)
# ══════════════════════════════════════════════════════════════════════════════

# Dedup en memoria: evita disparar dos veces el mismo slot en este proceso. NO
# sobrevive a un reinicio — para eso está _ya_corrio_hoy() (pregunta a la DB), que
# cubre el caso de un redeploy de Railway dentro de la ventana de un slot.
_corridos_hoy = set()

AUTO_RUN_SLOTS = [
    {"name": "open",     "hour_et": 10, "minute_et": 0},   # post-apertura
    {"name": "midday",   "hour_et": 12, "minute_et": 0},   # mediodía
    {"name": "afternoon","hour_et": 14, "minute_et": 0},   # tarde
    {"name": "preclose", "hour_et": 15, "minute_et": 30},  # pre-cierre
]

# Ventana de disparo (no un minuto exacto): el loop hace sleep(60)+trabajo, el
# reloj deriva, y un slot podría no caer nunca en el minuto justo. La ventana lo
# absorbe; la deduplicación evita que dispare de más.
VENTANA_MIN = 15


def _get_et_time():
    """Hora en Nueva York con zoneinfo (maneja DST de verdad, no aproximación)."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo("America/New_York"))


def _ya_corrio_hoy(slot_name, hoy_et):
    """
    ¿Ya hay un run de este slot hoy en auto_run_logs? Sobrevive a un reinicio.
    Ante error de DB devuelve False: preferimos un run de más (que los gates de
    no-apilar absorben) a ninguno.
    """
    try:
        import psycopg2
        conn = psycopg2.connect(os.getenv("DATABASE_URL")); cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM auto_run_logs
            WHERE slot = %s
              AND (run_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date = %s
        """, (slot_name, hoy_et))
        n = cur.fetchone()[0]
        cur.close(); conn.close()
        return n > 0
    except Exception as e:
        print(f"  ⚠️  no se pudo verificar si {slot_name} ya corrió ({e}) — se asume que no")
        return False


def should_run_auto():
    """Devuelve el nombre del slot si toca correr, o None."""
    et_now = _get_et_time()
    today  = et_now.date()
    if et_now.weekday() >= 5:
        return None

    ahora_min = et_now.hour * 60 + et_now.minute
    for slot in AUTO_RUN_SLOTS:
        slot_min = slot["hour_et"] * 60 + slot["minute_et"]
        atraso   = ahora_min - slot_min
        if not (0 <= atraso < VENTANA_MIN):
            continue
        if (today, slot["name"]) in _corridos_hoy:
            return None
        if _ya_corrio_hoy(slot["name"], today):
            _corridos_hoy.add((today, slot["name"]))
            return None
        if atraso > 0:
            print(f"  ⏱  slot '{slot['name']}' con {atraso}min de atraso — se dispara igual")
        return slot["name"]
    return None


def run_auto():
    """Corre auto_run.py como subprocess (aislado) para el slot que toque."""
    slot = should_run_auto()
    if not slot:
        return
    et_now = _get_et_time()
    # Se marca ANTES de correr: si auto_run revienta, no se reintenta en bucle
    # dentro de la ventana.
    _corridos_hoy.add((et_now.date(), slot))

    print(f"\n{'═'*55}")
    print(f"  AUTO RUN ({slot}) — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═'*55}\n")
    try:
        subprocess.run([sys.executable, "auto_run.py", "--slot", slot], cwd=str(_THIS_DIR))
    except Exception as e:
        import traceback
        print(f"\n  AUTO RUN ERROR: {e}")
        traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{'═'*55}")
print(f"  MONITOR + AUTO RUN v2 — Railway Worker")
print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"{'═'*55}")
print(f"  Monitor: {INTERVAL_MARKET_OPEN}min (open) / {INTERVAL_PRE_MARKET}min (pre) / "
      f"{INTERVAL_MARKET_CLOSED}min (closed)")
print(f"  Auto run: 10:00 · 12:00 · 14:00 · 15:30 ET (weekdays)")
print(f"{'═'*55}\n")

# Corre el monitor una vez al arrancar
scheduled_run()

current_interval = get_interval()
schedule.every(current_interval).minutes.do(scheduled_run)
print(f"  ⏱  Initial interval: {current_interval}min\n")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

while True:
    schedule.run_pending()
    run_auto()
    healthcheck_ping()          # dead-man's switch (avisa desde afuera si el worker muere)

    new_interval = get_interval()
    if new_interval != current_interval:
        schedule.clear()
        schedule.every(new_interval).minutes.do(scheduled_run)
        current_interval = new_interval
        print(f"  ⏱  Interval updated → {current_interval}min "
              f"({datetime.now().strftime('%H:%M')})")

    time.sleep(60)