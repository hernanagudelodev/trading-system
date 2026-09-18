"""
scripts/tools/equity.py  (v2-bidi)
==================================
NLV (net liquidating value) actual y su evolución, de account_snapshots. Muestra el
capital ahora, el cambio vs el primer snapshot y vs el de hace 24h. Barato (lee la tabla).

account_snapshots es de la CUENTA (una sola), no por libro: en v2 el paper no tiene
cuenta propia, comparte el NLV real de Tastytrade. El flag se acepta por consistencia
con las demás tools, pero la fuente es la misma.

    python tools/equity.py
    python tools/equity.py --n 10     # muestra los últimos 10 snapshots
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


def render_equity(n=8):
    conn = _conn(); cur = conn.cursor()
    if not _table_exists(cur, "account_snapshots"):
        cur.close(); conn.close()
        return "💵 sin snapshots todavía (account_snapshots no existe)"

    # NLV actual (último) + primero + el más cercano a hace 24h
    cur.execute("SELECT snapshot_at, net_liquidating_value FROM account_snapshots ORDER BY snapshot_at DESC LIMIT 1")
    last = cur.fetchone()
    if not last:
        cur.close(); conn.close()
        return "💵 sin snapshots registrados"
    now_at, now_nlv = last[0], float(last[1] or 0)

    cur.execute("SELECT net_liquidating_value FROM account_snapshots ORDER BY snapshot_at ASC LIMIT 1")
    first_nlv = float(cur.fetchone()[0] or 0)

    cur.execute("""
        SELECT net_liquidating_value FROM account_snapshots
        WHERE snapshot_at <= NOW() - INTERVAL '24 hours'
        ORDER BY snapshot_at DESC LIMIT 1
    """)
    row = cur.fetchone()
    nlv_24h = float(row[0]) if row else None

    cur.execute("""
        SELECT snapshot_at, net_liquidating_value
        FROM account_snapshots ORDER BY snapshot_at DESC LIMIT %s
    """, (n,))
    recent = cur.fetchall()
    cur.close(); conn.close()

    def _chg(base):
        if not base:
            return ""
        d = now_nlv - base
        pct = d / base * 100 if base else 0
        return f"{d:+,.0f} ({pct:+.2f}%)"

    lines = [
        f"💵 NLV: ${now_nlv:,.2f}",
        f"   desde inicio: {_chg(first_nlv)}",
    ]
    if nlv_24h is not None:
        lines.append(f"   últimas 24h:  {_chg(nlv_24h)}")
    lines.append("")
    lines.append(f"últimos {len(recent)} snapshots:")
    for at, nlv in recent:
        ts = at.strftime("%m-%d %H:%M") if at else "?"
        lines.append(f"  {ts}  ${float(nlv or 0):,.2f}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="NLV actual y su evolución")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_const", const="paper", dest="book")
    g.add_argument("--live",  action="store_const", const="live",  dest="book")
    p.add_argument("--n", type=int, default=8, help="cuántos snapshots mostrar (default 8)")
    p.set_defaults(book="paper")
    a = p.parse_args()

    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1
    print(render_equity(a.n))
    return 0


if __name__ == "__main__":
    sys.exit(main())