"""
trade.py
========
Position and account tracker — syncs with Tastytrade API.

Does NOT execute orders. You open/close manually in Tastytrade.
This script reads what you have open and keeps DB in sync.

Commands:
    python trade.py --sync          # sync positions + save account snapshot
    python trade.py --list          # show open positions from DB
    python trade.py --account       # show account balance history
    python trade.py --history       # show closed positions P&L history

Sync logic:
    1. Fetch open positions from Tastytrade API
    2. Group legs into spreads when applicable (Bull Call Spread, Bear Put Spread)
    3. Compare with DB positions (status='OPEN')
    4. New positions in Tastytrade → insert in DB as ONE record per spread
    5. Positions in DB but not in Tastytrade → mark as closed, calculate P&L
    6. Save account snapshot (balances) to DB

DB tables used:
    positions         — open/closed option positions
    account_snapshots — daily balance history
"""

import os
import sys
import asyncio
from decimal import Decimal
from datetime import datetime, date
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# TASTYTRADE — fetch positions and balances
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_tastytrade_data():
    """
    Fetch current positions and balances from Tastytrade API.

    Returns dict with:
        account_number  (str)
        balances        (dict)
        positions       (list of dicts)
    """
    from tastytrade import Session
    from tastytrade.account import Account

    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")

    session  = Session(client_secret, refresh_token)
    accounts = await Account.get(session)
    account  = accounts[0]

    # ── Balances ──────────────────────────────────────────────────────────────
    bal = await account.get_balances(session)

    balances = {
        "account_number":          account.account_number,
        "net_liquidating_value":   float(bal.net_liquidating_value or 0),
        "equity_buying_power":     float(bal.equity_buying_power or 0),
        "derivative_buying_power": float(bal.derivative_buying_power or 0),
        "cash_balance":            float(bal.cash_balance or 0),
        "pending_cash":            float(bal.pending_cash or 0),
        "long_derivative_value":   float(bal.long_derivative_value or 0),
        "maintenance_excess":      float(bal.maintenance_excess or 0),
    }

    # ── Positions ─────────────────────────────────────────────────────────────
    raw_positions = await account.get_positions(session)
    positions = []

    for p in raw_positions:
        if p.instrument_type not in ("Equity Option",):
            continue

        parsed     = _parse_option_symbol(p.symbol)
        avg_open   = float(p.average_open_price or 0)
        quantity   = abs(int(p.quantity))
        multiplier = float(p.multiplier or 100)
        cost_basis = round(avg_open * quantity * multiplier, 2)
        close_px   = float(p.close_price or 0)
        mark       = float(p.mark or close_px)

        positions.append({
            "symbol":             p.symbol,
            "ticker":             parsed["ticker"],
            "expiration":         parsed["expiration"],
            "strike":             parsed["strike"],
            "option_type":        parsed["option_type"],
            "quantity":           int(p.quantity),
            "quantity_direction": str(p.quantity_direction),  # 'Long' or 'Short'
            "avg_open_price":     avg_open,
            "mark":               mark,
            "close_price":        close_px,
            "cost_basis":         cost_basis,
            "market_value":       round(mark * quantity * multiplier, 2),
            "unrealized_pnl":     0.0,
        })

    return {
        "account_number": account.account_number,
        "balances":        balances,
        "positions":       positions,
    }


def _parse_option_symbol(symbol):
    """
    Parse OCC option symbol format.
    Handles: .SLB260618C58  AND  XYZ   260702C00079000
    """
    import re

    clean = symbol.lstrip(".").replace(" ", "")

    # OCC format: TICKER YYMMDD C/P STRIKE(8 digits, strike * 1000)
    pattern = r'^([A-Z]+)(\d{6})([CP])(\d{8})$'
    m = re.match(pattern, clean)
    if m:
        ticker   = m.group(1)
        date_str = m.group(2)
        opt_type = "CALL" if m.group(3) == "C" else "PUT"
        strike   = int(m.group(4)) / 1000.0
        try:
            exp_date = datetime.strptime(date_str, "%y%m%d").date()
        except Exception:
            exp_date = None
        return {"ticker": ticker, "expiration": exp_date,
                "option_type": opt_type, "strike": strike}

    # Fallback: short format .SLB260618C58
    pattern2 = r'^([A-Z]+)(\d{6})([CP])(\d+(?:\.\d+)?)$'
    m2 = re.match(pattern2, clean)
    if m2:
        ticker   = m2.group(1)
        date_str = m2.group(2)
        opt_type = "CALL" if m2.group(3) == "C" else "PUT"
        strike   = float(m2.group(4))
        try:
            exp_date = datetime.strptime(date_str, "%y%m%d").date()
        except Exception:
            exp_date = None
        return {"ticker": ticker, "expiration": exp_date,
                "option_type": opt_type, "strike": strike}

    return {"ticker": clean[:20], "expiration": None,
            "option_type": "CALL", "strike": 0.0}


