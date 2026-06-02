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
    2. Compare with DB positions (status='open')
    3. New positions in Tastytrade → insert in DB
    4. Positions in DB but not in Tastytrade → mark as closed, calculate P&L
    5. Save account snapshot (balances) to DB

DB tables used:
    positions       — open/closed option positions
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

    Returns:
        dict with keys:
            account_number  (str)
            balances        (dict)
            positions       (list of dicts)
    """
    from tastytrade import Session
    from tastytrade.account import Account

    session = Session(
        os.getenv("TASTYTRADE_CLIENT_SECRET"),
        os.getenv("TASTYTRADE_REFRESH_TOKEN")
    )

    accounts = await Account.get(session)
    account  = accounts[0]

    # ── Balances ──────────────────────────────────────────────────────────────
    bal = await account.get_balances(session)
    balances = {
        "account_number":        account.account_number,
        "net_liquidating_value": float(bal.net_liquidating_value or 0),
        "equity_buying_power":   float(bal.equity_buying_power or 0),
        "derivative_buying_power": float(bal.derivative_buying_power or 0),
        "cash_balance":          float(bal.cash_balance or 0),
        "pending_cash":          float(bal.pending_cash or 0),
        "long_derivative_value": float(bal.long_derivative_value or 0),
        "maintenance_excess":    float(bal.maintenance_excess or 0),
        "updated_at":            bal.updated_at,
    }

    # ── Positions ─────────────────────────────────────────────────────────────
    raw_positions = await account.get_positions(session)
    positions = []

    for p in raw_positions:
        print(f"DEBUG raw: symbol={p.symbol} qty={p.quantity} instrument={p.instrument_type}")  # <-- agrega esto
        # Only process options (not stocks)
        if p.instrument_type not in ("Equity Option",):
            continue

        # Parse option symbol: .SLB260618C58 → ticker, exp, type, strike
        symbol   = str(p.symbol)
        quantity = int(p.quantity)
        avg_open = float(p.average_open_price or 0)
        close_px = float(p.close_price or 0)
        mark     = float(p.mark or close_px)

        # Parse OCC symbol
        parsed = _parse_option_symbol(symbol)

        positions.append({
            "symbol":           symbol,
            "ticker":           parsed["ticker"],
            "expiration":       parsed["expiration"],
            "strike":           parsed["strike"],
            "option_type":      parsed["option_type"],  # CALL or PUT
            "quantity":         quantity,
            "avg_open_price":   avg_open,
            "mark":             mark,
            "close_price":      close_px,
            "cost_basis":       round(avg_open * quantity * 100, 2),
            "market_value":     round(mark * quantity * 100, 2),
            "unrealized_pnl":   round((mark - avg_open) * quantity * 100, 2),
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

    # Remove leading dot and collapse spaces
    clean = symbol.lstrip(".").replace(" ", "")

    # OCC format: TICKER YYMMDD C/P STRIKE(8 digits, strike * 1000)
    # Example: XYZ260702C00079000 → ticker=XYZ, exp=260702, C, strike=79.0
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
        return {
            "ticker":      ticker,
            "expiration":  exp_date,
            "option_type": opt_type,
            "strike":      strike,
        }

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
        return {
            "ticker":      ticker,
            "expiration":  exp_date,
            "option_type": opt_type,
            "strike":      strike,
        }

    # Cannot parse
    return {
        "ticker":      clean[:10],
        "expiration":  None,
        "option_type": "CALL",
        "strike":      0.0,
    }

def fetch_tastytrade_data():
    """Synchronous wrapper."""
    return asyncio.run(_fetch_tastytrade_data())


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

    # Add tastytrade_symbol column to positions if not exists
    cur.execute("""
        ALTER TABLE positions
        ADD COLUMN IF NOT EXISTS tastytrade_symbol VARCHAR(50),
        ADD COLUMN IF NOT EXISTS broker            VARCHAR(20) DEFAULT 'tastytrade',
        ADD COLUMN IF NOT EXISTS option_type       VARCHAR(10)
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
               broker, opened_at
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

