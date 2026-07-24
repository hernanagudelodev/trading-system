"""
migrate_paper_symbols.py
========================
Aligns `paper_positions` with `positions` by adding the OCC option symbols and
backfilling them for currently open rows.

WHY
    The monitor is migrating to REST pricing (get_market_data_by_type), which
    needs OCC symbols. `positions` already stores tastytrade_symbol /
    tastytrade_symbol_short; `paper_positions` does not. Without this, the paper
    monitor cannot price by the same channel as live and the two libraries would
    diverge again -- the exact pattern this project keeps paying for.

WHAT IT DOES
    1. ADD COLUMN tastytrade_symbol, tastytrade_symbol_short (VARCHAR(50), NULL).
    2. Backfill both columns for every OPEN paper row, building the OCC symbol
       from ticker + expiration + option type + strike.

OCC FORMAT (verified against real DB symbols: DLTR/PAYX/JNJ)
    'ROOT  YYMMDD C|P SSSSSSSS'
    - ROOT   : ticker left-justified to 6 chars with spaces  ('JNJ   ')
    - YYMMDD : expiration
    - C|P    : call / put  (derived from strategy: Bull Put Spread -> P)
    - strike : strike * 1000, zero-padded to 8 digits

    Long leg  = strike_low   (both spreads: the lower strike is the long)
    Short leg = strike_high
    Verified: this matches how `positions` stored them (DLTR long=126=strike_low).

SAFETY
    ADD COLUMN IF NOT EXISTS, NULLABLE. Backfill only touches rows where the
    symbol is still NULL. Idempotent: a second run is a no-op.

USAGE
    python migrate_paper_symbols.py            # dry-run: shows what it would write
    python migrate_paper_symbols.py --commit   # apply
"""
import datetime
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

TABLE   = "paper_positions"
COLUMNS = (
    ("tastytrade_symbol",       "VARCHAR(50)"),
    ("tastytrade_symbol_short", "VARCHAR(50)"),
)


def build_occ_symbol(ticker, expiration, option_type, strike):
    """
    Build an OCC option symbol. Single source of truth for the format.

    option_type: 'call'/'put' (or anything starting with c/p).
    Verified against DLTR/PAYX/JNJ symbols already stored in `positions`.
    """
    d = datetime.date.fromisoformat(str(expiration))
    yymmdd = d.strftime("%y%m%d")
    cp = "C" if str(option_type).lower().startswith("c") else "P"
    strike_int = int(round(float(strike) * 1000))
    return f"{ticker:<6}{yymmdd}{cp}{strike_int:08d}"


def col_type(cur, table, column):
    cur.execute("""
        SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
    """, (table, column))
    r = cur.fetchone()
    return r[0] if r else None


def main():
    commit = "--commit" in sys.argv
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL missing")
        return 1

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    # ── 1) columns ────────────────────────────────────────────────────────────
    print()
    print(f"  {'column':<26} {'state'}")
    print(f"  {'-'*26} {'-'*12}")
    missing_cols = []
    for col, _ in COLUMNS:
        t = col_type(cur, TABLE, col)
        print(f"  {col:<26} {t or 'MISSING'}")
        if t is None:
            missing_cols.append(col)
    print()

    if commit and missing_cols:
        types = dict(COLUMNS)
        for col in missing_cols:
            cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {col} {types[col]}")
            print(f"  ALTER {TABLE}.{col}")
        conn.commit()
        print()
    elif missing_cols:
        print(f"  DRY RUN: would add {len(missing_cols)} column(s).")
        print()

    # If columns still don't exist (dry-run), we can't read them for backfill,
    # but we can still SHOW what would be written from the base data.
    have_cols = not missing_cols or commit

    # ── 2) backfill open rows ─────────────────────────────────────────────────
    cur.execute(f"""
        SELECT id, ticker, strategy, strike_low, strike_high, expiration
        FROM {TABLE}
        WHERE UPPER(status) = 'OPEN'
        ORDER BY id
    """)
    rows = cur.fetchall()
    print(f"  Open paper rows: {len(rows)}")
    print(f"  {'id':>4}  {'ticker':<6} {'type':<4} {'long (low)':<24} {'short (high)':<24}")
    print(f"  {'-'*4}  {'-'*6} {'-'*4} {'-'*24} {'-'*24}")

    updates = []
    for pos_id, ticker, strategy, sl, sh, exp in rows:
        opt_type = "put" if strategy == "Bull Put Spread" else "call"
        long_sym  = build_occ_symbol(ticker, exp, opt_type, sl)   # low  = long
        short_sym = build_occ_symbol(ticker, exp, opt_type, sh)   # high = short
        print(f"  {pos_id:>4}  {ticker:<6} {opt_type:<4} {long_sym:<24} {short_sym:<24}")
        updates.append((long_sym, short_sym, pos_id))

    print()
    if not commit:
        print("  DRY RUN -- nothing written. Re-run with --commit to apply.\n")
        cur.close(); conn.close()
        return 0

    if have_cols:
        written = 0
        for long_sym, short_sym, pos_id in updates:
            cur.execute(f"""
                UPDATE {TABLE}
                SET tastytrade_symbol = %s, tastytrade_symbol_short = %s
                WHERE id = %s
                  AND tastytrade_symbol IS NULL
            """, (long_sym, short_sym, pos_id))
            written += cur.rowcount
        conn.commit()
        print(f"  Backfilled {written} row(s) (only those still NULL).")

    # verify
    cur.execute(f"""
        SELECT COUNT(*) FROM {TABLE}
        WHERE UPPER(status)='OPEN' AND tastytrade_symbol IS NULL
    """)
    still_null = cur.fetchone()[0]
    print(f"  Open rows still without symbol: {still_null}")

    cur.close(); conn.close()
    print()
    return 0 if still_null == 0 else 1


if __name__ == "__main__":
    sys.exit(main())