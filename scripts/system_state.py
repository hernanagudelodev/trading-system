"""
scripts/system_state.py  (v2-bidi)
==================================
Acceso GENÉRICO a la tabla de parámetros de política del owner (mismo esquema
clave-valor que def: key TEXT PK, value TEXT, updated_at). Los parámetros que
gobiernan cómo opera el sistema —sizing, modo del dial, umbrales, riesgo— viven
acá, NO hardcodeados: se cambian sin tocar código ni redesplegar.

API para el código (getters TIPADOS — el que lee castea bien, sin olvidos):
    get_param(key, default=None)         -> str | None   (crudo)
    get_param_float(key, default)        -> float
    get_param_int(key, default)          -> int
    get_param_str(key, default)          -> str
    set_param(key, value)                -> None
    delete_param(key)                    -> int (filas borradas)
    list_params()                        -> list[(key, value, updated_at)]

Robustez: si la tabla no existe o la clave falta, los getters devuelven el
DEFAULT (no revientan). Así el sistema corre con defaults antes de configurar nada.

CLI (administración):
    python system_state.py --init                     # crea la tabla si no existe
    python system_state.py --list                     # muestra todos los parámetros
    python system_state.py --get neutral_size_factor  # muestra un parámetro
    python system_state.py --set neutral_size_factor 0.5
    python system_state.py --del neutral_size_factor  # borra un parámetro

VARIABLES DE ENTORNO (en .env.v2): DATABASE_URL
"""
import os
import sys
import argparse
from pathlib import Path

from dotenv import load_dotenv

_THIS_DIR  = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
_ENV_NAME  = os.getenv("ENV_FILE", ".env.v2")
_ENV_PATH  = Path(_ENV_NAME)
if not _ENV_PATH.is_absolute():
    _ENV_PATH = _REPO_ROOT / _ENV_PATH
load_dotenv(_ENV_PATH)


def _conn():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS system_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def init_table():
    conn = _conn(); cur = conn.cursor()
    ensure_table(cur)
    conn.commit(); cur.close(); conn.close()
    print("  system_state lista (creada o ya existía).")


# ══════════════════════════════════════════════════════════════════════════════
# LECTURA (para el código)
# ══════════════════════════════════════════════════════════════════════════════

def get_param(key):
    """
    Valor crudo (TEXT) de un parámetro, o None si no existe. Robusto: si la tabla
    aún no existe u ocurre un error, devuelve None (el que llama usa su default).
    """
    try:
        conn = _conn(); cur = conn.cursor()
        cur.execute("SELECT value FROM system_state WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return row[0] if row else None
    except Exception:
        return None


def get_param_float(key, default):
    v = get_param(key)
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def get_param_int(key, default):
    v = get_param(key)
    if v is None:
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def get_param_str(key, default):
    v = get_param(key)
    return v if v is not None else default


# ══════════════════════════════════════════════════════════════════════════════
# ESCRITURA (administración)
# ══════════════════════════════════════════════════════════════════════════════

def set_param(key, value):
    conn = _conn(); cur = conn.cursor()
    ensure_table(cur)
    cur.execute("""
        INSERT INTO system_state (key, value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    """, (key, str(value)))
    conn.commit(); cur.close(); conn.close()


def delete_param(key):
    conn = _conn(); cur = conn.cursor()
    ensure_table(cur)
    cur.execute("DELETE FROM system_state WHERE key = %s", (key,))
    n = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    return n


def list_params():
    conn = _conn(); cur = conn.cursor()
    ensure_table(cur)
    cur.execute("SELECT key, value, updated_at FROM system_state ORDER BY key")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Administra system_state (parámetros de política)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--init", action="store_true", help="crea la tabla si no existe")
    g.add_argument("--list", action="store_true", help="muestra todos los parámetros")
    g.add_argument("--get", metavar="KEY", help="muestra un parámetro")
    g.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"), help="fija un parámetro")
    g.add_argument("--del", dest="delete", metavar="KEY", help="borra un parámetro")
    a = p.parse_args()

    if not os.getenv("DATABASE_URL"):
        print(f"  ⛔ falta DATABASE_URL (revisá {_ENV_PATH})")
        return 1

    if a.init:
        init_table()
    elif a.list:
        rows = list_params()
        if not rows:
            print("  (sin parámetros — la tabla está vacía; el código usa defaults)")
        else:
            print(f"\n  {'key':<28} {'value':<12} updated_at")
            for k, v, ts in rows:
                print(f"  {k:<28} {v:<12} {ts}")
    elif a.get:
        v = get_param(a.get)
        print(f"  {a.get} = {v!r}" if v is not None else f"  {a.get}: (no está — el código usa su default)")
    elif a.set:
        set_param(a.set[0], a.set[1])
        print(f"  ✅ {a.set[0]} = {a.set[1]}")
    elif a.delete:
        n = delete_param(a.delete)
        print(f"  borrado: {a.delete} (filas: {n})")
    return 0


if __name__ == "__main__":
    sys.exit(main())