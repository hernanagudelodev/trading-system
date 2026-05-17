"""
db.py
=====
Database management for the v2 backtest system.
Handles v2_snapshots, v2_criteria, and v2_outcomes tables only.
Does NOT touch existing tables (analysis, criteria_scores, positions, outcomes).

Connection uses DATABASE_URL from .env — same Railway PostgreSQL instance.
"""

import os
import json
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


# ══════════════════════════════════════════════════════════════════════════════
# CONNECTION
# ══════════════════════════════════════════════════════════════════════════════

def get_connection():
    return psycopg2.connect(DATABASE_URL)


# ══════════════════════════════════════════════════════════════════════════════
# SNAPSHOTS
# ══════════════════════════════════════════════════════════════════════════════

def save_snapshot(ticker, backtest_date, price, sector=None):
    """
    Save a backtest snapshot (one per ticker per day).
    Uses UPSERT — safe to re-run.

    Returns:
        int — snapshot id
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        INSERT INTO v2_snapshots (ticker, backtest_date, price, sector)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (ticker, backtest_date)
        DO UPDATE SET
            price  = EXCLUDED.price,
            sector = EXCLUDED.sector
        RETURNING id;
    """, (ticker, backtest_date, price, sector))

    snapshot_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return snapshot_id


def get_last_backtest_date(ticker):
    """
    Get the most recent backtest_date for a ticker.
    Used by --resume mode.

    Returns:
        date | None
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT MAX(backtest_date)
        FROM v2_snapshots
        WHERE ticker = %s;
    """, (ticker,))

    result = cur.fetchone()[0]
    cur.close()
    conn.close()
    return result


# ══════════════════════════════════════════════════════════════════════════════
# CRITERIA
# ══════════════════════════════════════════════════════════════════════════════

# Criteria that store a single float in raw_value
NUMERIC_CRITERIA = {
    "trend_25d_pct",
    "sma50",
    "sma200",
    "sma50_pct",
    "sma200_pct",
    "rsi",
    "week_52_position",
    "nearest_support_pct",
    "nearest_resistance_pct",
    "hv_30d",
    "volume_ratio",
}

# Criteria that are boolean — stored as 1.0 / 0.0
BOOL_CRITERIA = {
    "above_sma50",
    "above_sma200",
}

# Criteria that need raw_extra (non-numeric)
EXTRA_CRITERIA = {
    "sma50_direction",      # "RISING" | "FLAT" | "FALLING"
    "candlestick_signal",   # "BULLISH" | "BEARISH" | "NEUTRAL"
    "candlestick_pattern",  # "HAMMER", "DOJI", etc.
}

# Metadata keys — not saved as criteria
SKIP_KEYS = {"price", "date", "ticker", "timestamp"}


def save_criteria(snapshot_id, raw):
    """
    Save all raw criteria values for a snapshot.
    Uses UPSERT — safe to re-run.

    Numeric values -> raw_value (FLOAT)
    Boolean values -> raw_value (1.0 or 0.0)
    String values  -> raw_extra (JSONB)

    Args:
        snapshot_id (int)  — from save_snapshot()
        raw         (dict) — from get_raw_criteria()
    """
    conn = get_connection()
    cur  = conn.cursor()

    for key, value in raw.items():
        if key in SKIP_KEYS:
            continue

        raw_value = None
        raw_extra = None

        if key in BOOL_CRITERIA:
            raw_value = 1.0 if value else 0.0 if value is not None else None

        elif key in NUMERIC_CRITERIA:
            raw_value = float(value) if value is not None else None

        elif key in EXTRA_CRITERIA:
            raw_extra = json.dumps({"value": value}) if value is not None else None

        else:
            # Unknown key — try to store as numeric, fall back to extra
            if isinstance(value, (int, float)) and value is not None:
                raw_value = float(value)
            elif value is not None:
                raw_extra = json.dumps({"value": value})

        cur.execute("""
            INSERT INTO v2_criteria (snapshot_id, criterion, raw_value, raw_extra)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (snapshot_id, criterion)
            DO UPDATE SET
                raw_value = EXCLUDED.raw_value,
                raw_extra = EXCLUDED.raw_extra;
        """, (snapshot_id, key, raw_value, raw_extra))

    conn.commit()
    cur.close()
    conn.close()


