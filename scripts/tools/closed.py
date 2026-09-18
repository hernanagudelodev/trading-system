"""
scripts/tools/closed.py  (v2-bidi)
==================================
Posiciones CERRADAS de un libro + rendimiento realizado: cada trade con su P&L y
motivo de cierre, más el resumen (cerrados, win rate, P&L acumulado). En paper, esto
es lo que muestra el rendimiento real de los trades simulados. Barato (lee la tabla).

    python tools/closed.py            # paper, cerrados últimos 7 días
    python tools/closed.py --days 30  # ventana de 30 días
    python tools/closed.py --all      # todos los cerrados
    python tools/closed.py --live     # libro live (vacío honesto por ahora)
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

_BOOK_TABLE = {"paper": "paper_positions", "live": "positions"}


def _conn():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def _table_exists(cur, table):
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cur.fetchone()[0] is not None


def render_closed(book="paper", days=7, all_time=False):
    table = _BOOK_TABLE.get(book)
    if table is None:
        return f"libro desconocido: {book!r} (usar paper o live)"

    conn = _conn(); cur = conn.cursor()
    if not _table_exists(cur, table):
        cur.close(); conn.close()
        return f"📕 {book.upper()} — sin datos (el libro aún no existe)"

    where = "UPPER(status) = 'CLOSED'"
    params = []
    if not all_time:
        where += " AND closed_at >= NOW() - INTERVAL '%s days'" % int(days)
    cur.execute(f"""
        SELECT ticker, strategy, gross_pnl, pnl_pct, close_reason, closed_at
        FROM {table}
        WHERE {where}
        ORDER BY closed_at DESC
    """, params)
    rows = cur.fetchall()
    cur.close(); conn.close()

    ventana = "histórico" if all_time else f"últimos {days}d"
    if not rows:
        return f"📕 {book.upper()} — 0 cerrados ({ventana})"

    total = sum(float(g or 0) for _, _, g, _, _, _ in rows)
    wins  = sum(1 for _, _, g, _, _, _ in rows if float(g or 0) > 0)
    losses = len(rows) - wins
    wr    = round(wins / len(rows) * 100, 1) if rows else 0

    lines = [
        f"📕 {book.upper()} — {len(rows)} cerrados ({ventana})",
        f"   P&L realizado: {total:+.0f} · win rate {wr}% ({wins}W/{losses}L)",
        "",
    ]
    for tk, strat, pnl, pct, reason, at in rows:
        pnl = float(pnl or 0); pct = float(pct or 0)
        ts  = at.strftime("%m-%d") if at else "?"
        sign = "✅" if pnl >= 0 else "❌"
        lines.append(f"  {sign} {tk:<6} {strat:<18} {pnl:+.0f} ({pct:+.1f}%) · "
                     f"{reason or '?'} · {ts}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Posiciones cerradas + rendimiento realizado")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_const", const="paper", dest="book")
    g.add_argument("--live",  action="store_const", const="live",  dest="book")
    p.add_argument("--days", type=int, default=7, help="ventana en días (default 7)")
    p.add_argument("--all", action="store_true", help="todos los cerrados (ignora --days)")
    p.set_defaults(book="paper")
    a = p.parse_args()

    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1
    print(render_closed(a.book, a.days, a.all))
    return 0


if __name__ == "__main__":
    sys.exit(main())