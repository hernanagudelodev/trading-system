"""
migrate_mode.py
===============
Agrega la columna `mode` ('paper'|'live') a las tablas que hoy NO saben de qué
libro vienen: auto_run_logs y account_snapshots.

Por qué hace falta: con dos workers (paper y live) escribiendo en la MISMA DB,
los logs quedan intercalados y no hay forma de decir qué corrida fue de cuál.
En account_snapshots es peor: dos NLV distintos mezclados en la misma serie.

NO toca trade_context: esa tabla ya tiene el libro implícito en sus FKs
(position_id vs paper_position_id). Agregarle `mode` sería un dato que puede
contradecir a la FK.

Idempotente — seguro correr varias veces.

USO — dos pasos, en este orden:

    1) AHORA (antes de deployar código nuevo):
           python migrate_mode.py

       Deja DEFAULT 'paper' puesto. El worker paper que está corriendo ahora
       mismo no sabe de esta columna y sigue insertando sin romperse.

    2) DESPUÉS de deployar el código que inserta `mode` explícito:
           python migrate_mode.py --drop-defaults

       Quita el DEFAULT. A partir de ahí, un INSERT que se olvide de `mode`
       explota en vez de mentir. Mientras el DEFAULT exista, un worker live que
       no pase `mode` loguea como 'paper' y nadie se entera — que es el bug de
       junio otra vez (falta el dato, el sistema rellena solo, se ve normal).

       NO correr el paso 2 antes del deploy o se rompe paper.
"""
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

TABLES = ["auto_run_logs", "account_snapshots"]


def get_connection():
    if not DATABASE_URL:
        print("  ✗ DATABASE_URL no está en el entorno. Abortado.")
        sys.exit(1)
    return psycopg2.connect(DATABASE_URL)


def table_exists(cur, table):
    cur.execute("SELECT to_regclass(%s)", (table,))
    return cur.fetchone()[0] is not None


# ══════════════════════════════════════════════════════════════════════════════
# PASO 1 — agregar columna, backfill, NOT NULL, DEFAULT, CHECK
# ══════════════════════════════════════════════════════════════════════════════

def build_migrations(table):
    """
    Orden deliberado y idempotente:
      1. ADD COLUMN nullable  -> no falla si la tabla tiene filas
      2. UPDATE ... IS NULL   -> todo lo histórico queda 'paper'
      3. SET NOT NULL         -> solo pasa si el paso 2 no dejó NULLs
      4. SET DEFAULT 'paper'  -> temporal; lo quita --drop-defaults
      5. CHECK constraint     -> impide 'Live', 'paper ', typos
    """
    chk = f"{table}_mode_chk"
    return [
        (
            f"{table}: agregar columna mode",
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS mode VARCHAR(10);",
        ),
        (
            f"{table}: backfill histórico -> 'paper'",
            f"UPDATE {table} SET mode = 'paper' WHERE mode IS NULL;",
        ),
        (
            f"{table}: mode NOT NULL",
            f"ALTER TABLE {table} ALTER COLUMN mode SET NOT NULL;",
        ),
        (
            f"{table}: DEFAULT 'paper' (temporal)",
            f"ALTER TABLE {table} ALTER COLUMN mode SET DEFAULT 'paper';",
        ),
        (
            f"{table}: CHECK mode IN ('paper','live')",
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = '{chk}'
                ) THEN
                    ALTER TABLE {table}
                        ADD CONSTRAINT {chk} CHECK (mode IN ('paper','live'));
                END IF;
            END $$;
            """,
        ),
    ]


def build_drop_defaults(table):
    return [
        (
            f"{table}: quitar DEFAULT de mode",
            f"ALTER TABLE {table} ALTER COLUMN mode DROP DEFAULT;",
        ),
    ]


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def run(drop_defaults=False):
    conn = get_connection()
    cur = conn.cursor()

    modo = "DROP DEFAULTS" if drop_defaults else "MIGRACIÓN"
    print(f"\n  === {modo} — columna `mode` ===\n")

    ok = 0
    fallos = 0

    for table in TABLES:
        if not table_exists(cur, table):
            print(f"  ⊘ {table}: la tabla no existe — se omite")
            continue

        pasos = build_drop_defaults(table) if drop_defaults else build_migrations(table)

        for nombre, sql in pasos:
            try:
                cur.execute(sql)
                conn.commit()
                print(f"  ✓ {nombre}")
                ok += 1
            except Exception as e:
                conn.rollback()
                print(f"  ✗ {nombre}")
                print(f"      {e}")
                fallos += 1

    # ── Verificación: qué quedó realmente en la DB ────────────────────────────
    print(f"\n  === VERIFICACIÓN ===\n")

    for table in TABLES:
        if not table_exists(cur, table):
            continue

        try:
            cur.execute(f"SELECT mode, COUNT(*) FROM {table} GROUP BY mode ORDER BY mode")
            filas = cur.fetchall()
            total = sum(n for _, n in filas)
            print(f"  {table}: {total} filas")
            for m, n in filas:
                print(f"      mode='{m}': {n}")
            if not filas:
                print(f"      (tabla vacía)")
        except Exception as e:
            print(f"  ✗ {table}: no se pudo verificar — {e}")
            fallos += 1

        # Estado del DEFAULT — para saber en qué paso estás
        try:
            cur.execute("""
                SELECT column_default
                FROM information_schema.columns
                WHERE table_name = %s AND column_name = 'mode'
            """, (table,))
            r = cur.fetchone()
            dflt = r[0] if r else None
            estado = f"DEFAULT {dflt}" if dflt else "sin DEFAULT (fail-loud activo)"
            print(f"      {estado}")
        except Exception:
            pass

    cur.close()
    conn.close()

    print(f"\n  {ok} statements OK · {fallos} fallos")

    if not drop_defaults:
        print(
            "\n  SIGUIENTE: deployar el código que inserta `mode` explícito.\n"
            "  DESPUÉS de eso, y solo después:  python migrate_mode.py --drop-defaults\n"
        )
    else:
        print(
            "\n  DEFAULT quitado. Un INSERT sin `mode` ahora falla en vez de\n"
            "  loguear 'paper' en silencio.\n"
        )

    return fallos == 0


if __name__ == "__main__":
    drop = "--drop-defaults" in sys.argv
    exito = run(drop_defaults=drop)
    sys.exit(0 if exito else 1)