def fetch_tastytrade_data():
    """Synchronous wrapper."""
    return asyncio.run(_fetch_tastytrade_data())


# ══════════════════════════════════════════════════════════════════════════════
# SPREAD GROUPING
# ══════════════════════════════════════════════════════════════════════════════

def group_spreads(tt_positions):
    """
    Group individual option legs into spreads when applicable.

    Logic:
        Bull Call Spread: two CALLs, same ticker + expiration, different strikes.
            - Long leg  = lower strike (more expensive)
            - Short leg = higher strike (cheaper)
        All other positions: treated as individual (Long Call, Long Put, etc.)

    Returns:
        spreads   (list of dicts) — grouped spread records
        singles   (list of dicts) — individual position records
    """
    spreads = []
    singles = []
    used    = set()

    calls_by_key = {}
    for i, pos in enumerate(tt_positions):
        if pos["option_type"] == "CALL":
            key = (pos["ticker"], str(pos["expiration"]))
            calls_by_key.setdefault(key, []).append((i, pos))

    # Detect Bull Call Spreads: 2 CALLs same ticker+exp, different strikes
    for key, legs in calls_by_key.items():
        if len(legs) == 2:
            idx_a, leg_a = legs[0]
            idx_b, leg_b = legs[1]

            # Long leg = quantity_direction 'Long', Short leg = 'Short'
            # Fallback: lower strike = long leg
            if leg_a.get("quantity_direction") == "Long":
                long_leg, short_leg = leg_a, leg_b
                long_idx, short_idx = idx_a, idx_b
            elif leg_b.get("quantity_direction") == "Long":
                long_leg, short_leg = leg_b, leg_a
                long_idx, short_idx = idx_b, idx_a
            elif leg_a["strike"] < leg_b["strike"]:
                long_leg, short_leg = leg_a, leg_b
                long_idx, short_idx = idx_a, idx_b
            else:
                long_leg, short_leg = leg_b, leg_a
                long_idx, short_idx = idx_b, idx_a

            net_debit  = round(long_leg["avg_open_price"] - short_leg["avg_open_price"], 2)
            total_cost = round(net_debit * abs(long_leg["quantity"]) * 100, 2)

            spreads.append({
                "type":          "Bull Call Spread",
                "ticker":        long_leg["ticker"],
                "expiration":    long_leg["expiration"],
                "strike_low":    long_leg["strike"],
                "strike_high":   short_leg["strike"],
                "contracts":     abs(long_leg["quantity"]),
                "premium_paid":  net_debit,
                "total_cost":    total_cost,
                "avg_open_long": long_leg["avg_open_price"],
                "avg_open_short":short_leg["avg_open_price"],
                # Store both symbols for close detection
                "symbol_long":   long_leg["symbol"],
                "symbol_short":  short_leg["symbol"],
                # Use long leg symbol as primary tastytrade_symbol
                "tastytrade_symbol": long_leg["symbol"],
                "tastytrade_symbol_short": short_leg["symbol"],
            })
            used.add(long_idx)
            used.add(short_idx)

    # Everything else is a single position
    for i, pos in enumerate(tt_positions):
        if i not in used:
            singles.append(pos)

    return spreads, singles


