"""
audit_pnlpct.py — read-only. Trusts gross_pnl (stored, correct). Recomputes ONLY
pnl_pct over real max risk, with the correct sign, and flags rows where the
stored pnl_pct sign disagrees with the gross_pnl sign (the credit-spread bug).
"""
import argparse, os, sys, psycopg2
from dotenv import load_dotenv
load_dotenv()

def is_credit(p): return p is not None and float(p) < 0

def max_risk(row):
    sl,sh = row["strike_low"], row["strike_high"]
    n = int(row["contracts"] or 1)
    tc = row["total_cost"]
    if sl is None or sh is None or tc is None: return None
    width = abs(float(sh)-float(sl))
    if is_credit(row["premium_paid"]):
        return round(width*100*n - abs(float(tc)), 2)     # BPS: real max loss
    return round(abs(float(tc)), 2)                        # BCS: cost paid

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--table",choices=("positions","paper_positions","both"),default="both")
    ap.add_argument("--since",default=None)
    a=ap.parse_args()
    tables=("positions","paper_positions") if a.table=="both" else (a.table,)
    conn=psycopg2.connect(os.getenv("DATABASE_URL")); cur=conn.cursor()

    total=sign_bug=0
    wl_flips=0
    for table in tables:
        w="UPPER(status)='CLOSED'"; p=[]
        if a.since: w+=" AND closed_at>=%s"; p.append(a.since)
        cur.execute(f"""SELECT id,ticker,strategy,strike_low,strike_high,contracts,
                     premium_paid,total_cost,gross_pnl,pnl_pct,close_reason
                     FROM {table} WHERE {w} ORDER BY id""",p)
        cols=[d[0] for d in cur.description]
        rows=[dict(zip(cols,r)) for r in cur.fetchall()]
        print(f"\n{'='*70}\n  {table} — {len(rows)} closed\n{'='*70}")
        print(f"  {'id':>4} {'ticker':<6} {'cr?':<3} {'gross':>8} {'pnl%_db':>8} {'pnl%_fix':>8}  flag")
        print(f"  {'-'*4} {'-'*6} {'-'*3} {'-'*8} {'-'*8} {'-'*8}  ----")
        tbug=0
        for r in rows:
            total+=1
            g = None if r["gross_pnl"] is None else float(r["gross_pnl"])
            pdb = None if r["pnl_pct"] is None else float(r["pnl_pct"])
            mr = max_risk(r)
            pfix = round(g/mr*100,2) if (g is not None and mr) else None
            cr = "BPS" if is_credit(r["premium_paid"]) else "BCS"
            flag=[]
            # sign bug: stored pnl_pct sign disagrees with gross sign
            if g is not None and pdb is not None and g!=0:
                if (g<0)!=(pdb<0):
                    flag.append("SIGN"); tbug+=1; sign_bug+=1
            fl=",".join(flag) if flag else "ok"
            def f(x,w=8): return f"{x:{w}.2f}" if x is not None else " "*(w-2)+"--"
            print(f"  {r['id']:>4} {r['ticker']:<6} {cr:<3} {f(g)} {f(pdb)} {f(pfix)}  {fl}")
        print(f"\n  {table}: {tbug} sign-bug row(s)")
    cur.close();conn.close()
    print(f"\n{'='*70}\n  TOTAL sign-bug: {sign_bug} / {total} closed")
    print(f"\n  gross_pnl is TRUSTED (stored from fills). Only pnl_pct sign was wrong,")
    print(f"  and only on credit spreads. This does NOT change the dollar P&L or")
    print(f"  the expectancy total -- it affected stop calibration (fixed in D) and")
    print(f"  any win/loss classification that keyed on pnl_pct sign.\n")
    return 0
main()