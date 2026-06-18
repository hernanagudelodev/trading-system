"""
fix_weekend_expirations.py
==========================
Corrige las expiraciones guardadas en fin de semana (bug del LLM que elegía
sábados). Sábado -> viernes anterior, domingo -> viernes anterior.
Las opciones vencen viernes; el viernes anterior es la expiración real.

Uso:
    python fix_weekend_expirations.py            # dry-run
    python fix_weekend_expirations.py --commit   # aplica
"""
import os
import sys
from datetime import timedelta
import psycopg2
from dotenv import load_dotenv

load_dotenv()
COMMIT = "--commit" in sys.argv

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()
cur.execute("""
    SELECT id, ticker, expiration
    FROM paper_positions
    WHERE UPPER(status) = 'OPEN'
    ORDER BY id
""")
rows = cur.fetchall()

fixes = []
for pid, ticker, exp in rows:
    wd = exp.weekday()           # 5 = sábado, 6 = domingo
    if wd == 5:
        new_exp = exp - timedelta(days=1)
    elif wd == 6:
        new_exp = exp - timedelta(days=2)
    else:
        continue                 # ya es día hábil, no se toca
    fixes.append((pid, ticker, exp, new_exp))

if not fixes:
    print("\n  No hay expiraciones de fin de semana. Nada que corregir.\n")
    cur.close(); conn.close(); sys.exit(0)

print(f"\n  Se corregirán {len(fixes)} expiraciones:")
for pid, ticker, old, new in fixes:
    print(f"    id {pid:<4} {ticker:<6} {old} ({old.strftime('%A')}) -> "
          f"{new} ({new.strftime('%A')})")

if not COMMIT:
    print("\n  DRY-RUN — no se escribió nada. Corré con --commit para aplicar.\n")
    cur.close(); conn.close(); sys.exit(0)

for pid, ticker, old, new in fixes:
    cur.execute("UPDATE paper_positions SET expiration = %s WHERE id = %s", (new, pid))
conn.commit()
print(f"\n  ✅ {len(fixes)} expiraciones corregidas a viernes.\n")
cur.close()
conn.close()