"""
db.py
=====
Database management module for Bull Call Spread analysis system.

This file is responsible ONLY for:
    - Connecting to PostgreSQL (Railway)
    - Creating tables if they don't exist
    - Reading and writing data

It does NOT score, analyze, or make trading decisions.

Schema:
    analysis        → one record per ticker per run
    criteria_scores → one record per criterion per analysis
    positions       → open and closed trading positions
    outcomes        → actual market results for audit

Other modules:
    criteria.py  → fetches raw market data
    scoring.py   → applies scoring rules
    audit.py     → reads outcomes and suggests score adjustments
    scanner.py   → orchestrates analysis runs
"""

import os
import psycopg2
import psycopg2.extras
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


# ══════════════════════════════════════════════════════════════════════════════
# CONNECTION
# ══════════════════════════════════════════════════════════════════════════════

def get_connection():
    """
    Open and return a new PostgreSQL connection.
    Uses DATABASE_URL from .env file.
    """
    return psycopg2.connect(DATABASE_URL)


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA CREATION
# ══════════════════════════════════════════════════════════════════════════════

def create_tables():
    """
    Create all tables if they don't exist.
    Safe to run multiple times — uses IF NOT EXISTS.

    Tables:
        analysis        → one record per ticker per analysis run
        criteria_scores → breakdown of each criterion score per analysis
        positions       → paper and live trading positions
        outcomes        → actual price results used for audit
    """
    conn = get_connection()
    cur  = conn.cursor()

    # ── analysis ─────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS analysis (
            id              SERIAL PRIMARY KEY,
            ticker          VARCHAR(10)     NOT NULL,
            timestamp       TIMESTAMP       NOT NULL,
            price           DECIMAL(10,2)   NOT NULL,
            score           INTEGER         NOT NULL,
            score_max       INTEGER         NOT NULL,
            score_pct       DECIMAL(6,2)    NOT NULL,
            verdict         VARCHAR(20)     NOT NULL,
            is_backtest     BOOLEAN         DEFAULT FALSE,
            backtest_date   DATE,
            created_at      TIMESTAMP       DEFAULT NOW()
        );
    """)

    # ── criteria_scores ───────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS criteria_scores (
            id              SERIAL PRIMARY KEY,
            analysis_id     INTEGER         NOT NULL REFERENCES analysis(id),
            criterion       VARCHAR(50)     NOT NULL,
            score           INTEGER         NOT NULL,
            label           VARCHAR(100)    NOT NULL
        );
    """)

    # ── positions ─────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id                  SERIAL PRIMARY KEY,

            -- Identification
            ticker              VARCHAR(10)     NOT NULL,
            strategy            VARCHAR(50)     NOT NULL,
            broker              VARCHAR(50),
            is_paper            BOOLEAN         DEFAULT TRUE,

            -- Spread structure
            strike_low          DECIMAL(10,2),
            strike_high         DECIMAL(10,2),
            contracts           INTEGER         NOT NULL DEFAULT 1,
            expiration          DATE,

            -- Entry
            premium_paid        DECIMAL(10,2),
            total_cost          DECIMAL(10,2),
            commission_open     DECIMAL(10,2)   DEFAULT 0,
            opened_at           TIMESTAMP,
            price_at_open       DECIMAL(10,2),

            -- Exit
            premium_received    DECIMAL(10,2),
            total_received      DECIMAL(10,2),
            commission_close    DECIMAL(10,2)   DEFAULT 0,
            closed_at           TIMESTAMP,
            price_at_close      DECIMAL(10,2),

            -- Result
            gross_pnl           DECIMAL(10,2),
            total_commission    DECIMAL(10,2),
            net_pnl             DECIMAL(10,2),
            pnl_pct             DECIMAL(10,4),

            -- Status
            status              VARCHAR(20)     DEFAULT 'OPEN',
            close_reason        VARCHAR(50),

            -- Score context at entry
            score_at_open       INTEGER,
            score_pct_at_open   DECIMAL(6,2),
            verdict_at_open     VARCHAR(20),

            -- Notes
            notes               TEXT,

            -- Link to analysis that triggered the trade
            analysis_id         INTEGER         REFERENCES analysis(id),

            created_at          TIMESTAMP       DEFAULT NOW()
        );
    """)

    # ── outcomes ──────────────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS outcomes (
            id                  SERIAL PRIMARY KEY,
            analysis_id         INTEGER         NOT NULL REFERENCES analysis(id),
            ticker              VARCHAR(10)     NOT NULL,
            price_at_analysis   DECIMAL(10,2),
            price_at_30d        DECIMAL(10,2),
            price_at_expiry     DECIMAL(10,2),
            pct_change_30d      DECIMAL(10,4),
            would_have_profited BOOLEAN,
            recorded_at         TIMESTAMP       DEFAULT NOW()
        );
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("✅ Tables created successfully")


# ══════════════════════════════════════════════════════════════════════════════
# ANALYSIS — READ / WRITE
# ══════════════════════════════════════════════════════════════════════════════

def save_analysis(scored_result):
    """
    Save a real-time scored analysis result to the database.
    Uses UPSERT to avoid duplicates if scanner runs twice same day.

    Requires unique index: unique_real_analysis_per_day
    Created with:
        CREATE UNIQUE INDEX unique_real_analysis_per_day
        ON analysis (ticker, DATE(timestamp))
        WHERE is_backtest = FALSE;

    Args:
        scored_result (dict) — output of scoring.score_criteria()

    Returns:
        int — the analysis id
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        INSERT INTO analysis (
            ticker, timestamp, price,
            score, score_max, score_pct, verdict,
            is_backtest, backtest_date
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, NULL)
        ON CONFLICT ON CONSTRAINT unique_real_analysis_per_day
        DO UPDATE SET
            price     = EXCLUDED.price,
            score     = EXCLUDED.score,
            score_max = EXCLUDED.score_max,
            score_pct = EXCLUDED.score_pct,
            verdict   = EXCLUDED.verdict,
            timestamp = EXCLUDED.timestamp
        RETURNING id;
    """, (
        scored_result["ticker"],
        scored_result["timestamp"],
        scored_result["price"],
        scored_result["score"],
        scored_result["score_max"],
        scored_result["score_pct"],
        scored_result["verdict"],
    ))

    analysis_id = cur.fetchone()[0]

    # Delete old criteria scores before reinserting
    cur.execute(
        "DELETE FROM criteria_scores WHERE analysis_id = %s;",
        (analysis_id,)
    )

    for criterion, data in scored_result["criteria_scores"].items():
        cur.execute("""
            INSERT INTO criteria_scores (analysis_id, criterion, score, label)
            VALUES (%s, %s, %s, %s);
        """, (analysis_id, criterion, data["score"], data["label"]))

    conn.commit()
    cur.close()
    conn.close()
    return analysis_id


def get_latest_analysis(ticker, limit=10):
    """
    Retrieve the most recent real-time analysis records for a ticker.

    Args:
        ticker  (str) — stock symbol
        limit   (int) — max records to return

    Returns:
        list of dicts
    """
    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT a.*,
               json_agg(
                   json_build_object(
                       'criterion', cs.criterion,
                       'score', cs.score,
                       'label', cs.label
                   ) ORDER BY cs.criterion
               ) AS criteria
        FROM analysis a
        LEFT JOIN criteria_scores cs ON cs.analysis_id = a.id
        WHERE a.ticker = %s
          AND a.is_backtest = FALSE
        GROUP BY a.id
        ORDER BY a.timestamp DESC
        LIMIT %s;
    """, (ticker, limit))

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def get_analysis_history(ticker=None, verdict=None, days=30):
    """
    Retrieve real-time analysis history with optional filters.

    Args:
        ticker  (str | None) — filter by ticker
        verdict (str | None) — filter by verdict
        days    (int)        — how many days back to look

    Returns:
        list of dicts
    """
    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    conditions = [
        "a.timestamp >= NOW() - INTERVAL '%s days'",
        "a.is_backtest = FALSE"
    ]
    params = [days]

    if ticker:
        conditions.append("a.ticker = %s")
        params.append(ticker)

    if verdict:
        conditions.append("a.verdict = %s")
        params.append(verdict)

    where = " AND ".join(conditions)

    cur.execute(f"""
        SELECT * FROM analysis a
        WHERE {where}
        ORDER BY a.timestamp DESC;
    """, params)

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# POSITIONS — READ / WRITE
# ══════════════════════════════════════════════════════════════════════════════

