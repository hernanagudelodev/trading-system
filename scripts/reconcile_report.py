"""
reconcile_report.py
===================
READ-ONLY. Compares the REAL P&L from Tastytrade (with fees) against what the DB
recorded, for a period. Writes NOTHING to the DB or the broker.

WHY
    The DB (`positions`) said +$167 for the closed live trades; the broker's
    P/L YTD showed ~$120. The gap is unexplained. The DB stores gross_pnl from
    fills but does NOT store commissions/fees, and may be missing transactions
    the broker has (pre-system trades, or anything closed outside the system).
    This report reconciles the two so the REAL number is known.

    This is the first read-only piece of pillar §22.4 (DB-broker reconciliation)
    and resolves §22.12 (no read-only broker tool): the only thing that queried
    the broker before was run_sync, which WRITES. This only reads.

WHAT IT DOES (totals mode)
    1. Pulls all broker transactions in the period via Account.get_history
       (async in SDK 13.x — wrapped in asyncio.run here).
    2. Sums the broker's real cash movement (net_value) and breaks out every fee
       (commission, regulatory, clearing, proprietary, other).
    3. Sums the DB's gross_pnl for CLOSED positions in the same period.
    4. Shows the difference and how much of it is fees.

WHAT IT DOESN'T DO
    No per-position cross (that's mode b, if this leaves a gap). No writes.
    Cannot recover historical marks. Just totals.

USAGE
    python reconcile_report.py --since 2026-06-20
    python reconcile_report.py --since 2026-06-20 --until 2026-07-29
    python reconcile_report.py --since 2026-06-20 --account 5WI77328
"""
import argparse
import asyncio
import os
import sys
from datetime import date, datetime

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def _f(v):
    """Decimal/None -> float. Fees/values come as Decimal; None -> 0.0 (a missing
    fee really is zero, unlike a missing PRICE which we never zero-fill)."""
    return float(v) if v is not None else 0.0


async def fetch_transactions(start, end, account_number):
    from tastytrade import Session, Account

    cs = os.getenv("TASTYTRADE_CLIENT_SECRET")
    rt = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not cs or not rt:
        print("  ERROR: missing TASTYTRADE_CLIENT_SECRET / TASTYTRADE_REFRESH_TOKEN")
        return None

    session = Session(cs, rt)

    # Resolve the account. If a number was given, use it; else take the first.
    accounts = await Account.get(session)   # async in 13.x
    if account_number:
        acct = next((a for a in accounts if a.account_number == account_number), None)
        if acct is None:
            print(f"  ERROR: account {account_number} not found. "
                  f"Available: {[a.account_number for a in accounts]}")
            return None
    else:
        acct = accounts[0]

    print(f"  Account: {acct.account_number}")

    # get_history is async in 13.x and paginates. Pull the period.
    txns = await acct.get_history(session, start_date=start, end_date=end)
    return txns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="YYYY-MM-DD")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--account", default=None, help="account number (default: first)")
    a = ap.parse_args()

    start = date.fromisoformat(a.since)
    end = date.fromisoformat(a.until) if a.until else date.today()

    print(f"\n  Period: {start} .. {end}")

    # ── BROKER SIDE ───────────────────────────────────────────────────────────
    try:
        txns = asyncio.run(fetch_transactions(start, end, a.account))
    except Exception as e:
        print(f"  ERROR pulling broker history: {type(e).__name__}: {e}")
        return 1
    if txns is None:
        return 1

    print(f"  Broker transactions in period: {len(txns)}")

    # Sum the real cash movement and the fees.
    broker_net = 0.0
    fee_commission = fee_reg = fee_clearing = fee_prop = fee_other = 0.0
    by_type = {}

    for t in txns:
        broker_net += _f(getattr(t, "net_value", None))
        fee_commission += _f(getattr(t, "commission", None))
        fee_reg        += _f(getattr(t, "regulatory_fees", None))
        fee_clearing   += _f(getattr(t, "clearing_fees", None))
        fee_prop       += _f(getattr(t, "proprietary_index_option_fees", None))
        fee_other      += _f(getattr(t, "other_charge", None))
        tt = getattr(t, "transaction_type", "?")
        by_type[tt] = by_type.get(tt, 0) + 1

    fees_total = fee_commission + fee_reg + fee_clearing + fee_prop + fee_other

    print(f"\n  Transaction types:")
    for tt, n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"    {tt:<28} {n}")

    print(f"\n  ── BROKER (real cash, from get_history) ──")
    print(f"    net_value sum (incl. fees)   : ${broker_net:>10,.2f}")
    print(f"    fees breakdown:")
    print(f"      commission                 : ${fee_commission:>10,.2f}")
    print(f"      regulatory                 : ${fee_reg:>10,.2f}")
    print(f"      clearing                   : ${fee_clearing:>10,.2f}")
    print(f"      proprietary index          : ${fee_prop:>10,.2f}")
    print(f"      other                      : ${fee_other:>10,.2f}")
    print(f"      ── total fees              : ${fees_total:>10,.2f}")
    print(f"    net BEFORE fees (net+fees)   : ${broker_net - fees_total:>10,.2f}")

    # ── DB SIDE ───────────────────────────────────────────────────────────────
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(gross_pnl), 0), COUNT(*)
        FROM positions
        WHERE UPPER(status) = 'CLOSED'
          AND closed_at >= %s AND closed_at < (%s::date + 1)
    """, (start, end))
    db_gross, db_count = cur.fetchone()
    cur.close(); conn.close()
    db_gross = float(db_gross)

    print(f"\n  ── DB (positions.gross_pnl, CLOSED) ──")
    print(f"    closed positions in period   : {db_count}")
    print(f"    sum gross_pnl (NO fees)      : ${db_gross:>10,.2f}")

    # ── RECONCILE ─────────────────────────────────────────────────────────────
    print(f"\n  ── RECONCILIATION ──")
    print(f"    DB gross (no fees)           : ${db_gross:>10,.2f}")
    print(f"    Broker net (with fees)       : ${broker_net:>10,.2f}")
    diff = db_gross - broker_net
    print(f"    difference                   : ${diff:>10,.2f}")
    print(f"    total fees (explains part)   : ${fees_total:>10,.2f}")
    residual = diff - fees_total
    print(f"    residual after fees          : ${residual:>10,.2f}")
    print()
    if abs(residual) < 1.0:
        print("  → The gap is FULLY explained by fees. DB gross - fees = broker net.")
    else:
        print(f"  → ${abs(residual):,.2f} remains AFTER fees. Likely causes:")
        print(f"     - transactions the broker has but the DB doesn't (pre-system")
        print(f"       trades, or closed outside the system), or vice versa;")
        print(f"     - broker net_value includes movements beyond option opens/closes")
        print(f"       (dividends, assignments, transfers) in this window.")
        print(f"     Run a per-position cross (mode b) to find the specific rows.")
    print()

    # A hint: net_value includes ALL cash, not just option trades. Show the
    # transaction types so the user can see if non-trade movements are inflating it.
    print("  NOTE: broker net_value sums ALL cash movements in the window "
          "(see transaction types above), not only option opens/closes. If types "
          "other than Trade appear, they widen the gap legitimately.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())