# ══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_db_connection():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def ensure_tables():
    """Create account_snapshots table if it doesn't exist."""
    conn = get_db_connection()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id                      SERIAL PRIMARY KEY,
            account_number          VARCHAR(20),
            net_liquidating_value   DECIMAL(12,2),
            equity_buying_power     DECIMAL(12,2),
            derivative_buying_power DECIMAL(12,2),
            cash_balance            DECIMAL(12,2),
            pending_cash            DECIMAL(12,2),
            long_derivative_value   DECIMAL(12,2),
            maintenance_excess      DECIMAL(12,2),
            snapshot_at             TIMESTAMP DEFAULT NOW(),
            created_at              TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        ALTER TABLE positions
        ADD COLUMN IF NOT EXISTS tastytrade_symbol       VARCHAR(50),
        ADD COLUMN IF NOT EXISTS tastytrade_symbol_short VARCHAR(50),
        ADD COLUMN IF NOT EXISTS broker                  VARCHAR(20) DEFAULT 'tastytrade',
        ADD COLUMN IF NOT EXISTS option_type             VARCHAR(10)
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_open_positions_from_db():
    """Get all open positions from DB."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, ticker, strategy, strike_low, strike_high,
               expiration, contracts, total_cost, tastytrade_symbol,
               tastytrade_symbol_short, broker, opened_at
        FROM positions
        WHERE UPPER(status) = 'OPEN'
        ORDER BY opened_at DESC
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def save_account_snapshot(balances):
    """Save account balance snapshot to DB."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO account_snapshots
            (account_number, net_liquidating_value, equity_buying_power,
             derivative_buying_power, cash_balance, pending_cash,
             long_derivative_value, maintenance_excess)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        balances["account_number"],
        balances["net_liquidating_value"],
        balances["equity_buying_power"],
        balances["derivative_buying_power"],
        balances["cash_balance"],
        balances["pending_cash"],
        balances["long_derivative_value"],
        balances["maintenance_excess"],
    ))
    snapshot_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return snapshot_id


def insert_spread(spread, account_number):
    """Insert a Bull Call Spread as a single consolidated DB record."""
    conn = get_db_connection()
    cur  = conn.cursor()

    notes = (
        f"Bull Call Spread ${spread['strike_low']}/${spread['strike_high']} "
        f"exp {spread['expiration']}. "
        f"Long avg: ${spread['avg_open_long']:.2f} | "
        f"Short avg: ${spread['avg_open_short']:.2f} | "
        f"Net debit: ${spread['premium_paid']:.2f}"
    )

    cur.execute("""
        INSERT INTO positions
            (ticker, strategy, broker, is_paper,
             strike_low, strike_high, contracts, expiration,
             premium_paid, total_cost,
             status, opened_at, price_at_open,
             tastytrade_symbol, tastytrade_symbol_short,
             option_type, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'OPEN', NOW(), 0.0,
                %s, %s, %s, %s)
        RETURNING id
    """, (
        spread["ticker"],
        spread["type"],
        "tastytrade",
        False,
        spread["strike_low"],
        spread["strike_high"],
        spread["contracts"],
        spread["expiration"],
        spread["premium_paid"],
        spread["total_cost"],
        spread["tastytrade_symbol"],
        spread["tastytrade_symbol_short"],
        "CALL",
        notes,
    ))

    pos_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return pos_id


def insert_position(tt_pos, account_number):
    """Insert a single-leg position (Long Call, Long Put, etc.) into DB."""
    conn = get_db_connection()
    cur  = conn.cursor()

    qty = tt_pos["quantity"]
    if tt_pos["option_type"] == "CALL" and qty > 0:
        strategy = "Long Call"
    elif tt_pos["option_type"] == "CALL" and qty < 0:
        strategy = "Short Call"
    elif tt_pos["option_type"] == "PUT" and qty > 0:
        strategy = "Long Put"
    else:
        strategy = "Short Put"

    notes = (
        f"Auto-imported from Tastytrade. "
        f"Avg open: ${tt_pos['avg_open_price']:.2f}"
    )

    cur.execute("""
        INSERT INTO positions
            (ticker, strategy, broker, is_paper,
             strike_low, strike_high, contracts, expiration,
             premium_paid, total_cost,
             status, opened_at, price_at_open,
             tastytrade_symbol, option_type, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'OPEN', NOW(), 0.0,
                %s, %s, %s)
        RETURNING id
    """, (
        tt_pos["ticker"],
        strategy,
        "tastytrade",
        False,
        tt_pos["strike"],
        tt_pos["strike"],
        abs(tt_pos["quantity"]),
        tt_pos["expiration"],
        tt_pos["avg_open_price"],
        abs(tt_pos["avg_open_price"]) * abs(tt_pos["quantity"]) * 100,
        tt_pos["symbol"],
        tt_pos["option_type"],
        notes,
    ))

    pos_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return pos_id