def open_position(position_data):
    """
    Record a new trading position (paper or live).

    Args:
        position_data (dict) with keys:
            ticker, strategy, broker, is_paper,
            strike_low, strike_high, contracts, expiration,
            premium_paid, total_cost, commission_open,
            opened_at, price_at_open,
            score_at_open, score_pct_at_open, verdict_at_open,
            notes, analysis_id

    Returns:
        int — the new position id
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        INSERT INTO positions (
            ticker, strategy, broker, is_paper,
            strike_low, strike_high, contracts, expiration,
            premium_paid, total_cost, commission_open,
            opened_at, price_at_open,
            score_at_open, score_pct_at_open, verdict_at_open,
            notes, analysis_id, status
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, 'OPEN'
        ) RETURNING id;
    """, (
        position_data.get("ticker"),
        position_data.get("strategy"),
        position_data.get("broker"),
        position_data.get("is_paper", True),
        position_data.get("strike_low"),
        position_data.get("strike_high"),
        position_data.get("contracts", 1),
        position_data.get("expiration"),
        position_data.get("premium_paid"),
        position_data.get("total_cost"),
        position_data.get("commission_open", 0),
        position_data.get("opened_at", datetime.now()),
        position_data.get("price_at_open"),
        position_data.get("score_at_open"),
        position_data.get("score_pct_at_open"),
        position_data.get("verdict_at_open"),
        position_data.get("notes"),
        position_data.get("analysis_id"),
    ))

    position_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return position_id


