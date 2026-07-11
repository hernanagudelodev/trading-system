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

limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 20

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

cur.execute("""
    SELECT run_at, slot, opened, closed, errors, run_time_sec
    FROM auto_run_logs
    ORDER BY run_at DESC
    LIMIT %s
""", (limit,))
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
cur.execute("""
    SELECT DATE(run_at) AS dia, COUNT(*) AS runs
    FROM auto_run_logs
    WHERE run_at >= NOW() - INTERVAL '10 days'
    GROUP BY DATE(run_at)
    ORDER BY dia DESC
""")
print(f"\n  Runs por día (últimos 10 días) — esperado 2 por día hábil:")
for dia, n in cur.fetchall():
    flag = "  ⚠️ menos de 2" if n < 2 else ""
    print(f"    {dia}: {n}{flag}")

print()
cur.close()
conn.close()