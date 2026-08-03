import os, psycopg2
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("""
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public'
    ORDER BY table_name
""")
print("=== TABLAS ===")
for r in cur.fetchall():
    print(" ", r[0])
cur.close(); conn.close()