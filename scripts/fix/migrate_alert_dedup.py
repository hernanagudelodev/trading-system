"""
migrate_alert_dedup.py
======================
Agrega las columnas que permiten deduplicar alertas del monitor:

    last_alert_level    VARCHAR(20)   ultimo nivel notificado (WATCH/ACTION/URGENT)
    last_alert_at       TIMESTAMPTZ   cuando se envio ese ultimo aviso
    last_alert_pnl_pct  DOUBLE PRECISION   el pnl_pct de ese aviso

en `positions` y `paper_positions`.

POR QUE
    Hoy el monitor manda push CADA ciclo mientras una posicion este en
    WATCH/ACTION/URGENT. Con mercado abierto son cada 5 min: una posicion que
    se queda en WATCH toda la tarde manda decenas de notificaciones iguales y
    degrada el canal por donde tambien llegan las URGENT de plata real (fatiga
    de alertas, 22.2).

    Con estas columnas el monitor puede decidir NO repetir:
      - re-alerta si cambia de nivel               (last_alert_level)
      - re-alerta si empeora >=10 puntos de pnl_pct (last_alert_pnl_pct)
      - re-alerta si paso >=1h desde el ultimo      (last_alert_at)
      - se resetea al volver a NORMAL

SEGURIDAD
    ADD COLUMN IF NOT EXISTS, todas NULLABLE: no reescribe la tabla, no toca
    filas existentes, no rompe el codigo actual (que las ignora). Idempotente.

ORDEN
    Va ANTES del cambio de monitor.py. Al reves, el monitor escribe contra
    columnas que no existen.

USO
    python migrate_alert_dedup.py            # dry-run
    python migrate_alert_dedup.py --commit   # aplica
"""
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

TABLES  = ("positions", "paper_positions")
COLUMNS = (
    ("last_alert_level",   "VARCHAR(20)"),
    ("last_alert_at",      "TIMESTAMPTZ"),
    ("last_alert_pnl_pct", "DOUBLE PRECISION"),
)


def col_type(cur, table, column):
    cur.execute("""
        SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
    """, (table, column))
    r = cur.fetchone()
    return r[0] if r else None


def main():
    commit = "--commit" in sys.argv
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: falta DATABASE_URL")
        return 1

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    print()
    print(f"  {'tabla':<18} {'columna':<20} {'estado':<12}")
    print(f"  {'-'*18} {'-'*20} {'-'*12}")
    faltantes = []
    for table in TABLES:
        for col, _ in COLUMNS:
            tipo = col_type(cur, table, col)
            print(f"  {table:<18} {col:<20} {tipo or 'NO EXISTE':<12}")
            if tipo is None:
                faltantes.append((table, col))
    print()

    if not faltantes:
        print("  Nada que hacer: las columnas ya existen en ambas tablas.")
        cur.close(); conn.close()
        return 0

    if not commit:
        print(f"  DRY RUN — faltan {len(faltantes)}. Correr con --commit para aplicar.")
        cur.close(); conn.close()
        return 0

    tipos = dict(COLUMNS)
    for table, col in faltantes:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {tipos[col]}")
        print(f"  ALTER {table}.{col}")
    conn.commit()

    # Verificar contra el esquema, no confiar en que el ALTER no lanzo error.
    print()
    ok = True
    for table in TABLES:
        for col, _ in COLUMNS:
            tipo = col_type(cur, table, col)
            print(f"  verificado · {table}.{col:<20} {tipo or 'FALTA'}")
            if tipo is None:
                ok = False

    cur.close(); conn.close()
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())