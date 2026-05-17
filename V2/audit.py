"""
audit.py
========
Reads raw criteria from v2_snapshots + v2_criteria in DB,
applies any scoring params JSON, and reports win rates.

Can compare multiple scoring versions side by side.
Does NOT modify any data — read only.

Usage:
    python audit.py                                      # all params in score/
    python audit.py --params score/params_v1.json        # specific params
    python audit.py --tickers AAPL MSFT JPM              # filter by ticker
    python audit.py --sector "Energy"                    # filter by sector
    python audit.py --strategy PRICE_TARGET              # filter by strategy
    python audit.py --summary                            # DB stats only
    python audit.py --simulate                           # compare all versions

Dependencies:
    db.py      -> get_all_snapshots_with_criteria()
    scoring.py -> score_raw(), load_params()
    .env       -> DATABASE_URL
"""

import os
import sys
import glob
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from db import get_connection, get_all_snapshots_with_criteria
from scoring import score_raw, load_params


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

SCORE_DIR = os.path.join(os.path.dirname(__file__), "score")


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY — DB stats without scoring
# ══════════════════════════════════════════════════════════════════════════════

def print_db_summary():
    """Show raw DB stats for v2 tables."""
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM v2_snapshots;")
    n_snapshots = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM v2_criteria;")
    n_criteria = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM v2_outcomes;")
    n_outcomes = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT ticker) FROM v2_snapshots;")
    n_tickers = cur.fetchone()[0]

    cur.execute("SELECT MIN(backtest_date), MAX(backtest_date) FROM v2_snapshots;")
    date_min, date_max = cur.fetchone()

    cur.execute("""
        SELECT strategy, COUNT(*) as outcomes,
               SUM(CASE WHEN was_successful THEN 1 ELSE 0 END) as wins
        FROM v2_outcomes
        WHERE was_successful IS NOT NULL
        GROUP BY strategy
        ORDER BY strategy;
    """)
    strategies = cur.fetchall()

    cur.execute("""
        SELECT sector, COUNT(DISTINCT ticker) as tickers, COUNT(*) as snapshots
        FROM v2_snapshots
        WHERE sector IS NOT NULL
        GROUP BY sector
        ORDER BY snapshots DESC;
    """)
    sectors = cur.fetchall()

    cur.close()
    conn.close()

    print(f"\n{'=' * 60}")
    print(f"  V2 DATABASE SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Snapshots:  {n_snapshots:,}")
    print(f"  Criteria:   {n_criteria:,}")
    print(f"  Outcomes:   {n_outcomes:,}")
    print(f"  Tickers:    {n_tickers}")
    print(f"  Period:     {date_min} -> {date_max}")

    if strategies:
        print(f"\n  OUTCOMES BY STRATEGY")
        print(f"  {'─' * 45}")
        print(f"  {'STRATEGY':<25} {'OUTCOMES':>9}  {'WINS':>6}  {'WIN RATE':>9}")
        print(f"  {'─' * 45}")
        for strategy, outcomes, wins in strategies:
            wr = wins / outcomes * 100 if outcomes > 0 else 0
            print(f"  {strategy:<25} {outcomes:>9,}  {wins:>6,}  {wr:>8.1f}%")

    if sectors:
        print(f"\n  {'SECTOR':<35} {'TICKERS':>8}  {'SNAPSHOTS':>10}")
        print(f"  {'─' * 55}")
        for sector, tickers, snapshots in sectors:
            print(f"  {sector:<35} {tickers:>8}  {snapshots:>10,}")

    print(f"{'=' * 60}\n")


# ══════════════════════════════════════════════════════════════════════════════
# LOAD DATA FROM DB
# ══════════════════════════════════════════════════════════════════════════════

def load_snapshots(tickers=None, sector=None, strategy=None):
    """
    Load all snapshots with their raw criteria from DB.
    Optionally filter by ticker list, sector, or strategy.

    Args:
        tickers  (list | None) — filter by ticker symbols
        sector   (str | None)  — filter by sector name
        strategy (str | None)  — filter outcomes by strategy name

    Returns:
        list of dicts — each has snapshot metadata + criteria dict + outcome
    """
    rows = get_all_snapshots_with_criteria(strategy=strategy)

    if tickers:
        tickers_upper = [t.upper() for t in tickers]
        rows = [r for r in rows if r["ticker"].upper() in tickers_upper]

    if sector:
        rows = [r for r in rows if r.get("sector") == sector]

    return rows