def close_position_in_db(db_pos_id, tt_pos, close_reason="Closed in Tastytrade"):
    """Mark a position as closed in DB and calculate P&L."""
    conn = get_db_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT total_cost, contracts, premium_paid
        FROM positions WHERE id = %s
    """, (db_pos_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return

    total_cost, contracts, premium_paid = row
    total_cost   = float(total_cost or 0)
    contracts    = int(contracts or 1)

    close_price    = tt_pos["mark"] if tt_pos else 0.0
    total_received = round(close_price * contracts * 100, 2)
    gross_pnl      = round(total_received - total_cost, 2)
    pnl_pct        = round(gross_pnl / total_cost * 100, 2) if total_cost else 0

    cur.execute("""
        UPDATE positions SET
            status           = 'CLOSED',
            closed_at        = NOW(),
            premium_received = %s,
            total_received   = %s,
            gross_pnl        = %s,
            net_pnl          = %s,
            pnl_pct          = %s,
            close_reason     = %s
        WHERE id = %s
    """, (
        close_price,
        total_received,
        gross_pnl,
        gross_pnl,
        pnl_pct,
        close_reason,
        db_pos_id,
    ))

    conn.commit()
    cur.close()
    conn.close()
    return gross_pnl


# ══════════════════════════════════════════════════════════════════════════════
# SYNC LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def run_sync():
    """
    Main sync: Tastytrade → DB.

    1. Fetch Tastytrade positions + balances
    2. Group legs into spreads (Bull Call Spread detection)
    3. Save account snapshot
    4. Insert new positions (spreads as 1 record, singles as 1 record each)
    5. Close positions no longer in Tastytrade
    """
    print(f"\n{'=' * 55}")
    print(f"  TRADE SYNC — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 55}\n")

    ensure_tables()

    print("  Fetching from Tastytrade...", end=" ", flush=True)
    try:
        tt_data = fetch_tastytrade_data()
        print("OK")
    except Exception as e:
        print(f"ERROR: {e}")
        return

    account_number = tt_data["account_number"]
    balances       = tt_data["balances"]
    tt_positions   = tt_data["positions"]

    # Save account snapshot
    snapshot_id = save_account_snapshot(balances)
    print(f"  Account snapshot saved (id={snapshot_id})")
    print(f"  Net liquidating value:   ${balances['net_liquidating_value']:,.2f}")
    print(f"  Derivative buying power: ${balances['derivative_buying_power']:,.2f}")
    print(f"  Pending cash:            ${balances['pending_cash']:,.2f}")
    if balances['long_derivative_value']:
        print(f"  Options value:           ${balances['long_derivative_value']:,.2f}")

    # Group legs into spreads
    spreads, singles = group_spreads(tt_positions)
    print(f"\n  Tastytrade positions: {len(tt_positions)} legs "
          f"→ {len(spreads)} spread(s) + {len(singles)} single(s)")

    # Get DB positions
    db_positions = get_open_positions_from_db()
    print(f"  DB open positions:    {len(db_positions)}")

    # ── Build set of known TT symbols in DB ───────────────────────────────────
    # For spreads: check by symbol_long (tastytrade_symbol)
    # For singles: check by tastytrade_symbol
    db_symbols = set()
    for p in db_positions:
        if p.get("tastytrade_symbol"):
            db_symbols.add(p["tastytrade_symbol"])
        if p.get("tastytrade_symbol_short"):
            db_symbols.add(p["tastytrade_symbol_short"])

    new_count = 0

    # ── Insert new spreads ────────────────────────────────────────────────────
    for spread in spreads:
        if spread["tastytrade_symbol"] not in db_symbols:
            pos_id = insert_spread(spread, account_number)
            print(f"\n  NEW spread imported:")
            print(f"    {spread['ticker']} Bull Call Spread "
                  f"${spread['strike_low']}/${spread['strike_high']} "
                  f"exp {spread['expiration']}")
            print(f"    Contracts: {spread['contracts']} | "
                  f"Net debit: ${spread['premium_paid']:.2f} | "
                  f"Total cost: ${spread['total_cost']:.2f}")
            print(f"    DB id: {pos_id}")
            new_count += 1

    # ── Insert new singles ────────────────────────────────────────────────────
    for tt_pos in singles:
        if tt_pos["symbol"] not in db_symbols:
            pos_id = insert_position(tt_pos, account_number)
            print(f"\n  NEW position imported:")
            print(f"    {tt_pos['ticker']} {tt_pos['option_type']} "
                  f"${tt_pos['strike']} exp {tt_pos['expiration']}")
            print(f"    Qty: {tt_pos['quantity']} | "
                  f"Avg open: ${tt_pos['avg_open_price']:.2f} | "
                  f"Cost: ${abs(tt_pos['avg_open_price']) * abs(tt_pos['quantity']) * 100:.2f}")
            print(f"    DB id: {pos_id}")
            new_count += 1

    # ── Find closed positions (in DB but all legs gone from TT) ───────────────
    # Build set of all current TT symbols
    tt_all_symbols = {p["symbol"] for p in tt_positions}

    closed_count = 0
    for db_pos in db_positions:
        sym_long  = db_pos.get("tastytrade_symbol")
        sym_short = db_pos.get("tastytrade_symbol_short")

        if sym_short:
            # It's a spread: close only when BOTH legs are gone
            both_gone = (
                (sym_long  not in tt_all_symbols) and
                (sym_short not in tt_all_symbols)
            )
            if both_gone:
                pnl = close_position_in_db(db_pos["id"], None, "Closed in Tastytrade")
                print(f"\n  CLOSED spread detected:")
                print(f"    {db_pos['ticker']} (DB id={db_pos['id']})")
                print(f"    P&L: ${pnl:.2f}" if pnl is not None else "    P&L: unknown")
                closed_count += 1
        else:
            # Single leg: close when symbol is gone
            if sym_long and sym_long not in tt_all_symbols:
                pnl = close_position_in_db(db_pos["id"], None, "Closed in Tastytrade")
                print(f"\n  CLOSED position detected:")
                print(f"    {db_pos['ticker']} (DB id={db_pos['id']})")
                print(f"    P&L: ${pnl:.2f}" if pnl is not None else "    P&L: unknown")
                closed_count += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 55}")
    print(f"  Sync complete:")
    print(f"    New positions imported: {new_count}")
    print(f"    Positions closed:       {closed_count}")
    print(f"    Account snapshot:       saved")
    print(f"{'=' * 55}\n")


# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

def cmd_list():
    """Show open positions from DB."""
    positions = get_open_positions_from_db()
    print(f"\n  Open positions in DB: {len(positions)}\n")
    for p in positions:
        exp    = str(p["expiration"])[:10] if p["expiration"] else "N/A"
        cost   = float(p["total_cost"] or 0)
        broker = p.get("broker", "N/A")
        print(f"  #{p['id']} {p['ticker']} {p['strategy']} "
              f"${p['strike_low']}/${p['strike_high']} | Exp {exp} | "
              f"Cost ${cost:.2f} | {broker}")
    print()


def cmd_account():
    """Show account balance history."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT snapshot_at, net_liquidating_value, derivative_buying_power,
               pending_cash, long_derivative_value
        FROM account_snapshots
        ORDER BY snapshot_at DESC
        LIMIT 30
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    print(f"\n  Account history (last {len(rows)} snapshots):\n")
    print(f"  {'Date/Time':<22} {'NLV':>10} {'Deriv BP':>10} "
          f"{'Pending':>10} {'Options':>10}")
    print(f"  {'-'*22} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

    for row in rows:
        snap_at, nlv, dbp, pending, options = row
        print(f"  {str(snap_at)[:19]:<22} "
              f"${float(nlv or 0):>9,.0f} "
              f"${float(dbp or 0):>9,.0f} "
              f"${float(pending or 0):>9,.0f} "
              f"${float(options or 0):>9,.0f}")
    print()


def cmd_history():
    """Show closed positions P&L history."""
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT ticker, strategy, strike_low, strike_high, expiration,
               total_cost, total_received, net_pnl, pnl_pct,
               close_reason, closed_at
        FROM positions
        WHERE UPPER(status) = 'CLOSED'
        ORDER BY closed_at DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    print(f"\n  Closed positions history (last {len(rows)}):\n")
    for row in rows:
        ticker, strategy, strike_low, strike_high, exp, cost, received, pnl, pnl_pct, reason, closed = row
        pnl     = float(pnl or 0)
        pnl_pct = float(pnl_pct or 0)
        cost    = float(cost or 0)
        emoji   = "+" if pnl >= 0 else "-"
        strikes = f"${strike_low}/{strike_high}" if strike_high and strike_low != strike_high else f"${strike_low}"
        print(f"  {ticker} {strategy} {strikes} | "
              f"Cost ${cost:.0f} | "
              f"P&L {emoji}${abs(pnl):.0f} ({pnl_pct:+.1f}%) | "
              f"{str(closed)[:10] if closed else 'N/A'}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trade sync with Tastytrade")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sync",    action="store_true", help="Sync positions + save account snapshot")
    group.add_argument("--list",    action="store_true", help="Show open positions from DB")
    group.add_argument("--account", action="store_true", help="Show account balance history")
    group.add_argument("--history", action="store_true", help="Show closed positions P&L history")
    args = parser.parse_args()

    if args.sync:
        run_sync()
    elif args.list:
        cmd_list()
    elif args.account:
        cmd_account()
    elif args.history:
        cmd_history()