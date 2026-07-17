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

# ── DEDUPLICACIÓN ────────────────────────────────────────────────────────────
# En memoria: evita que el loop dispare dos veces el mismo slot en este proceso.
# NO sobrevive a un reinicio — para eso está _ya_corrio_hoy(), que pregunta a la
# DB. Railway reinicia el contenedor en cada deploy, y sin el chequeo de DB un
# deploy dentro de la ventana dispararía un segundo run del mismo slot.
_corridos_hoy = set()          # {(date, 'morning'), (date, 'afternoon')}

AUTO_RUN_SLOTS = [
    {"name": "morning",   "hour_et": 10, "minute_et": 0},
    {"name": "afternoon", "hour_et": 14, "minute_et": 30},
]

# El loop hace sleep(60) DESPUÉS de trabajar, y scheduled_run() tarda segundos
# priceando posiciones. O sea que cada vuelta son 60s + trabajo: el reloj deriva.
# Con la condición original —`et_now.minute == m`, un minuto exacto— basta con
# que una vuelta caiga a las 09:59:58 y la siguiente a las 10:01:03 para que el
# slot NO se dispare en todo el día, sin error y sin aviso. Pasó el 17-jul.
# La ventana lo absorbe; la deduplicación evita que dispare de más.
VENTANA_MIN = 15


def _get_et_time():
    """
    Hora en Nueva York, con zoneinfo — no con una aproximación.

    La versión anterior hacía:
        offset = -4 if 3 <= month <= 11 else -5
    EE.UU. sale del horario de verano el PRIMER DOMINGO DE NOVIEMBRE, así que
    con esa cuenta todo noviembre quedaba en EDT y el auto_run habría corrido
    una hora antes de lo previsto — 9:00 ET en vez de 10:00. zoneinfo es stdlib
    y conoce las reglas de verdad.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo("America/New_York"))


def _ya_corrio_hoy(slot_name, hoy_et):
    """
    ¿Ya hay un run de este slot hoy en auto_run_logs?

    Sobrevive a un reinicio, que es justo lo que la marca en memoria no hace.
    run_at está en UTC; se compara contra el rango del día en ET.

    Ante un error de DB devuelve False: preferimos un run de más a ninguno.
    Un run duplicado lo frena el bloqueo de concentración; un run que falta no
    lo frena nadie.
    """
    try:
        import os
        import psycopg2
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur  = conn.cursor()
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
    """
    Devuelve el nombre del slot si toca correr, o None.
    """
    et_now  = _get_et_time()
    today   = et_now.date()

    if et_now.weekday() >= 5:
        return None

    ahora_min = et_now.hour * 60 + et_now.minute

    for slot in AUTO_RUN_SLOTS:
        slot_min = slot["hour_et"] * 60 + slot["minute_et"]
        atraso   = ahora_min - slot_min

        # Ventana, no un minuto exacto.
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
    """Import and run auto_run.main() in-process."""
    et_now = _get_et_time()
    slot   = should_run_auto()
    if not slot:
        return

    # Se marca ANTES de correr: si auto_run revienta, no se reintenta en bucle
    # dentro de la ventana.
    _corridos_hoy.add((et_now.date(), slot))

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