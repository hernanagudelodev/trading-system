"""
audit_closed_pnl.py
===================
Read-only. Recomputes gross_pnl / pnl_pct for every CLOSED position from the
stored fills, and compares against what is recorded in the row.

WHY
    Two findings converged on 24-jul:
      1. pnl_pct is computed differently per strategy in spread_pnl:
           debit  (BCS): gross_pnl / total_cost   -- total_cost is POSITIVE
           credit (BPS): gross_pnl / max_profit    (the credit)
         For a BPS, total_cost is NEGATIVE, so any code path that divided a loss
         by total_cost produced a POSITIVE pnl_pct for a LOSS. The sign lies.
      2. gross_pnl itself: for a spread it is total_received - total_cost, all
         from stored fills. It does NOT depend on the live `mark`, so it can be
         recomputed exactly today from data already in the DB.

    This audit recomputes gross_pnl from the stored fills and a CONSISTENT
    pnl_pct (always over real max loss / max risk), and shows where the stored
    values disagree. It writes NOTHING.

WHAT IT CANNOT DO
    It cannot recover the historical `mark` at each close, so it cannot tell you
    whether a close was triggered by a noisy mid. That decision is gone. This
    only audits the ARITHMETIC of what was recorded, not the timing of the exit.

DEFINITIONS USED HERE (the consistent ones)
    debit spread (BCS):  max_risk = total_cost              (what you paid)
                         gross    = total_received - total_cost
    credit spread (BPS): max_risk = width*100 - credit       (real max loss)
                         gross    = credit_received - cost_to_close
                                  = total_received - total_cost   (total_cost<0)
    In BOTH cases gross_pnl = total_received - total_cost holds with the stored
    sign convention (total_cost>0 debit, <0 credit; total_received mirrors it),
    so gross is recomputed uniformly and pnl_pct is expressed over max_risk.

USAGE
    python audit_closed_pnl.py                 # both tables
    python audit_closed_pnl.py --table positions
    python audit_closed_pnl.py --since 2026-06-20
"""
import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def is_credit(premium_paid):
    # sign is the source of truth: <0 credit (BPS), >0 debit (BCS)
    return premium_paid is not None and float(premium_paid) < 0


def recompute(row):
    """
    Returns (gross, pnl_pct_over_maxrisk, max_risk) from stored fills, or Nones
    if the row lacks the data to close the arithmetic.
    """
    sl   = row["strike_low"]
    sh   = row["strike_high"]
    n    = int(row["contracts"] or 1)
    tc   = row["total_cost"]
    tr   = row["total_received"]
    prem = row["premium_paid"]

    if tc is None or tr is None:
        return None, None, None

    tc = float(tc); tr = float(tr)
    gross = round(tr - tc, 2)

    if sl is None or sh is None:
        return gross, None, None
    width = abs(float(sh) - float(sl))

    if is_credit(prem):
        credit   = abs(tc)                       # tc<0 for credit
        max_risk = round(width * 100 * n - credit, 2)
    else:
        max_risk = round(tc, 2)                  # debit: cost is the risk

    pnl_pct = round(gross / max_risk * 100, 2) if max_risk else None
    return gross, pnl_pct, max_risk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", choices=("positions", "paper_positions", "both"),
                    default="both")
    ap.add_argument("--since", default=None, help="closed_at >= YYYY-MM-DD")
    args = ap.parse_args()

    tables = ("positions", "paper_positions") if args.table == "both" else (args.table,)

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    grand_mismatch = 0
    grand_total    = 0
    exp_stored_sum = 0.0
    exp_recomp_sum = 0.0
    exp_n          = 0

    for table in tables:
        where = "UPPER(status) = 'CLOSED'"
        params = []
        if args.since:
            where += " AND closed_at >= %s"
            params.append(args.since)

        cur.execute(f"""
            SELECT id, ticker, strategy, strike_low, strike_high, contracts,
                   premium_paid, total_cost, total_received,
                   gross_pnl, pnl_pct, close_reason
            FROM {table}
            WHERE {where}
            ORDER BY id
        """, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        print(f"\n{'='*78}")
        print(f"  {table}  —  {len(rows)} closed")
        print(f"{'='*78}")
        print(f"  {'id':>4} {'ticker':<6} {'cr?':<3} "
              f"{'gross_db':>9} {'gross_re':>9} {'pnl%_db':>8} {'pnl%_re':>8}  flag")
        print(f"  {'-'*4} {'-'*6} {'-'*3} {'-'*9} {'-'*9} {'-'*8} {'-'*8}  ----")

        t_mismatch = 0
        for r in rows:
            grand_total += 1
            g_re, p_re, _ = recompute(r)
            g_db = None if r["gross_pnl"] is None else float(r["gross_pnl"])
            p_db = None if r["pnl_pct"]  is None else float(r["pnl_pct"])
            cr   = "BPS" if is_credit(r["premium_paid"]) else "BCS"

            flags = []
            if g_re is not None and g_db is not None and abs(g_re - g_db) > 0.5:
                flags.append("GROSS")
            # sign lie: recomputed loss but stored positive pnl_pct (or vice versa)
            if (g_re is not None and p_db is not None
                    and g_re < 0 and p_db > 0):
                flags.append("SIGN")
            if p_re is not None and p_db is not None and abs(p_re - p_db) > 1.0:
                flags.append("PCT")

            flag = ",".join(flags) if flags else "ok"
            if flags:
                t_mismatch += 1

            if g_db is not None and g_re is not None:
                exp_stored_sum += g_db
                exp_recomp_sum += g_re
                exp_n += 1

            def fmt(x, w=9):
                return f"{x:{w}.2f}" if x is not None else " " * (w - 2) + "--"

            print(f"  {r['id']:>4} {r['ticker']:<6} {cr:<3} "
                  f"{fmt(g_db)} {fmt(g_re)} {fmt(p_db,8)} {fmt(p_re,8)}  {flag}")

        grand_mismatch += t_mismatch
        print(f"\n  {table}: {t_mismatch} row(s) with mismatches out of {len(rows)}")

    cur.close(); conn.close()

    print(f"\n{'='*78}")
    print(f"  TOTAL: {grand_mismatch} mismatched / {grand_total} closed")
    if exp_n:
        print(f"\n  Expectancy check over {exp_n} rows with both values:")
        print(f"    sum gross (as stored)     : ${exp_stored_sum:,.2f}"
              f"   -> ${exp_stored_sum/exp_n:+.2f}/trade")
        print(f"    sum gross (recomputed)    : ${exp_recomp_sum:,.2f}"
              f"   -> ${exp_recomp_sum/exp_n:+.2f}/trade")
        d = exp_recomp_sum - exp_stored_sum
        print(f"    difference                : ${d:,.2f}")
        print(f"\n  NOTE: gross_pnl rarely changes (it's from stored fills). The")
        print(f"  real damage of the sign bug is in pnl_pct, which drives STOPS")
        print(f"  and the win/loss classification -- not in the dollar total.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())