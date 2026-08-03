import os, psycopg2
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("""
    SELECT ticker, gross_pnl, total_commission, net_pnl
    FROM positions
    WHERE UPPER(status) = 'CLOSED'
      AND closed_at >= '2026-06-20'
    ORDER BY id
""")
rows = cur.fetchall()
print(f"{'ticker':<7} {'gross':>9} {'commis':>8} {'net':>9}")
sg = sc = sn = 0
for t, g, c, n in rows:
    g = float(g or 0); c = float(c or 0); n = float(n or 0)
    sg += g; sc += c; sn += n
    print(f"{t:<7} {g:>9.2f} {c:>8.2f} {n:>9.2f}")
print("-"*36)
print(f"{'TOTAL':<7} {sg:>9.2f} {sc:>8.2f} {sn:>9.2f}")
cur.close()
conn.close()