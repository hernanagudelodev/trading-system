"""
scripts/tools/book.py  (v2-bidi)
================================
Estado direccional y de riesgo de un libro: delta neto, riesgo total y por sector
vs los topes, y balance alcista/bajista. Reutiliza portfolio.py.

    python tools/book.py            # paper (default)
    python tools/book.py --live     # libro live (vacío honesto si aún no existe)

NOTA: el delta neto se calcula con deltas FRESCOS de cada pata (fetch a Tastytrade),
así que esta tool tarda unos segundos — el riesgo y el balance salen de la DB al toque.
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


def _table_exists(table):
    conn = _conn(); cur = conn.cursor()
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    ok = cur.fetchone()[0] is not None
    cur.close(); conn.close()
    return ok


def render_book(book="paper"):
    import portfolio
    table = portfolio._BOOK_TABLE.get(book)
    if table is None:
        return f"libro desconocido: {book!r} (usar paper o live)"
    if not _table_exists(table):
        return f"📊 {book.upper()} — sin datos (el libro aún no existe)"

    # Riesgo (barato, de la DB) + topes
    from option_selector import get_account_nlv, portfolio_risk_pct, max_sector_risk_pct
    try:
        current_risk, sector_risk, open_tickers = portfolio.read_book_risk(book)
        capital  = get_account_nlv()
        max_port = capital * portfolio_risk_pct() / 100.0
        max_sect = capital * max_sector_risk_pct() / 100.0
    except Exception as e:
        return f"📊 {book.upper()} — error leyendo riesgo/topes: {e}"

    if not open_tickers:
        return f"📊 {book.upper()} — 0 posiciones abiertas"

    # Delta neto + balance (delta hace fetch: puede tardar)
    net, positions, incomplete = portfolio.build_book(book)
    bearish = {"Bear Call Spread", "Bear Put Spread", "Long Put"}
    n_bear  = sum(1 for p in positions if p["strategy"] in bearish)
    n_bull  = len(positions) - n_bear
    side    = "net long (gana si sube)"  if net > 0 else \
              "net short (gana si baja)" if net < 0 else "neutral"

    lines = [
        f"📊 {book.upper()} — {len(open_tickers)} posiciones",
        "",
        f"🧭 Delta neto: {net:+.0f} ({side})",
        f"⚖️  Balance: {n_bull} alcistas / {n_bear} bajistas",
        "",
        f"💰 Riesgo total: ${current_risk:,.0f} / ${max_port:,.0f} (NLV ${capital:,.0f})",
        f"🏦 Por sector (tope ${max_sect:,.0f}):",
    ]
    for sec, risk in sorted(sector_risk.items(), key=lambda x: -x[1]):
        flag = "  ⚠️" if risk > max_sect else ""
        lines.append(f"     {sec}: ${risk:,.0f}{flag}")
    if incomplete:
        lines.append(f"\n⚠️  {len(incomplete)} posición(es) con delta faltante (no sumadas)")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Estado direccional y de riesgo de un libro")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_const", const="paper", dest="book")
    g.add_argument("--live",  action="store_const", const="live",  dest="book")
    p.set_defaults(book="paper")
    a = p.parse_args()

    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1
    print(render_book(a.book))
    return 0


if __name__ == "__main__":
    sys.exit(main())