"""
migrate.py
==========
One-time database migration script.

Adds columns and constraints required by backtest.py and db.py.
Safe to run multiple times — all operations use IF NOT EXISTS / DO NOTHING.

Run once:
    python migrate.py

After running successfully, this script is no longer needed.
"""

from db import get_connection


def run_migrations():
    conn = get_connection()
    cur  = conn.cursor()

    migrations = [

        # ── Step 1: Remove criteria_scores of duplicates first (FK constraint) ─
        (
            "Remove duplicate analysis keeping the one linked to positions",
            """
            DELETE FROM criteria_scores
            WHERE analysis_id IN (
                SELECT id FROM analysis
                WHERE is_backtest = FALSE
                AND id NOT IN (
                    SELECT DISTINCT analysis_id FROM positions
                    WHERE analysis_id IS NOT NULL
                )
                AND id NOT IN (
                    SELECT DISTINCT ON (ticker, DATE(timestamp)) id
                    FROM analysis
                    WHERE is_backtest = FALSE
                    AND id NOT IN (
                        SELECT DISTINCT analysis_id FROM positions
                        WHERE analysis_id IS NOT NULL
                    )
                    ORDER BY ticker, DATE(timestamp), timestamp DESC
                )
            );
            """
        ),

        # ── Step 2: Remove duplicate analysis rows (no position linked) ────────
        (
            "Remove duplicate real-time analysis keeping position-linked ones",
            """
            DELETE FROM analysis
            WHERE is_backtest = FALSE
            AND id NOT IN (
                SELECT DISTINCT analysis_id FROM positions
                WHERE analysis_id IS NOT NULL
            )
            AND id NOT IN (
                SELECT DISTINCT ON (ticker, DATE(timestamp)) id
                FROM analysis
                WHERE is_backtest = FALSE
                AND id NOT IN (
                    SELECT DISTINCT analysis_id FROM positions
                    WHERE analysis_id IS NOT NULL
                )
                ORDER BY ticker, DATE(timestamp), timestamp DESC
            );
            """
        ),

        # ── Step 3: Remove specific MSFT duplicate (id=8, no position linked) ──
        (
            "Remove MSFT duplicate analysis id=8 criteria_scores",
            """
            DELETE FROM criteria_scores WHERE analysis_id = 8;
            """
        ),
        (
            "Remove MSFT duplicate analysis id=8",
            """
            DELETE FROM analysis WHERE id = 8 AND is_backtest = FALSE;
            """
        ),

        # ── Step 4: Add new columns to analysis ───────────────────────────────
        (
            "Add is_backtest column",
            "ALTER TABLE analysis ADD COLUMN IF NOT EXISTS is_backtest BOOLEAN DEFAULT FALSE;"
        ),
        (
            "Add backtest_date column",
            "ALTER TABLE analysis ADD COLUMN IF NOT EXISTS backtest_date DATE;"
        ),

        # ── Step 5: Add unique constraint for backtest records ────────────────
        (
            "Add unique_backtest_ticker_date constraint",
            """
            DO $$ BEGIN
                ALTER TABLE analysis
                ADD CONSTRAINT unique_backtest_ticker_date
                UNIQUE (ticker, backtest_date, is_backtest);
            EXCEPTION WHEN duplicate_table THEN
                RAISE NOTICE 'Constraint already exists, skipping.';
            END $$;
            """
        ),

        # ── Step 6: Add unique index for real-time analysis ───────────────────
        (
            "Add unique_real_analysis_per_day index",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS unique_real_analysis_per_day
            ON analysis (ticker, DATE(timestamp))
            WHERE is_backtest = FALSE;
            """
        ),

        # ── Step 7: Add unique constraint for outcomes ────────────────────────
        (
            "Add unique_outcome_analysis constraint",
            """
            DO $$ BEGIN
                ALTER TABLE outcomes
                ADD CONSTRAINT unique_outcome_analysis
                UNIQUE (analysis_id);
            EXCEPTION WHEN duplicate_table THEN
                RAISE NOTICE 'Constraint already exists, skipping.';
            END $$;
            """
        ),

    ]

    print("\n  Running migrations...\n")
    success = 0
    failed  = 0

    for name, sql in migrations:
        try:
            cur.execute(sql)
            conn.commit()
            print(f"  ✅ {name}")
            success += 1
        except Exception as e:
            conn.rollback()
            print(f"  ❌ {name} → {e}")
            failed += 1

    cur.close()
    conn.close()

    print(f"\n  Done: {success} succeeded, {failed} failed\n")


if __name__ == "__main__":
    run_migrations()