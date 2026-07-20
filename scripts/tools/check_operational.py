"""
check_operational.py
====================
Salud operativa del auto_run: ¿corrió cada slot? ¿abrió/cerró? ¿errores?
Solo lectura — no modifica nada.

Uso:
    python check_operational.py            # últimos 20 runs
    python check_operational.py 40         # últimos N runs
"""
import os
import sys
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# El libro filtra por la columna `mode` (auto_run_logs la tiene); sin él, ambos.
_args = [a.lower() for a in sys.argv[1:]]
book  = "live" if "live" in _args else ("paper" if "paper" in _args else None)
_nums = [a for a in _args if a.isdigit()]
limit = int(_nums[0]) if _nums else 20
_mode_where = "WHERE mode = %s" if book else ""
_mode_p     = (book,) if book else ()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

cur.execute(f"""
    SELECT run_at, slot, opened, closed, errors, run_time_sec
    FROM auto_run_logs
    {_mode_where}
    ORDER BY run_at DESC
    LIMIT %s
""", (*_mode_p, limit))
rows = cur.fetchall()

if not rows:
    print("\n  No hay registros en auto_run_logs.\n")
    cur.close(); conn.close(); sys.exit(0)

print(f"\n  Últimos {len(rows)} runs (más reciente arriba):\n")
print(f"  {'run_at':<20} {'slot':<10} {'open':>4} {'close':>5} {'err':>4} {'seg':>5}")
print(f"  {'-'*20} {'-'*10} {'-'*4} {'-'*5} {'-'*4} {'-'*5}")

total_runs   = len(rows)
runs_con_err = 0
runs_abrieron = 0
total_open   = 0
total_close  = 0

for run_at, slot, opened, closed, errors, secs in rows:
    o = opened or 0
    c = closed or 0
    e = errors or 0
    flag = "  ⚠️" if e else ""
    ts = run_at.strftime("%Y-%m-%d %H:%M") if hasattr(run_at, "strftime") else str(run_at)
    print(f"  {ts:<20} {str(slot):<10} {o:>4} {c:>5} {e:>4} {str(secs or '-'):>5}{flag}")
    if e: runs_con_err += 1
    if o: runs_abrieron += 1
    total_open  += o
    total_close += c

print(f"\n  Resumen ventana:")
print(f"    Runs totales         : {total_runs}")
print(f"    Runs con error       : {runs_con_err}")
print(f"    Runs que abrieron     : {runs_abrieron}")
print(f"    Aperturas acumuladas : {total_open}")
print(f"    Cierres acumulados   : {total_close}")

# Chequeo de cadencia: ¿hay días hábiles sin sus 2 slots?
cur.execute(f"""
    SELECT DATE(run_at) AS dia, COUNT(*) AS runs
    FROM auto_run_logs
    WHERE run_at >= NOW() - INTERVAL '10 days'
    {"AND mode = %s" if book else ""}
    GROUP BY DATE(run_at)
    ORDER BY dia DESC
""", _mode_p)
print(f"\n  Runs por día (últimos 10 días) — esperado 2 por día hábil:")
for dia, n in cur.fetchall():
    flag = "  ⚠️ menos de 2" if n < 2 else ""
    print(f"    {dia}: {n}{flag}")

print()
cur.close()
conn.close()