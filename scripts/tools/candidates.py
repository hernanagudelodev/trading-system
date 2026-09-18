"""
scripts/tools/candidates.py  (v2-bidi)
======================================
Candidatas del último scan: reparto por status, balance alcistas/bajistas de las que
pasaron, y el top por fuerza de señal (el orden en que el opener las evaluaría).
Rápido (solo lee la DB). Reutiliza el ranking del opener.

    python tools/candidates.py            # top 10
    python tools/candidates.py --n 20     # top 20
"""
import os
import sys
import argparse
from pathlib import Path

from dotenv import load_dotenv

_THIS_DIR  = Path(__file__).resolve().parent
_SCRIPTS   = _THIS_DIR.parent
_REPO_ROOT = _SCRIPTS.parent
sys.path.insert(0, str(_SCRIPTS))

_ENV_NAME  = os.getenv("ENV_FILE", ".env.v2")
_ENV_PATH  = Path(_ENV_NAME)
if not _ENV_PATH.is_absolute():
    _ENV_PATH = _REPO_ROOT / _ENV_PATH
load_dotenv(_ENV_PATH)


def _conn():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def _table_exists(cur, table):
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cur.fetchone()[0] is not None


def render_candidates(n=10):
    conn = _conn(); cur = conn.cursor()
    if not _table_exists(cur, "selection_result"):
        cur.close(); conn.close()
        return "🎯 sin selección todavía (selection_result no existe)"

    # Reparto por status del último scan
    cur.execute("""
        WITH latest AS (SELECT MAX(scan_at) AS m FROM selection_result)
        SELECT status, COUNT(*) FROM selection_result
        WHERE scan_at = (SELECT m FROM latest)
        GROUP BY status
    """)
    status_counts = dict(cur.fetchall())
    total = sum(status_counts.values())
    n_cand = status_counts.get("candidate", 0)

    if total == 0:
        cur.close(); conn.close()
        return "🎯 sin candidatas en el último scan"

    # Balance de las candidatas + top por señal (rs_vs_sector en la dirección propia)
    cur.execute("""
        WITH latest AS (SELECT MAX(scan_at) AS m FROM selection_result)
        SELECT s.ticker, s.direction, s.strategy, f.value_num
        FROM selection_result s
        JOIN study_fact f ON f.study_id = s.study_id AND f.criterion = 'rs_vs_sector'
        WHERE s.status = 'candidate' AND s.scan_at = (SELECT m FROM latest)
    """)
    cands = []
    n_up = n_down = 0
    for tk, direction, strat, rs in cur.fetchall():
        rs = float(rs) if rs is not None else 0.0
        strength = rs if direction == "UPTREND" else -rs
        cands.append((strength, tk, direction, strat))
        if direction == "UPTREND":
            n_up += 1
        else:
            n_down += 1
    cur.close(); conn.close()
    cands.sort(reverse=True)

    lines = [
        f"🎯 Candidatas del último scan: {n_cand} de {total}",
        f"   {n_up} alcistas / {n_down} bajistas",
        "",
        "reparto:",
    ]
    order = ["candidate", "blocked_counter_trend", "no_direction",
             "macro_blocked", "earnings_blocked", "not_operable"]
    for st in order:
        if st in status_counts:
            lines.append(f"   {st:<22} {status_counts[st]}")
    lines.append("")
    lines.append(f"top {min(n, len(cands))} por señal:")
    for strength, tk, direction, strat in cands[:n]:
        arrow = "↑" if direction == "UPTREND" else "↓"
        lines.append(f"   {tk:<6} {arrow} {strat:<18} rs={strength:+.1f}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Candidatas del último scan (reparto + top por señal)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_const", const="paper", dest="book")
    g.add_argument("--live",  action="store_const", const="live",  dest="book")
    p.add_argument("--n", type=int, default=10, help="cuántas del top mostrar (default 10)")
    p.set_defaults(book="paper")
    a = p.parse_args()

    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1
    print(render_candidates(a.n))
    return 0


if __name__ == "__main__":
    sys.exit(main())