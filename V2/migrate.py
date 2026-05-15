"""
migrate.py
==========
Creates v2_ tables in the existing Railway PostgreSQL database.
Does NOT touch any existing tables (analysis, criteria_scores, positions, outcomes).
Safe to run multiple times — uses IF NOT EXISTS.

Run once:
    python migrate.py

Tables created:
    v2_snapshots   → one record per ticker per day (raw price + metadata)
    v2_criteria    → one record per criterion per snapshot (raw numeric value)
    v2_outcomes    → one record per snapshot (what happened in next 30 days)
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def run_migrations():
    conn = get_connection()
    cur  = conn.cursor()

    migrations = [

        # ── v2_snapshots ──────────────────────────────────────────────────────
        # One record per ticker per backtest day.
        # Stores raw price and metadata only — no score, no verdict.
        (
            "Create v2_snapshots table",
            """
            CREATE TABLE IF NOT EXISTS v2_snapshots (
                id              SERIAL PRIMARY KEY,
                ticker          VARCHAR(10)     NOT NULL,
                backtest_date   DATE            NOT NULL,
                price           DECIMAL(10,2)   NOT NULL,
                sector          VARCHAR(50),
                created_at      TIMESTAMP       DEFAULT NOW(),
                UNIQUE (ticker, backtest_date)
            );
            """
        ),

        # ── v2_criteria ───────────────────────────────────────────────────────
        # One record per criterion per snapshot.
        # raw_value stores the pure numeric value (float).
        # raw_extra stores any additional context as JSON
        # (e.g. for candlestick: {"pattern": "HAMMER", "signal": "BULLISH"}).
        (
            "Create v2_criteria table",
            """
            CREATE TABLE IF NOT EXISTS v2_criteria (
                id              SERIAL PRIMARY KEY,
                snapshot_id     INTEGER         NOT NULL REFERENCES v2_snapshots(id) ON DELETE CASCADE,
                criterion       VARCHAR(50)     NOT NULL,
                raw_value       DECIMAL(12,4),
                raw_extra       JSONB,
                UNIQUE (snapshot_id, criterion)
            );
            """
        ),

        # ── v2_outcomes ───────────────────────────────────────────────────────
        # One record per snapshot.
        # Records what actually happened in the 30 days after the snapshot.
        # Populated by simulate.py — NOT by backtest.py.
        # exit_reason: TARGET_REACHED | STOP_LOSS | EXPIRED | PENDING
        (
            "Create v2_outcomes table",
            """
            CREATE TABLE IF NOT EXISTS v2_outcomes (
                id                  SERIAL PRIMARY KEY,
                snapshot_id         INTEGER         NOT NULL REFERENCES v2_snapshots(id) ON DELETE CASCADE,
                exit_day            INTEGER,
                exit_price          DECIMAL(10,2),
                pct_change          DECIMAL(10,4),
                was_successful      BOOLEAN,
                exit_reason         VARCHAR(30),
                recorded_at         TIMESTAMP       DEFAULT NOW(),
                UNIQUE (snapshot_id)
            );
            """
        ),

        # ── Indexes for common queries ────────────────────────────────────────
        (
            "Create index on v2_snapshots(ticker)",
            """
            CREATE INDEX IF NOT EXISTS idx_v2_snapshots_ticker
            ON v2_snapshots(ticker);
            """
        ),
        (
            "Create index on v2_snapshots(backtest_date)",
            """
            CREATE INDEX IF NOT EXISTS idx_v2_snapshots_date
            ON v2_snapshots(backtest_date);
            """
        ),
        (
            "Create index on v2_criteria(snapshot_id)",
            """
            CREATE INDEX IF NOT EXISTS idx_v2_criteria_snapshot
            ON v2_criteria(snapshot_id);
            """
        ),
        (
            "Create index on v2_criteria(criterion)",
            """
            CREATE INDEX IF NOT EXISTS idx_v2_criteria_criterion
            ON v2_criteria(criterion);
            """
        ),

    ]

    print("\n  Running v2 migrations...\n")
    success = 0
    failed  = 0

    for name, sql in migrations:
        try:
            cur.execute(sql)
            conn.commit()
            print(f"  OK  {name}")
            success += 1
        except Exception as e:
            conn.rollback()
            print(f"  ERR {name} -> {e}")
            failed += 1

    cur.close()
    conn.close()

    print(f"\n  Done: {success} succeeded, {failed} failed\n")

    if failed == 0:
        print("  v2 tables ready:")
        print("    v2_snapshots  — ticker + date + price")
        print("    v2_criteria   — raw numeric values per criterion")
        print("    v2_outcomes   — what happened after (populated by simulate.py)")
        print()


if __name__ == "__main__":
    run_migrations()