def close_position(position_id, close_data):
    """
    Record the closing of an existing position and calculate P&L.

    Args:
        position_id (int)  — id of the position to close
        close_data  (dict) with keys:
            premium_received, commission_close,
            closed_at, price_at_close, close_reason, notes

    Returns:
        dict — updated position with P&L calculated
    """
    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Fetch current position to calculate P&L
    cur.execute("SELECT * FROM positions WHERE id = %s;", (position_id,))
    pos = dict(cur.fetchone())

    premium_received = close_data.get("premium_received", 0)
    commission_close = close_data.get("commission_close", 0)
    contracts        = pos["contracts"]

    total_received   = premium_received * contracts * 100
    total_cost       = float(pos["total_cost"] or 0)
    commission_open  = float(pos["commission_open"] or 0)
    total_commission = commission_open + commission_close
    gross_pnl        = total_received - total_cost
    net_pnl          = gross_pnl - total_commission
    pnl_pct          = (net_pnl / total_cost * 100) if total_cost != 0 else 0

    cur.execute("""
        UPDATE positions SET
            premium_received    = %s,
            total_received      = %s,
            commission_close    = %s,
            closed_at           = %s,
            price_at_close      = %s,
            gross_pnl           = %s,
            total_commission    = %s,
            net_pnl             = %s,
            pnl_pct             = %s,
            status              = 'CLOSED',
            close_reason        = %s,
            notes               = COALESCE(notes || ' | ' || %s, %s)
        WHERE id = %s;
    """, (
        premium_received,
        total_received,
        commission_close,
        close_data.get("closed_at", datetime.now()),
        close_data.get("price_at_close"),
        gross_pnl,
        total_commission,
        net_pnl,
        pnl_pct,
        close_data.get("close_reason", "MANUAL"),
        close_data.get("notes", ""),
        close_data.get("notes", ""),
        position_id,
    ))

    conn.commit()

    # Return updated position
    cur.execute("SELECT * FROM positions WHERE id = %s;", (position_id,))
    updated = dict(cur.fetchone())

    cur.close()
    conn.close()
    return updated


