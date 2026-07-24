"""
migrate_close_rationale.py
==========================
Agrega la columna `close_rationale TEXT` a `positions` y `paper_positions`.

POR QUÉ
    El motivo de cierre que escribe el LLM son ~450 caracteres de prosa, y se
    estaba metiendo en `close_reason varchar(50)`. Reventaba el INSERT y el
    cierre no se registraba (DLTR, 2026-07-23, live y paper).

POR QUÉ NO SE ENSANCHA close_reason
    `close_reason` es una columna de CÓDIGO, no de texto: check_closed.py y las
    métricas de expectativa agrupan por ella (STOP_LOSS, TARGET_REACHED,
    MANUAL, CLOSED_LIVE...). Meterle prosa rompe todo GROUP BY sin lanzar
    error — degradación silenciosa, justo lo que el proyecto persigue.
    Código corto en close_reason; prosa en close_rationale.

SEGURIDAD
    ADD COLUMN IF NOT EXISTS de una columna NULLABLE: no reescribe la tabla,
    no toca filas existentes, no rompe el código actual (que la ignora).
    Es idempotente: correrlo dos veces no hace nada la segunda vez.

ORDEN
    Esta migración va ANTES del cambio de código. Al revés, el primer cierre
    escribe contra una columna inexistente.

USO
    python migrate_close_rationale.py            # dry-run: muestra el estado
    python migrate_close_rationale.py --commit   # aplica
"""
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

TABLES = ("positions", "paper_positions")
COLUMN = "close_rationale"


def column_exists(cur, table, column):
    cur.execute("""
        SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
    """, (table, column))
    row = cur.fetchone()
    return row[0] if row else None


def main():
    commit = "--commit" in sys.argv

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: falta DATABASE_URL")
        return 1

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    print()
    print(f"  {'tabla':<18} {'close_rationale':>18}")
    print(f"  {'-'*18} {'-'*18}")

    faltantes = []
    for table in TABLES:
        tipo = column_exists(cur, table, COLUMN)
        print(f"  {table:<18} {(tipo or 'NO EXISTE'):>18}")
        if tipo is None:
            faltantes.append(table)
    print()

    if not faltantes:
        print("  Nada que hacer: la columna ya existe en ambas tablas.")
        cur.close(); conn.close()
        return 0

    if not commit:
        print(f"  DRY RUN — se agregaría en: {', '.join(faltantes)}")
        print(f"  Correr con --commit para aplicar.")
        cur.close(); conn.close()
        return 0

    for table in faltantes:
        cur.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {COLUMN} TEXT"
        )
        print(f"  ALTER TABLE {table} — columna agregada.")

    conn.commit()

    # Verificar contra el esquema, no confiar en que el ALTER no lanzó error.
    print()
    ok = True
    for table in TABLES:
        tipo = column_exists(cur, table, COLUMN)
        estado = tipo or "FALTA"
        print(f"  verificado · {table:<18} {estado}")
        if tipo is None:
            ok = False

    cur.close(); conn.close()
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())