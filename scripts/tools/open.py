"""
scripts/tools/open.py  (v2-bidi)
================================
Posiciones ABIERTAS de un libro. Reutilizable: render_open(book) devuelve el texto
(para el bot de Telegram) y el CLI lo imprime.

    python tools/open.py            # paper (default)
    python tools/open.py --live     # libro live (vacío honesto si aún no existe)

Vacío honesto: si la tabla del libro no existe todavía (p.ej. live en v2), NO falla
— informa que no hay datos.
"""
import os
import sys
import argparse
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

_THIS_DIR  = Path(__file__).resolve().parent            # scripts/tools
_SCRIPTS   = _THIS_DIR.parent                           # scripts
_REPO_ROOT = _SCRIPTS.parent                            # raíz del repo
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


def render_open(book="paper"):
    """Texto con las posiciones abiertas del libro. Vacío honesto si no hay tabla."""
    table = _BOOK_TABLE.get(book)
    if table is None:
        return f"libro desconocido: {book!r} (usar paper o live)"

    conn = _conn(); cur = conn.cursor()
    if not _table_exists(cur, table):
        cur.close(); conn.close()
        return f"📁 {book.upper()} — sin datos (el libro aún no existe)"

    cur.execute(f"""
        SELECT ticker, strategy, strike_low, strike_high, expiration,
               gross_pnl, pnl_pct
        FROM {table}
        WHERE UPPER(status) = 'OPEN'
        ORDER BY opened_at
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()

    if not rows:
        return f"📁 {book.upper()} — 0 posiciones abiertas"

    lines = [f"📁 {book.upper()} — {len(rows)} posiciones abiertas", ""]
    pnl_total = 0.0
    for tk, strat, sl, sh, exp, pnl, pct in rows:
        pnl = float(pnl or 0); pnl_total += pnl
        pct = float(pct or 0)
        strikes = f"${sl:.0f}" if sh is None else f"${sl:.0f}/{sh:.0f}"
        dte = (exp - date.today()).days if exp else "?"
        lines.append(f"  {tk:<6} {strat:<18} {strikes:<12} "
                     f"P&L {pnl:+.0f} ({pct:+.1f}%) · {dte}d")
    lines.append("")
    lines.append(f"  P&L abierto total: {pnl_total:+.0f}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Posiciones abiertas de un libro")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_const", const="paper", dest="book")
    g.add_argument("--live",  action="store_const", const="live",  dest="book")
    p.set_defaults(book="paper")
    a = p.parse_args()

    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1
    print(render_open(a.book))
    return 0


if __name__ == "__main__":
    sys.exit(main())