def insert_position(tt_pos, account_number):
    """Insert a new position found in Tastytrade into DB."""
    conn = get_db_connection()
    cur  = conn.cursor()

    # Determine strategy
    qty = tt_pos["quantity"]
    if tt_pos["option_type"] == "CALL" and qty > 0:
        strategy = "Long Call"
    elif tt_pos["option_type"] == "CALL" and qty < 0:
        strategy = "Short Call"
    elif tt_pos["option_type"] == "PUT" and qty > 0:
        strategy = "Long Put"
    else:
        strategy = "Short Put"

    notes = f"Auto-imported from Tastytrade. Avg open: ${tt_pos['avg_open_price']:.2f}"

    cur.execute("""
        INSERT INTO positions
            (ticker, strategy, broker, is_paper,
             strike_low, strike_high, contracts, expiration,
             premium_paid, total_cost,
             status, opened_at, price_at_open,
             tastytrade_symbol, option_type, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'open', NOW(), %s, %s, %s, %s)
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
        tt_pos["cost_basis"],
        0.0,
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

    # Get original position data
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

    # Use mark price as close price
    close_price    = tt_pos["mark"] if tt_pos else 0.0
    total_received = round(close_price * contracts * 100, 2)
    gross_pnl      = round(total_received - total_cost, 2)
    pnl_pct        = round(gross_pnl / total_cost * 100, 2) if total_cost else 0

    cur.execute("""
        UPDATE positions SET
            status           = 'closed',
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
        gross_pnl,      # net = gross (no separate commission tracking here)
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
    2. Save account snapshot
    3. Compare positions: insert new, close removed
    """
    print(f"\n{'=' * 55}")
    print(f"  TRADE SYNC — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 55}\n")

    # Ensure tables exist
    ensure_tables()

    # Fetch from Tastytrade
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
    print(f"  Net liquidating value: ${balances['net_liquidating_value']:,.2f}")
    print(f"  Derivative buying power: ${balances['derivative_buying_power']:,.2f}")
    print(f"  Pending cash: ${balances['pending_cash']:,.2f}")
    if balances['long_derivative_value']:
        print(f"  Options value: ${balances['long_derivative_value']:,.2f}")

    # Get DB positions
    db_positions = get_open_positions_from_db()

    print(f"\n  Tastytrade positions: {len(tt_positions)}")
    print(f"  DB open positions:    {len(db_positions)}")

    # ── Find new positions (in TT but not in DB) ──────────────────────────────
    db_symbols = {
        p.get("tastytrade_symbol") for p in db_positions
        if p.get("tastytrade_symbol")
    }

    new_count = 0
    for tt_pos in tt_positions:
        if tt_pos["symbol"] not in db_symbols:
            print(f"DEBUG tt_pos: {tt_pos}")  # <-- agrega esta línea
            pos_id = insert_position(tt_pos, account_number)
            print(f"\n  NEW position imported:")
            print(f"    {tt_pos['ticker']} {tt_pos['option_type']} "
                  f"${tt_pos['strike']} exp {tt_pos['expiration']}")
            print(f"    Qty: {tt_pos['quantity']} | "
                  f"Avg open: ${tt_pos['avg_open_price']:.2f} | "
                  f"Cost: ${tt_pos['cost_basis']:.2f}")
            print(f"    DB id: {pos_id}")
            new_count += 1

    # ── Find closed positions (in DB but not in TT) ───────────────────────────
    tt_symbols = {p["symbol"] for p in tt_positions}

    closed_count = 0
    for db_pos in db_positions:
        db_symbol = db_pos.get("tastytrade_symbol")
        if db_symbol and db_symbol not in tt_symbols:
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
              f"${p['strike_low']} | Exp {exp} | "
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
        SELECT ticker, strategy, strike_low, expiration,
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
        ticker, strategy, strike, exp, cost, received, pnl, pnl_pct, reason, closed = row
        pnl      = float(pnl or 0)
        pnl_pct  = float(pnl_pct or 0)
        cost     = float(cost or 0)
        emoji    = "+" if pnl >= 0 else "-"
        print(f"  {ticker} {strategy} ${strike} | "
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