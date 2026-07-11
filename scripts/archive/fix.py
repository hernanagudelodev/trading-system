"""
check_hood.py — reconstruye qué pasó con HOOD el 6-jul (apertura -> stop en 90 min).
Solo lectura.
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

# Posición HOOD (stop del 6-jul)
cur.execute("""
    SELECT id, ticker, strike_low, strike_high, premium_paid,
           current_spread_value, gross_pnl, pnl_pct,
           opened_at, closed_at, last_synced_at
    FROM paper_positions
    WHERE ticker='HOOD' AND close_reason='STOP_LOSS'
    ORDER BY closed_at DESC LIMIT 1
""")
row = cur.fetchone()
if not row:
    print("\n  No se encontró el stop de HOOD.\n"); cur.close(); conn.close(); raise SystemExit

cols = [d[0] for d in cur.description]
rec  = dict(zip(cols, row))
print("\n  === POSICIÓN HOOD ===")
for k in cols:
    print(f"    {k:22}: {rec[k]}")

dur = None
if rec["opened_at"] and rec["closed_at"]:
    dur = (rec["closed_at"] - rec["opened_at"])
    print(f"\n    duración abierta      : {dur}")

credito = abs(float(rec["premium_paid"]))
cierre  = float(rec["current_spread_value"] or 0)
ancho   = float(rec["strike_high"]) - float(rec["strike_low"])
print(f"    crédito entrada       : ${credito:.2f}")
print(f"    spread al cerrar      : ${cierre:.2f}")
print(f"    ancho                 : ${ancho:.2f}")
print(f"    → el spread pasó de ${credito:.2f} a ${cierre:.2f} "
      f"({(cierre/credito-1)*100:+.0f}%)")

# Contexto de entrada (si existe)
cur.execute("""
    SELECT * FROM trade_context WHERE paper_position_id = %s
""", (rec["id"],))
ctx = cur.fetchone()
if ctx:
    ccols = [d[0] for d in cur.description]
    print("\n  === CONTEXTO DE ENTRADA (trade_context) ===")
    for k, v in zip(ccols, ctx):
        print(f"    {k:22}: {v}")
else:
    print("\n  Sin trade_context para esta posición.")

print()
cur.close(); conn.close()