"""
reconcile_positions.py
======================
READ-ONLY. Per-position reconciliation: the broker's REAL net (with fees) for
each closed underlying, next to the DB's gross_pnl. Writes NOTHING.

WHY THE TOTALS VERSION FAILED
    Summing ALL broker net_value in a window mixes opens and closes of positions
    that are still open (FITB, PNC), plus Money Movements (deposits/withdrawals).
    That is not comparable to the DB's gross_pnl of CLOSED positions. This
    version fixes that: it only looks at tickers the DB marks CLOSED, groups the
    broker's Trade transactions by underlying, and pairs them per ticker.

WHAT IT DOES
    1. Reads the CLOSED positions from the DB (ticker, gross_pnl, dates).
    2. Pulls broker Trade transactions in the period (excludes Money Movement).
    3. Groups broker net_value + fees by underlying_symbol.
    4. For each closed ticker: DB gross vs broker net (with fees), side by side.
    5. Bottom line over the tickers that closed in the period.

CAVEATS (read them before trusting the per-row match)
    - A ticker traded more than once in the period (e.g. PANW, HOOD, CCL) will
      have its broker transactions SUMMED across all its trades. If the DB has
      the same number of closes for it, the totals still line up; if not, that
      row is flagged. Per-row precision would need order_id pairing (a later
      refinement) — this is per-ticker, which is enough to find the big gaps.
    - Broker net for a ticker only nets out if BOTH its open and close are in the
      window. A position opened before --since but closed inside will show only
      its close on the broker side → flagged as "partial window".

USAGE
    python reconcile_positions.py --since 2026-06-20
    python reconcile_positions.py --since 2026-06-20 --until 2026-07-30 --account 5WI77328
"""
import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import date

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def _f(v):
    return float(v) if v is not None else 0.0