# ══════════════════════════════════════════════════════════════════════════════
# APPLY SCORING TO ALL SNAPSHOTS
# ══════════════════════════════════════════════════════════════════════════════

def apply_scoring(rows, params):
    """
    Apply scoring params to all snapshots.

    Args:
        rows   (list) — from load_snapshots()
        params (dict) — from load_params()

    Returns:
        list of dicts — each row with scoring result added
    """
    results = []
    for row in rows:
        criteria = row.get("criteria") or {}
        scored   = score_raw(criteria, params)

        results.append({
            **row,
            "score":     scored["score"],
            "score_max": scored["score_max"],
            "score_pct": scored["score_pct"],
            "verdict":   scored["verdict"],
            "breakdown": scored["breakdown"],
        })

    return results


# ══════════════════════════════════════════════════════════════════════════════
# REPORT — single params file
# ══════════════════════════════════════════════════════════════════════════════

def print_report(results, params_name, strategy_name=None):
    """Print detailed audit report for one scoring version."""

    total       = len(results)
    has_outcome = [r for r in results if r.get("was_successful") is not None]

    viable      = [r for r in results if r["verdict"] == "VIABLE"]
    caution     = [r for r in results if r["verdict"] == "CAUTION"]
    no_trade    = [r for r in results if r["verdict"] == "DO_NOT_TRADE"]

    viable_with_outcome = [r for r in viable if r.get("was_successful") is not None]

    strategy_label = f" | Strategy: {strategy_name}" if strategy_name else ""

    print(f"\n{'=' * 65}")
    print(f"  AUDIT — {params_name}{strategy_label}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 65}")

    print(f"\n  OVERVIEW")
    print(f"  {'─' * 45}")
    print(f"  Total snapshots:    {total:,}")
    print(f"  With outcomes:      {len(has_outcome):,}")
    print(f"  VIABLE:             {len(viable):,}  ({len(viable)/total*100:.1f}% of total)")
    print(f"  CAUTION:            {len(caution):,}  ({len(caution)/total*100:.1f}%)")
    print(f"  DO_NOT_TRADE:       {len(no_trade):,}  ({len(no_trade)/total*100:.1f}%)")

    # Win rate for VIABLE signals
    print(f"\n  VIABLE ACCURACY")
    print(f"  {'─' * 45}")
    if viable_with_outcome:
        wins     = [r for r in viable_with_outcome if r["was_successful"]]
        win_rate = len(wins) / len(viable_with_outcome) * 100
        avg_pct  = sum(r["pct_change"] for r in viable_with_outcome
                       if r["pct_change"] is not None) / len(viable_with_outcome)

        print(f"  Signals with outcome: {len(viable_with_outcome):,}")
        print(f"  Win rate:             {win_rate:.1f}%")
        print(f"  Avg pct change:       {avg_pct:+.2f}%")

        # Exit reasons breakdown
        reasons = {}
        for r in viable_with_outcome:
            reason = r.get("exit_reason", "UNKNOWN")
            reasons[reason] = reasons.get(reason, 0) + 1
        print(f"\n  Exit reasons:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:<20} {count:>6,}  ({count/len(viable_with_outcome)*100:.1f}%)")
    else:
        print(f"  No outcomes recorded yet — run simulate.py first")

    # Score distribution
    print(f"\n  SCORE DISTRIBUTION")
    print(f"  {'─' * 55}")
    print(f"  {'BAND':<12} {'SIGNALS':>8}  {'WITH OUT':>9}  {'WIN RATE':>10}  {'AVG RET':>9}")
    print(f"  {'─' * 55}")

    bands = {}
    for r in results:
        band = int(r["score_pct"] // 20) * 20
        if band not in bands:
            bands[band] = []
        bands[band].append(r)

    for band in sorted(bands.keys(), reverse=True):
        band_rows    = bands[band]
        band_outcome = [r for r in band_rows if r.get("was_successful") is not None]
        wr  = sum(1 for r in band_outcome if r["was_successful"]) / len(band_outcome) * 100 if band_outcome else None
        avg = sum(r["pct_change"] for r in band_outcome if r["pct_change"] is not None) / len(band_outcome) if band_outcome else None
        wr_str  = f"{wr:.1f}%"   if wr  is not None else "N/A"
        avg_str = f"{avg:+.2f}%" if avg is not None else "N/A"
        print(f"  {band:>3}-{band+19}%  {len(band_rows):>8,}  {len(band_outcome):>9,}  {wr_str:>10}  {avg_str:>9}")

    # Criterion breakdown
    print(f"\n  CRITERION BREAKDOWN (avg score contribution)")
    print(f"  {'─' * 45}")
    if results and results[0].get("breakdown"):
        criteria_totals = {}
        for r in results:
            for criterion, pts in r["breakdown"].items():
                if criterion not in criteria_totals:
                    criteria_totals[criterion] = []
                criteria_totals[criterion].append(pts)

        for criterion, values in sorted(criteria_totals.items(),
                                        key=lambda x: -sum(x[1])/len(x[1])):
            avg = sum(values) / len(values)
            bar = "+" * int(abs(avg) * 5) if avg > 0 else "-" * int(abs(avg) * 5) if avg < 0 else "."
            print(f"  {criterion:<28} avg: {avg:>+.2f}  {bar}")

    # By ticker
    print(f"\n  BY TICKER")
    print(f"  {'─' * 65}")
    print(f"  {'TICKER':<8} {'TOTAL':>7}  {'VIABLE':>7}  {'W/OUT':>6}  {'WIN RATE':>10}  {'AVG RET':>9}")
    print(f"  {'─' * 65}")

    tickers = {}
    for r in results:
        t = r["ticker"]
        if t not in tickers:
            tickers[t] = []
        tickers[t].append(r)

    ticker_rows = []
    for ticker, rows in tickers.items():
        viable_rows  = [r for r in rows if r["verdict"] == "VIABLE"]
        outcome_rows = [r for r in viable_rows if r.get("was_successful") is not None]
        wr  = sum(1 for r in outcome_rows if r["was_successful"]) / len(outcome_rows) * 100 if outcome_rows else None
        avg = sum(r["pct_change"] for r in outcome_rows if r["pct_change"] is not None) / len(outcome_rows) if outcome_rows else None
        ticker_rows.append((ticker, len(rows), len(viable_rows), len(outcome_rows), wr, avg))

    for ticker, total_n, viable_n, outcome_n, wr, avg in sorted(
            ticker_rows, key=lambda x: -(x[4] or -999)):
        wr_str  = f"{wr:.1f}%"   if wr  is not None else "N/A"
        avg_str = f"{avg:+.2f}%" if avg is not None else "N/A"
        print(f"  {ticker:<8} {total_n:>7,}  {viable_n:>7,}  {outcome_n:>6,}  {wr_str:>10}  {avg_str:>9}")

    # Monthly breakdown
    print(f"\n  MONTHLY BREAKDOWN")
    print(f"  {'─' * 65}")
    print(f"  {'MONTH':<10} {'TOTAL':>7}  {'VIABLE':>7}  {'W/OUT':>6}  {'WIN RATE':>10}  {'AVG RET':>9}")
    print(f"  {'─' * 65}")

    months = {}
    for r in results:
        month = str(r["date"])[:7]
        if month not in months:
            months[month] = []
        months[month].append(r)

    for month in sorted(months.keys()):
        month_rows   = months[month]
        viable_rows  = [r for r in month_rows if r["verdict"] == "VIABLE"]
        outcome_rows = [r for r in viable_rows if r.get("was_successful") is not None]
        wr  = sum(1 for r in outcome_rows if r["was_successful"]) / len(outcome_rows) * 100 if outcome_rows else None
        avg = sum(r["pct_change"] for r in outcome_rows if r["pct_change"] is not None) / len(outcome_rows) if outcome_rows else None
        wr_str  = f"{wr:.1f}%"   if wr  is not None else "N/A"
        avg_str = f"{avg:+.2f}%" if avg is not None else "N/A"
        print(f"  {month:<10} {len(month_rows):>7,}  {len(viable_rows):>7,}  {len(outcome_rows):>6,}  {wr_str:>10}  {avg_str:>9}")

    print(f"\n{'=' * 65}\n")


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATE — compare multiple scoring versions
# ══════════════════════════════════════════════════════════════════════════════

def simulate_all(rows, params_files, strategy_name=None):
    """Compare multiple scoring param files on the same dataset."""
    total        = len(rows)
    with_outcome = [r for r in rows if r.get("was_successful") is not None]
    strategy_label = f" | Strategy: {strategy_name}" if strategy_name else ""

    print(f"\n{'=' * 70}")
    print(f"  SCORING SIMULATION — {datetime.now().strftime('%Y-%m-%d %H:%M')}{strategy_label}")
    print(f"  Snapshots: {total:,}  |  With outcomes: {len(with_outcome):,}")
    print(f"{'=' * 70}\n")
    print(f"  {'FILE':<30} {'VIABLE':>8} {'VBL%':>6} {'WIN RATE':>10} {'AVG RET':>9}")
    print(f"  {'─' * 67}")

    for path in params_files:
        params  = load_params(path)
        results = apply_scoring(rows, params)

        viable         = [r for r in results if r["verdict"] == "VIABLE"]
        viable_outcome = [r for r in viable   if r.get("was_successful") is not None]

        win_rate = sum(1 for r in viable_outcome if r["was_successful"]) / len(viable_outcome) * 100 if viable_outcome else 0
        avg_ret  = sum(r["pct_change"] for r in viable_outcome if r["pct_change"] is not None) / len(viable_outcome) if viable_outcome else 0
        vbl_pct  = len(viable) / total * 100

        fname = os.path.basename(path)
        print(f"  {fname:<30} {len(viable):>8,} {vbl_pct:>5.1f}% {win_rate:>9.1f}% {avg_ret:>+9.2f}%")

    print(f"\n{'=' * 70}\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="V2 Audit — apply scoring to raw backtest data"
    )
    parser.add_argument("--params", nargs="+", default=None,
                        help="Scoring params files (default: all in score/)")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Filter by ticker")
    parser.add_argument("--sector", default=None,
                        help="Filter by sector")
    parser.add_argument("--strategy", default=None,
                        help="Filter outcomes by strategy name (e.g. PRICE_TARGET)")
    parser.add_argument("--summary", action="store_true",
                        help="Show DB stats only")
    parser.add_argument("--simulate", action="store_true",
                        help="Compare all scoring versions side by side")

    args = parser.parse_args()

    if args.summary:
        print_db_summary()
        sys.exit(0)

    # Find params files
    if args.params:
        params_files = args.params
    else:
        params_files = sorted(glob.glob(os.path.join(SCORE_DIR, "params_*.json")))

    if not params_files:
        print(f"  No scoring params found in {SCORE_DIR}/")
        print(f"  Create score/params_v1.json to get started.")
        sys.exit(1)

    # Load snapshots
    print(f"\n  Loading snapshots from DB...", end=" ", flush=True)
    rows = load_snapshots(
        tickers=args.tickers,
        sector=args.sector,
        strategy=args.strategy,
    )
    print(f"{len(rows):,} records loaded")

    # DEBUG — ver qué llega para candlestick_signal y sma50_direction
    sample = rows[0]["criteria"] if rows else {}
    print(f"\nDEBUG criteria sample:")
    for k in ["candlestick_signal", "sma50_direction", "rsi", "above_sma50"]:
        print(f"  {k}: {repr(sample.get(k))}")

    if not rows:
        print("  No data found. Run backtest.py first.")
        sys.exit(0)

    if args.simulate:
        simulate_all(rows, params_files, strategy_name=args.strategy)
    else:
        for path in params_files:
            params  = load_params(path)
            results = apply_scoring(rows, params)
            print_report(results, os.path.basename(path), strategy_name=args.strategy)