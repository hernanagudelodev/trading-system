"""
verify_paper_symbols.py
=======================
Read-only. Builds the OCC symbols the migration would write for each OPEN paper
row and asks Tastytrade whether each one actually returns market data.

WHY
    A wrong expiration or width in the DB row produces a syntactically valid but
    non-existent OCC symbol. The migration itself can't tell -- it just builds a
    string. This asks the broker: does this contract exist and price?

    Sends nothing, writes nothing. Pure lookup.

READS
    A symbol that returns bid/ask/mark EXISTS. One that returns nothing is
    either wrong (bad expiration/strike in the row) or so illiquid the broker
    has no data -- both worth seeing before the monitor tries to price it.

USAGE
    python verify_paper_symbols.py
"""
import asyncio
import datetime
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def build_occ_symbol(ticker, expiration, option_type, strike):
    d = datetime.date.fromisoformat(str(expiration))
    yymmdd = d.strftime("%y%m%d")
    cp = "C" if str(option_type).lower().startswith("c") else "P"
    strike_int = int(round(float(strike) * 1000))
    return f"{ticker:<6}{yymmdd}{cp}{strike_int:08d}"


async def fetch(symbols):
    from tastytrade import Session
    from tastytrade.market_data import get_market_data_by_type
    cs = os.getenv("TASTYTRADE_CLIENT_SECRET")
    rt = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not cs or not rt:
        print("ERROR: missing Tastytrade credentials")
        return None
    session = Session(cs, rt)
    data = await get_market_data_by_type(session, options=symbols)
    return {d.symbol: d for d in data}


def main():
    import psycopg2
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT id, ticker, strategy, strike_low, strike_high, expiration
        FROM paper_positions
        WHERE UPPER(status) = 'OPEN'
        ORDER BY id
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()

    plan = []
    symbols = []
    for pos_id, ticker, strategy, sl, sh, exp in rows:
        ot = "put" if strategy == "Bull Put Spread" else "call"
        lo = build_occ_symbol(ticker, exp, ot, sl)
        hi = build_occ_symbol(ticker, exp, ot, sh)
        plan.append((pos_id, ticker, exp, lo, hi))
        symbols += [lo, hi]

    print(f"\n  Querying Tastytrade for {len(symbols)} symbols "
          f"({len(rows)} paper positions)...\n")
    try:
        md = asyncio.run(fetch(symbols))
    except Exception as e:
        print(f"  ERROR: {e}")
        return 1
    if md is None:
        return 1

    print(f"  {'id':>4}  {'ticker':<6} {'exp':<10} {'leg':<24} {'exists':<7} {'bid/ask/mark'}")
    print(f"  {'-'*4}  {'-'*6} {'-'*10} {'-'*24} {'-'*7} {'-'*22}")
    problems = []
    for pos_id, ticker, exp, lo, hi in plan:
        for tag, sym in (("long", lo), ("short", hi)):
            d = md.get(sym)
            if d is None:
                mark = "--"
                exists = "NO"
                problems.append((pos_id, ticker, sym))
            else:
                b = getattr(d, "bid", None)
                a = getattr(d, "ask", None)
                m = getattr(d, "mark", None)
                mark = f"{b}/{a}/{m}"
                exists = "yes"
            print(f"  {pos_id:>4}  {ticker:<6} {str(exp):<10} {sym:<24} {exists:<7} {mark}")

    print()
    if problems:
        print(f"  ⚠️  {len(problems)} symbol(s) returned NO data:")
        for pos_id, ticker, sym in problems:
            print(f"       id={pos_id} {ticker}: '{sym}'")
        print(f"  -> check the row's expiration / strikes before migrating.")
    else:
        print(f"  ✓ All symbols exist at the broker. Safe to migrate.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())