def get_open_positions():
    """
    Retrieve all currently open positions.

    Returns:
        list of dicts
    """
    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT * FROM positions
        WHERE status = 'OPEN'
        ORDER BY opened_at DESC;
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def get_position_history(ticker=None, is_paper=None):
    """
    Retrieve closed positions with optional filters.

    Args:
        ticker   (str | None)  — filter by ticker
        is_paper (bool | None) — filter by paper/live

    Returns:
        list of dicts
    """
    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    conditions = ["status = 'CLOSED'"]
    params     = []

    if ticker:
        conditions.append("ticker = %s")
        params.append(ticker)

    if is_paper is not None:
        conditions.append("is_paper = %s")
        params.append(is_paper)

    where = " AND ".join(conditions)

    cur.execute(f"""
        SELECT * FROM positions
        WHERE {where}
        ORDER BY closed_at DESC;
    """, params)

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# OUTCOMES — READ / WRITE (for audit)
# ══════════════════════════════════════════════════════════════════════════════

def save_outcome(analysis_id, ticker, price_at_analysis,
                 price_at_30d=None, price_at_expiry=None):
    """
    Record the actual market outcome for an analysis.
    Used by audit.py to compare predictions vs reality.
    Uses UPSERT to avoid duplicates on re-runs.

    Requires unique constraint: unique_outcome_analysis
    Created with:
        ALTER TABLE outcomes ADD CONSTRAINT unique_outcome_analysis
        UNIQUE (analysis_id);

    Args:
        analysis_id         (int)   — the analysis being evaluated
        ticker              (str)
        price_at_analysis   (float) — price when analysis was run
        price_at_30d        (float) — price 30 days later
        price_at_expiry     (float) — price at option expiration

    Returns:
        int — outcome id
    """
    conn = get_connection()
    cur  = conn.cursor()

    pct_change   = None
    would_profit = None

    if price_at_30d and price_at_analysis:
        pct_change   = round(
            (price_at_30d - price_at_analysis) / price_at_analysis * 100, 4
        )
        would_profit = pct_change > 0

    cur.execute("""
        INSERT INTO outcomes (
            analysis_id, ticker, price_at_analysis,
            price_at_30d, price_at_expiry,
            pct_change_30d, would_have_profited
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (analysis_id)
        DO UPDATE SET
            price_at_30d        = EXCLUDED.price_at_30d,
            price_at_expiry     = EXCLUDED.price_at_expiry,
            pct_change_30d      = EXCLUDED.pct_change_30d,
            would_have_profited = EXCLUDED.would_have_profited,
            recorded_at         = NOW()
        RETURNING id;
    """, (
        analysis_id, ticker, price_at_analysis,
        price_at_30d, price_at_expiry,
        pct_change, would_profit
    ))

    outcome_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return outcome_id


def get_outcomes_for_audit(days=90, backtest_only=False):
    """
    Retrieve analysis + outcome pairs for audit processing.
    Only returns records where outcomes have been recorded.

    Args:
        days          (int)  — how far back to look
        backtest_only (bool) — if True, only return backtest records

    Returns:
        list of dicts with analysis and outcome data joined
    """
    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    backtest_filter = "AND a.is_backtest = TRUE" if backtest_only else ""

    cur.execute(f"""
        SELECT
            a.id, a.ticker, a.timestamp, a.price,
            a.score, a.score_pct, a.verdict,
            a.is_backtest, a.backtest_date,
            o.price_at_30d, o.pct_change_30d, o.would_have_profited
        FROM analysis a
        JOIN outcomes o ON o.analysis_id = a.id
        WHERE a.timestamp >= NOW() - INTERVAL '%s days'
        {backtest_filter}
        ORDER BY a.timestamp DESC;
    """, (days,))

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# BACKTESTING — READ / WRITE
# ══════════════════════════════════════════════════════════════════════════════