async def fetch_trades(start, end, account_number):
    from tastytrade import Session, Account
    cs = os.getenv("TASTYTRADE_CLIENT_SECRET")
    rt = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not cs or not rt:
        print("  ERROR: missing Tastytrade credentials")
        return None
    session = Session(cs, rt)
    accounts = await Account.get(session)
    if account_number:
        acct = next((a for a in accounts if a.account_number == account_number), None)
        if acct is None:
            print(f"  ERROR: account {account_number} not found "
                  f"({[a.account_number for a in accounts]})")
            return None
    else:
        acct = accounts[0]
    print(f"  Account: {acct.account_number}")
    txns = await acct.get_history(session, start_date=start, end_date=end)
    return txns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default=None)
    ap.add_argument("--account", default=None)
    a = ap.parse_args()

    start = date.fromisoformat(a.since)
    end = date.fromisoformat(a.until) if a.until else date.today()
    print(f"\n  Period: {start} .. {end}")

    # ── DB: closed positions ──────────────────────────────────────────────────
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT ticker, gross_pnl, opened_at, closed_at
        FROM positions
        WHERE UPPER(status) = 'CLOSED'
          AND closed_at >= %s AND closed_at < (%s::date + 1)
        ORDER BY ticker, closed_at
    """, (start, end))
    db_rows = cur.fetchall()
    cur.close(); conn.close()

    # aggregate DB by ticker (a ticker may close more than once)
    db_by_ticker = defaultdict(lambda: {"gross": 0.0, "n": 0, "opens": []})
    for ticker, gross, opened_at, closed_at in db_rows:
        db_by_ticker[ticker]["gross"] += float(gross or 0)
        db_by_ticker[ticker]["n"] += 1
        db_by_ticker[ticker]["opens"].append(opened_at)

    # ── BROKER: trades grouped by underlying ─────────────────────────────────
    try:
        txns = asyncio.run(fetch_trades(start, end, a.account))
    except Exception as e:
        print(f"  ERROR pulling broker history: {type(e).__name__}: {e}")
        return 1
    if txns is None:
        return 1

    broker = defaultdict(lambda: {"net": 0.0, "fees": 0.0, "legs": 0})
    money_moves = 0
    for t in txns:
        ttype = getattr(t, "transaction_type", "")
        if ttype == "Money Movement":
            money_moves += 1
            continue
        if ttype != "Trade":
            continue
        u = getattr(t, "underlying_symbol", None) or getattr(t, "symbol", "?")
        fees = (_f(getattr(t, "commission", None))
                + _f(getattr(t, "regulatory_fees", None))
                + _f(getattr(t, "clearing_fees", None))
                + _f(getattr(t, "proprietary_index_option_fees", None))
                + _f(getattr(t, "other_charge", None)))
        broker[u]["net"] += _f(getattr(t, "net_value", None))
        broker[u]["fees"] += fees
        broker[u]["legs"] += 1

    print(f"  Broker: {sum(v['legs'] for v in broker.values())} trade legs "
          f"across {len(broker)} underlyings ({money_moves} money-movements skipped)\n")

    # ── PER-TICKER COMPARISON (only tickers the DB closed) ───────────────────
    print(f"  {'ticker':<7} {'db_gross':>10} {'broker_net':>11} {'fees':>8} "
          f"{'diff':>9}  flag")
    print(f"  {'-'*7} {'-'*10} {'-'*11} {'-'*8} {'-'*9}  ----")

    sum_db = sum_broker = sum_fees = 0.0
    for ticker in sorted(db_by_ticker):
        d = db_by_ticker[ticker]
        b = broker.get(ticker)
        db_gross = d["gross"]
        sum_db += db_gross

        if b is None:
            print(f"  {ticker:<7} {db_gross:>10.2f} {'--':>11} {'--':>8} "
                  f"{'--':>9}  NO BROKER DATA")
            continue

        broker_net = b["net"]
        fees = b["fees"]
        sum_broker += broker_net
        sum_fees += fees

        # broker_net already includes fees; broker gross = net - fees
        diff = db_gross - (broker_net - fees)   # compare gross to gross

        flags = []
        # odd number of legs suggests an open or close falls outside the window
        if b["legs"] % 2 != 0:
            flags.append("PARTIAL?")
        # opened before the window → broker only has the close
        if any(o is not None and o.date() < start for o in d["opens"]):
            flags.append("OPENED<WIN")
        if abs(diff) > 1.0 and not flags:
            flags.append("MISMATCH")
        flag = ",".join(flags) if flags else "ok"

        print(f"  {ticker:<7} {db_gross:>10.2f} {broker_net:>11.2f} {fees:>8.2f} "
              f"{diff:>9.2f}  {flag}")

    # ── BOTTOM LINE ──────────────────────────────────────────────────────────
    print(f"\n  {'='*54}")
    print(f"  BOTTOM LINE (tickers closed in period)")
    print(f"  {'='*54}")
    print(f"    DB gross_pnl (no fees)        : ${sum_db:>10,.2f}")
    print(f"    Broker gross (net - fees)     : ${sum_broker - sum_fees:>10,.2f}")
    print(f"    Broker net (what hit cash)    : ${sum_broker:>10,.2f}")
    print(f"    Total fees paid               : ${sum_fees:>10,.2f}")
    print()
    print(f"    → Tu ganancia REAL neta (con fees) del período, en las posiciones")
    print(f"      cerradas, es el 'Broker net': ${sum_broker:>,.2f}")
    print(f"      La DB mostraba ${sum_db:,.2f} porque no descuenta los ${abs(sum_fees):,.2f} de fees")
    print(f"      ni refleja diferencias de emparejamiento (ver flags arriba).")
    print()
    print(f"  NOTA: 'broker_net' de un ticker solo cuadra si su apertura Y cierre")
    print(f"  caen en la ventana. Los flags OPENED<WIN / PARTIAL marcan las filas")
    print(f"  donde falta una pata en el período — ahí el número del broker no es")
    print(f"  el P&L completo de esa posición, solo el flujo dentro de la ventana.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())