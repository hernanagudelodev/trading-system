"""
scripts/tools/runs.py  (v2-bidi)
================================
Historial de corridas del auto_run (tabla auto_run_logs): slot, régimen, candidatas,
cuántas abrió, delta neto y duración. Sirve para ver de un vistazo si los 4 slots
del día corrieron bien. Barato (solo lee la tabla).

    python tools/runs.py            # paper (default), últimos 10
    python tools/runs.py --n 20     # últimos 20
    python tools/runs.py --live     # runs del libro live (vacío honesto por ahora)
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


def _table_exists(cur, table):
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cur.fetchone()[0] is not None


def render_runs(book="paper", n=10):
    conn = _conn(); cur = conn.cursor()
    if not _table_exists(cur, "auto_run_logs"):
        cur.close(); conn.close()
        return "🕒 sin runs todavía (auto_run_logs no existe)"

    cur.execute("""
        SELECT run_at, slot, regime, candidates, opened, net_delta, run_time_sec
        FROM auto_run_logs
        WHERE mode = %s
        ORDER BY run_at DESC
        LIMIT %s
    """, (book, n))
    rows = cur.fetchall()
    cur.close(); conn.close()

    if not rows:
        return f"🕒 {book.upper()} — sin runs registrados"

    lines = [f"🕒 {book.upper()} — últimos {len(rows)} runs", ""]
    for at, slot, reg, cand, op, nd, rt in rows:
        ts   = at.strftime("%m-%d %H:%M") if at else "?"
        nd_s = f"{float(nd):+.0f}" if nd is not None else "?"
        rt_s = f"{rt}s" if rt is not None else "?"
        lines.append(f"  {ts} [{slot or '?'}] {reg or '?'} · "
                     f"{cand} cand · abrió {op} · Δ{nd_s} · {rt_s}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Historial de corridas del auto_run")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_const", const="paper", dest="book")
    g.add_argument("--live",  action="store_const", const="live",  dest="book")
    p.add_argument("--n", type=int, default=10, help="cuántos runs mostrar (default 10)")
    p.set_defaults(book="paper")
    a = p.parse_args()

    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1
    print(render_runs(a.book, a.n))
    return 0


if __name__ == "__main__":
    sys.exit(main())