def save_backtest_analysis(scored_result, backtest_date, sector=None):
    """
    Save a backtest analysis result to the database.
    Same as save_analysis but marks is_backtest=True
    and stores the simulated date.
    Uses UPSERT — safe to re-run without creating duplicates.

    Requires unique constraint: unique_backtest_ticker_date
    Created with:
        ALTER TABLE analysis
        ADD CONSTRAINT unique_backtest_ticker_date
        UNIQUE (ticker, backtest_date, is_backtest)
        WHERE is_backtest = TRUE;

    Args:
        scored_result (dict) — output of scoring.score_criteria()
        backtest_date (date) — the date being simulated
        sector        (str)  — GICS sector (e.g. 'Information Technology')

    Returns:
        int — the analysis id
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        INSERT INTO analysis (
            ticker, timestamp, price,
            score, score_max, score_pct, verdict,
            is_backtest, backtest_date, sector
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s)
        ON CONFLICT (ticker, backtest_date, is_backtest)
        WHERE is_backtest = TRUE
        DO UPDATE SET
            price     = EXCLUDED.price,
            score     = EXCLUDED.score,
            score_max = EXCLUDED.score_max,
            score_pct = EXCLUDED.score_pct,
            verdict   = EXCLUDED.verdict,
            timestamp = EXCLUDED.timestamp,
            sector    = EXCLUDED.sector
        RETURNING id;
    """, (
        scored_result["ticker"],
        scored_result["timestamp"],
        scored_result["price"],
        scored_result["score"],
        scored_result["score_max"],
        scored_result["score_pct"],
        scored_result["verdict"],
        backtest_date,
        sector,
    ))

    analysis_id = cur.fetchone()[0]

    # Delete old criteria scores before reinserting
    cur.execute(
        "DELETE FROM criteria_scores WHERE analysis_id = %s;",
        (analysis_id,)
    )

    for criterion, data in scored_result["criteria_scores"].items():
        cur.execute("""
            INSERT INTO criteria_scores (analysis_id, criterion, score, label)
            VALUES (%s, %s, %s, %s);
        """, (analysis_id, criterion, data["score"], data["label"]))

    conn.commit()
    cur.close()
    conn.close()
    return analysis_id

def get_backtest_progress(ticker):
    """
    Get the last backtest_date processed for a ticker.
    Used by --resume mode to continue where it left off.

    Returns:
        date | None
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT MAX(backtest_date)
        FROM analysis
        WHERE ticker = %s AND is_backtest = TRUE;
    """, (ticker,))

    result = cur.fetchone()[0]
    cur.close()
    conn.close()
    return result


def get_backtest_results(ticker=None, verdict=None):
    """
    Retrieve backtest analysis records for audit.

    Args:
        ticker  (str | None) — filter by ticker
        verdict (str | None) — filter by verdict

    Returns:
        list of dicts
    """
    conn = get_connection()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    conditions = ["a.is_backtest = TRUE"]
    params     = []

    if ticker:
        conditions.append("a.ticker = %s")
        params.append(ticker)

    if verdict:
        conditions.append("a.verdict = %s")
        params.append(verdict)

    where = " AND ".join(conditions)

    cur.execute(f"""
        SELECT a.*,
               json_agg(
                   json_build_object(
                       'criterion', cs.criterion,
                       'score', cs.score,
                       'label', cs.label
                   ) ORDER BY cs.criterion
               ) AS criteria
        FROM analysis a
        LEFT JOIN criteria_scores cs ON cs.analysis_id = a.id
        WHERE {where}
        GROUP BY a.id
        ORDER BY a.backtest_date ASC, a.ticker ASC;
    """, params)

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY
# ══════════════════════════════════════════════════════════════════════════════

def test_connection():
    """
    Verify database connectivity.
    Prints success or error message.
    """
    try:
        conn = get_connection()
        cur  = conn.cursor()
        cur.execute("SELECT version();")
        version = cur.fetchone()[0]
        cur.close()
        conn.close()
        print(f"✅ Connected to PostgreSQL: {version}")
        return True
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# RUN DIRECTLY TO INITIALIZE
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("Testing connection...")
    if test_connection():
        print("Creating tables...")
        create_tables()
        print("✅ Database ready")