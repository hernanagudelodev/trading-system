"""
scripts/tools/close.py  (v2-bidi)
=================================
Cierra UNA posición a mano. Dos pasos: sin --confirm muestra qué cerraría (dry-run);
con --confirm ejecuta. Cerrar es irreversible, por eso el paso extra.

    python tools/close.py ABT             # dry-run: muestra la posición y su P&L
    python tools/close.py ABT --confirm   # ejecuta el cierre (paper)
    python tools/close.py ABT --live      # libro live (no disponible aún)
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


def close_position(ticker, book="paper", confirm=False):
    """Cierra (o simula cerrar) la posición OPEN de `ticker`. Devuelve texto."""
    ticker = ticker.upper()
    table  = _BOOK_TABLE.get(book)
    if table is None:
        return f"libro desconocido: {book!r} (usar paper o live)"
    if book == "live":
        # DEUDA (cierre live): antes de mandar la orden de cierre al broker, hay que
        # RECONCILIAR contra Tastytrade — que las patas existan, que las cantidades
        # coincidan con la DB, que el spread esté como el sistema cree. En paper no
        # aplica (no hay patas reales; la DB es la única fuente), pero en live cerrar
        # a ciegas es peligroso. Traer esa validación cuando se construya LiveExecutor.
        return "⚠️  cierre live no disponible aún (v2 corre solo paper)"

    conn = _conn(); cur = conn.cursor()
    if not _table_exists(cur, table):
        cur.close(); conn.close()
        return f"{book.upper()} — sin datos (el libro aún no existe)"
    cur.execute(f"""
        SELECT strategy, strike_low, strike_high, gross_pnl, pnl_pct
        FROM {table} WHERE UPPER(status)='OPEN' AND UPPER(ticker)=%s
    """, (ticker,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return f"❌ {ticker} no tiene posición abierta en {book}"

    strat, sl, sh, pnl, pct = row
    strikes = f"${sl:.0f}" if sh is None else f"${sl:.0f}/{sh:.0f}"
    pnl = float(pnl or 0); pct = float(pct or 0)

    if not confirm:
        return (f"[DRY-RUN] cerraría en {book}:\n"
                f"  {ticker} {strat} {strikes} · P&L {pnl:+.0f} ({pct:+.1f}%)\n"
                f"Repetí con --confirm para ejecutar el cierre (irreversible).")

    # Ejecutar (cmd_paper_close busca precio real; cae al último valor si no hay)
    import trade
    ok = trade.cmd_paper_close(ticker, close_reason="MANUAL",
                               close_rationale="cierre manual via tool close.py")
    return f"✅ {ticker} cerrado (paper)." if ok else \
           f"❌ no se pudo cerrar {ticker} — sigue abierta (ver detalle arriba)."


def main():
    p = argparse.ArgumentParser(description="Cerrar una posición a mano (dos pasos)")
    p.add_argument("ticker", help="ticker de la posición a cerrar")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_const", const="paper", dest="book")
    g.add_argument("--live",  action="store_const", const="live",  dest="book")
    p.add_argument("--confirm", action="store_true", help="ejecuta el cierre (sin esto, dry-run)")
    p.set_defaults(book="paper")
    a = p.parse_args()

    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1
    print(close_position(a.ticker, a.book, a.confirm))
    return 0


if __name__ == "__main__":
    sys.exit(main())