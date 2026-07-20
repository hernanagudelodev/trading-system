"""
schema.py
=========
Imprime el esquema de una tabla. Existe porque el visor de Railway pagina
de a 5 filas y hoy propuse dos columnas que no existían (`exit_price`,
`mode` en account_snapshots) por no poder mirar.

Uso:
    python scripts/tools/schema.py positions
    python scripts/tools/schema.py positions paper_positions
    python scripts/tools/schema.py --diff positions paper_positions
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def columnas(cur, tabla):
    cur.execute("""
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """, (tabla,))
    return cur.fetchall()


conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

args  = [a for a in sys.argv[1:] if not a.startswith("--")]
diff  = "--diff" in sys.argv

if not args:
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
    """)
    print("\n  TABLAS:\n")
    for (t,) in cur.fetchall():
        print(f"    {t}")
    print()
    raise SystemExit

if diff and len(args) == 2:
    a, b = args
    ca = {c[0]: c[1] for c in columnas(cur, a)}
    cb = {c[0]: c[1] for c in columnas(cur, b)}
    todas = sorted(set(ca) | set(cb))
    print(f"\n  {'columna':<26} {a:<18} {b:<18}")
    print(f"  {'-'*26} {'-'*18} {'-'*18}")
    for col in todas:
        ta = ca.get(col, "—")
        tb = cb.get(col, "—")
        marca = "" if col in ca and col in cb else "   <<<"
        print(f"  {col:<26} {ta:<18} {tb:<18}{marca}")
    print(f"\n  solo en {a}: {sorted(set(ca) - set(cb))}")
    print(f"  solo en {b}: {sorted(set(cb) - set(ca))}\n")
else:
    for t in args:
        cols = columnas(cur, t)
        if not cols:
            print(f"\n  ⛔ la tabla '{t}' no existe\n")
            continue
        print(f"\n  {t}  ({len(cols)} columnas)")
        print(f"  {'-'*70}")
        for nombre, tipo, nullable, default in cols:
            null = "" if nullable == "YES" else "  NOT NULL"
            dflt = f"  DEFAULT {default}" if default else ""
            print(f"    {nombre:<26} {tipo:<20}{null}{dflt}")
        print()

cur.close()
conn.close()