"""
scripts/tools/health.py  (v2-bidi)
==================================
Salud del sistema — responde "¿está todo funcionando?". Cinco chequeos:
    1. DB conecta
    2. Credenciales Tastytrade presentes
    3. Mercado abierto o cerrado (ahora, ET)
    4. Scan fresco (último ticker_study)
    5. Latido del monitor (último snapshot) + último auto_run

Es GLOBAL (no por libro); acepta --paper/--live por consistencia, sin efecto.

    python tools/health.py
"""
import os
import sys
import argparse
from pathlib import Path

from dotenv import load_dotenv

_THIS_DIR  = Path(__file__).resolve().parent
_SCRIPTS   = _THIS_DIR.parent
_REPO_ROOT = _SCRIPTS.parent
sys.path.insert(0, str(_SCRIPTS))

_ENV_NAME  = os.getenv("ENV_FILE", ".env.v2")
_ENV_PATH  = Path(_ENV_NAME)
if not _ENV_PATH.is_absolute():
    _ENV_PATH = _REPO_ROOT / _ENV_PATH
load_dotenv(_ENV_PATH)


def _conn():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def _et_now():
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo("America/New_York"))


def _is_market_open():
    et = _et_now()
    if et.weekday() >= 5:
        return False
    mins = et.hour * 60 + et.minute
    return (9 * 60 + 30) <= mins < (16 * 60)


def _age_str(minutes):
    if minutes is None:
        return "nunca"
    m = int(minutes)
    if m < 60:
        return f"hace {m}min"
    if m < 60 * 48:
        return f"hace {m // 60}h {m % 60}min"
    return f"hace {m // (60*24)}d"


def _max_age(cur, table, col):
    """(timestamp, edad_en_minutos) del registro más reciente, o (None, None)."""
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    if cur.fetchone()[0] is None:
        return None, None
    cur.execute(f"SELECT MAX({col}), EXTRACT(EPOCH FROM (NOW() - MAX({col})))/60 FROM {table}")
    return cur.fetchone()


def render_health():
    lines = ["🩺 SALUD DEL SISTEMA (v2)", ""]

    # 1. DB
    try:
        conn = _conn(); cur = conn.cursor(); cur.execute("SELECT 1"); cur.fetchone()
    except Exception as e:
        return f"🩺 SALUD DEL SISTEMA (v2)\n\n❌ DB: no conecta ({e})"
    lines.append("✅ DB: conecta")

    # 2. Credenciales Tastytrade
    cs = os.getenv("TASTYTRADE_CLIENT_SECRET"); rt = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    lines.append(f"{'✅' if cs and rt else '❌'} Credenciales Tastytrade: "
                 f"{'presentes' if cs and rt else 'FALTAN'}")

    # 3. Mercado
    et = _et_now()
    lines.append(f"🕐 Mercado: {'ABIERTO' if _is_market_open() else 'cerrado'} "
                 f"(ET {et.strftime('%H:%M %a')})")

    # 4. Scan fresco
    scan_at, scan_age = _max_age(cur, "ticker_study", "scan_at")
    if scan_age is None:
        lines.append("⚠️  Scan: sin scans todavía")
    else:
        flag = "✅" if scan_age <= 8 * 60 else "⚠️ "   # un scan de hoy (<8h) es fresco
        lines.append(f"{flag} Scan: {_age_str(scan_age)} ({scan_at.strftime('%m-%d %H:%M')})")

    # 5. Latido del monitor (snapshot) + último auto_run
    snap_at, snap_age = _max_age(cur, "account_snapshots", "snapshot_at")
    if snap_age is None:
        lines.append("⚠️  Monitor: sin snapshots (¿el worker corrió?)")
    else:
        limite = 45 if _is_market_open() else 120   # late más seguido con mercado abierto
        flag = "✅" if snap_age <= limite else "❌"
        lines.append(f"{flag} Monitor (latido): {_age_str(snap_age)}")

    run_at, run_age = _max_age(cur, "auto_run_logs", "run_at")
    if run_age is None:
        lines.append("⚠️  Auto_run: sin runs todavía")
    else:
        lines.append(f"🔁 Último auto_run: {_age_str(run_age)} ({run_at.strftime('%m-%d %H:%M')})")

    cur.close(); conn.close()
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Salud del sistema (global)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_const", const="paper", dest="book")
    g.add_argument("--live",  action="store_const", const="live",  dest="book")
    p.set_defaults(book="paper")
    p.parse_args()

    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1
    print(render_health())
    return 0


if __name__ == "__main__":
    sys.exit(main())