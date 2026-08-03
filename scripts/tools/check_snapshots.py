import os, psycopg2
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

print("=== COLUMNAS de account_snapshots ===")
cur.execute("""
    SELECT column_name, data_type FROM information_schema.columns
    WHERE table_name = 'account_snapshots'
    ORDER BY ordinal_position
""")
for r in cur.fetchall():
    print(f"  {r[0]:<25} {r[1]}")

print("\n=== ÚLTIMAS 3 FILAS ===")
cur.execute("SELECT * FROM account_snapshots ORDER BY 1 DESC LIMIT 3")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    print(dict(zip(cols, row)))

cur.close(); conn.close()