def get_snapshots_without_outcomes(limit=None):
    """
    Get snapshots that don't have outcomes yet.
    Used by simulate.py.

    Returns:
        list of dicts: {id, ticker, backtest_date, price, sector}
    """
    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    limit_clause = f"LIMIT {limit}" if limit else ""

    cur.execute(f"""
        SELECT s.id, s.ticker, s.backtest_date, s.price, s.sector
        FROM v2_snapshots s
        LEFT JOIN v2_outcomes o ON o.snapshot_id = s.id
        WHERE o.id IS NULL
        ORDER BY s.ticker, s.backtest_date
        {limit_clause};
    """)

    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def save_outcome(snapshot_id, strategy, exit_day, exit_price, pct_change,
                 was_successful, exit_reason):
    """
    Save the simulated outcome for a snapshot + strategy combination.
    Used by simulate.py.

    Multiple strategies can be saved for the same snapshot.

    Args:
        snapshot_id    (int)   — from v2_snapshots
        strategy       (str)   — strategy name e.g. "BULL_CALL_SPREAD", "LONG_CALL"
        exit_day       (int)   — day number when exit condition was met
        exit_price     (float) — price on exit day
        pct_change     (float) — % price change from entry to exit
        was_successful (bool)  — True if target was reached
        exit_reason    (str)   — "TARGET_REACHED" | "STOP_LOSS" | "EXPIRED" | "NO_DATA"
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        INSERT INTO v2_outcomes (
            snapshot_id, strategy, exit_day, exit_price,
            pct_change, was_successful, exit_reason
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_id, strategy)
        DO UPDATE SET
            exit_day       = EXCLUDED.exit_day,
            exit_price     = EXCLUDED.exit_price,
            pct_change     = EXCLUDED.pct_change,
            was_successful = EXCLUDED.was_successful,
            exit_reason    = EXCLUDED.exit_reason,
            recorded_at    = NOW();
    """, (snapshot_id, strategy, exit_day, exit_price, pct_change, was_successful, exit_reason))

    conn.commit()
    cur.close()
    conn.close()


def get_all_snapshots_with_criteria(strategy=None):
    """
    Get all snapshots with their raw criteria pivoted into columns.
    Used by audit.py for scoring simulation.

    Args:
        strategy (str | None) — filter outcomes by strategy name
                                e.g. "BULL_CALL_SPREAD", "LONG_CALL"
                                None = include all outcomes (or no outcome)

    Returns:
        list of dicts: {snapshot_id, ticker, date, price, sector,
                        strategy, was_successful, pct_change, exit_reason,
                        criteria: {criterion: value}}
    """
    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    strategy_filter = "AND o.strategy = %(strategy)s" if strategy else ""

    cur.execute(f"""
        SELECT
            s.id            AS snapshot_id,
            s.ticker,
            s.backtest_date AS date,
            s.price,
            s.sector,
            o.strategy,
            o.was_successful,
            o.pct_change,
            o.exit_reason,
            json_object_agg(
                c.criterion,
                CASE
                    WHEN c.raw_value IS NOT NULL THEN to_json(c.raw_value)
                    WHEN c.raw_extra IS NOT NULL THEN to_json(c.raw_extra -> 'value')
                    ELSE 'null'::json
                END
            ) AS criteria
        FROM v2_snapshots s
        JOIN v2_criteria c ON c.snapshot_id = s.id
        LEFT JOIN v2_outcomes o ON o.snapshot_id = s.id {strategy_filter}
        GROUP BY s.id, s.ticker, s.backtest_date, s.price, s.sector,
                 o.strategy, o.was_successful, o.pct_change, o.exit_reason
        ORDER BY s.ticker, s.backtest_date;
    """, {"strategy": strategy} if strategy else {})

    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows