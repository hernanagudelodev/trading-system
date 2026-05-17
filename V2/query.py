"""
query.py
========
Corre queries directamente contra la DB de Railway y muestra
el resultado completo sin paginación.

Uso:
    python query.py                    → corre la query hardcodeada abajo
    python query.py "SELECT ..."       → corre una query custom
"""

import sys
import os
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# ══════════════════════════════════════════════════════════════════════════════
# QUERY — cambia esto o pasa una query como argumento
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_QUERY = """
SELECT 
    ROUND(score_pct/5)*5 as score_band,
    COUNT(*) as total,
    ROUND(AVG(CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END)*100,1) as win_rate,
    ROUND(AVG(o.pct_change_30d)::numeric, 2) as avg_return
FROM analysis a
JOIN outcomes o ON o.analysis_id = a.id
WHERE a.is_backtest = TRUE
GROUP BY score_band
ORDER BY score_band DESC;
"""

# ══════════════════════════════════════════════════════════════════════════════

def run_query(sql):
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor()
    cur.execute(sql)

    cols = [desc[0] for desc in cur.description]
    rows = cur.fetchall()

    cur.close()
    conn.close()
    return cols, rows


def print_results(cols, rows):
    if not rows:
        print("  No results.")
        return

    # Calculate column widths
    widths = [len(c) for c in cols]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val) if val is not None else "NULL"))

    # Header
    header = "  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    separator = "  " + "  ".join("─" * w for w in widths)
    print(header)
    print(separator)

    # Rows
    for row in rows:
        line = "  " + "  ".join(
            (str(val) if val is not None else "NULL").ljust(widths[i])
            for i, val in enumerate(row)
        )
        print(line)

    print(f"\n  {len(rows)} rows returned.")


if __name__ == "__main__":
    sql = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_QUERY
    sql = sql.strip()

    print(f"\n{'═' * 65}")
    print(f"  QUERY")
    print(f"{'═' * 65}")
    print(f"  {sql[:200]}{'...' if len(sql) > 200 else ''}")
    print(f"{'═' * 65}\n")

    cols, rows = run_query(sql)
    print_results(cols, rows)
    print()