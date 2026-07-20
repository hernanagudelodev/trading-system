"""
check_runs.py — auditoría de decisiones del auto_run (lee auto_run_logs).
Muestra el razonamiento COMPLETO (summary / no_trade_reason), sin el truncado
del push. Solo lectura.

Uso:
    python check_runs.py                 # runs de HOY
    python check_runs.py 4               # últimos 4 runs
    python check_runs.py 2026-07-09      # todos los runs de ese día
    python check_runs.py 123             # un run por id (número > 31 se trata como id)
    python check_runs.py 2026-07-09 --log   # además imprime el full_log completo
"""
import os
import sys
import psycopg2
from datetime import date
from dotenv import load_dotenv

load_dotenv()

# El libro es un FILTRO opcional sobre auto_run_logs (que tiene columna `mode`),
# no una tabla que elegir. Sin él, se ven los dos libros.
raw      = [a.lower() for a in sys.argv[1:]]
show_log = "--log" in raw
book     = "live" if "live" in raw else ("paper" if "paper" in raw else None)
args     = [a for a in raw if a not in ("--log", "live", "paper")]
arg      = args[0] if args else None

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

# Resolver el filtro
if arg is None:
    where, params, titulo = "DATE(run_at) = %s", (date.today(),), f"runs de HOY ({date.today()})"
elif "-" in str(arg):                       # fecha YYYY-MM-DD
    where, params, titulo = "DATE(run_at) = %s", (arg,), f"runs del {arg}"
elif arg.isdigit() and int(arg) <= 31:      # últimos N
    where, params, titulo = "TRUE", (), f"últimos {arg} runs"
else:                                        # id puntual
    where, params, titulo = "id = %s", (int(arg),), f"run id {arg}"

# Acoplar el filtro de libro, si se pidió
if book:
    where  += " AND mode = %s"
    params  = (*params, book)
    titulo += f" · {book.upper()}"

limit = f"LIMIT {int(arg)}" if (arg and arg.isdigit() and int(arg) <= 31) else ""

cur.execute(f"""
    SELECT id, run_at, slot, verdict, vix, opened, closed, errors,
           summary, no_trade_reason, run_time_sec, full_log
    FROM auto_run_logs
    WHERE {where}
    ORDER BY run_at DESC
    {limit}
""", params)
rows = cur.fetchall()

if not rows:
    print(f"\n  Sin runs para: {titulo}\n")
    cur.close(); conn.close(); raise SystemExit

print(f"\n  ═══ {titulo} — {len(rows)} run(s) ═══")

for (rid, run_at, slot, verdict, vix, opened, closed, errors,
     summary, no_trade, secs, full_log) in rows:
    ts = run_at.strftime("%Y-%m-%d %H:%M") if hasattr(run_at, "strftime") else str(run_at)
    err_flag = f"  ⚠️ {errors} error(es)" if errors else ""
    print("\n" + "─" * 60)
    print(f"  #{rid} · {ts} · {slot} · {verdict} · VIX {vix}")
    print(f"  Abrió {opened} · Cerró {closed} · {secs}s{err_flag}")

    if summary:
        print(f"\n  RESUMEN:\n    {summary}")
    if no_trade:
        print(f"\n  NO-TRADE:\n    {no_trade}")
    if show_log and full_log:
        print(f"\n  FULL LOG:\n{full_log}")

print("\n" + "─" * 60)
print(f"  (usá --log para ver el registro completo de cada run)\n")

cur.close()
conn.close()