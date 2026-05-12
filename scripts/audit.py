"""
audit.py
========
Backtest results analysis for Bull Call Spread system.

Reads backtest data from PostgreSQL and generates a raw metrics report.
No AI calls — bring the report to Claude for interpretation and
scoring.py adjustment recommendations.

Usage:
    python audit.py                    → full report
    python audit.py --ticker AAPL      → single ticker report
    python audit.py --verdict VIABLE   → filter by verdict
    python audit.py --simulate         → compare all scoring_params_v*.json files

Dependencies:
    db.py               → PostgreSQL read
    score/scoring_params_v*.json → parameter files to simulate
    .env                → DATABASE_URL
"""

import argparse
import glob
import json
from datetime import datetime
from pathlib import Path
from db import get_connection


# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_backtest_summary(ticker=None, verdict=None):
    """Fetch overall backtest summary statistics."""
    conn = get_connection()
    cur  = conn.cursor()

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
        SELECT
            COUNT(*)                                                            as total,
            SUM(CASE WHEN a.verdict = 'VIABLE'       THEN 1 ELSE 0 END)        as viable,
            SUM(CASE WHEN a.verdict = 'CAUTION'      THEN 1 ELSE 0 END)        as caution,
            SUM(CASE WHEN a.verdict = 'DO_NOT_TRADE' THEN 1 ELSE 0 END)        as do_not_trade,
            MIN(a.backtest_date)                                                as date_from,
            MAX(a.backtest_date)                                                as date_to,
            COUNT(DISTINCT a.ticker)                                            as tickers,
            AVG(a.score_pct)                                                    as avg_score_pct
        FROM analysis a
        WHERE {where};
    """, params)

    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def fetch_verdict_accuracy(ticker=None):
    """
    For each verdict, calculate what % of signals resulted in
    price increase 30 days later.
    """
    conn = get_connection()
    cur  = conn.cursor()

    conditions = ["a.is_backtest = TRUE", "o.would_have_profited IS NOT NULL"]
    params     = []

    if ticker:
        conditions.append("a.ticker = %s")
        params.append(ticker)

    where = " AND ".join(conditions)

    cur.execute(f"""
        SELECT
            a.verdict,
            COUNT(*)                                                            as total,
            SUM(CASE WHEN o.would_have_profited THEN 1 ELSE 0 END)             as profitable,
            ROUND(AVG(CASE WHEN o.would_have_profited
                THEN 1.0 ELSE 0.0 END) * 100, 1)                               as win_rate_pct,
            ROUND(AVG(o.pct_change_30d)::numeric, 2)                           as avg_return_pct,
            ROUND(MIN(o.pct_change_30d)::numeric, 2)                           as min_return_pct,
            ROUND(MAX(o.pct_change_30d)::numeric, 2)                           as max_return_pct
        FROM analysis a
        JOIN outcomes o ON o.analysis_id = a.id
        WHERE {where}
        GROUP BY a.verdict
        ORDER BY win_rate_pct DESC;
    """, params)

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def fetch_criterion_analysis(ticker=None):
    """
    For each criterion, calculate predictive value:
    win rate when bullish vs bearish signal.
    """
    conn = get_connection()
    cur  = conn.cursor()

    conditions = ["a.is_backtest = TRUE", "o.would_have_profited IS NOT NULL"]
    params     = []

    if ticker:
        conditions.append("a.ticker = %s")
        params.append(ticker)

    where = " AND ".join(conditions)

    cur.execute(f"""
        SELECT
            cs.criterion,
            COUNT(*)                                                            as total,
            ROUND(AVG(cs.score)::numeric, 2)                                   as avg_score,

            SUM(CASE WHEN cs.score > 0 THEN 1 ELSE 0 END)                      as bullish_signals,
            ROUND(AVG(CASE WHEN cs.score > 0
                THEN CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END
                END) * 100, 1)                                                  as bullish_win_rate,

            SUM(CASE WHEN cs.score < 0 THEN 1 ELSE 0 END)                      as bearish_signals,
            ROUND(AVG(CASE WHEN cs.score < 0
                THEN CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END
                END) * 100, 1)                                                  as bearish_win_rate,

            SUM(CASE WHEN cs.score = 0 THEN 1 ELSE 0 END)                      as neutral_signals,
            ROUND(AVG(CASE WHEN cs.score = 0
                THEN CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END
                END) * 100, 1)                                                  as neutral_win_rate,

            ROUND(AVG(CASE WHEN o.would_have_profited
                THEN 1.0 ELSE 0.0 END) * 100, 1)                               as overall_win_rate,

            ROUND(AVG(CASE WHEN cs.score > 0
                THEN o.pct_change_30d END)::numeric, 2)                        as bullish_avg_return,
            ROUND(AVG(CASE WHEN cs.score < 0
                THEN o.pct_change_30d END)::numeric, 2)                        as bearish_avg_return

        FROM criteria_scores cs
        JOIN analysis a ON a.id = cs.analysis_id
        JOIN outcomes o ON o.analysis_id = a.id
        WHERE {where}
        GROUP BY cs.criterion
        ORDER BY
            (COALESCE(AVG(CASE WHEN cs.score > 0
                THEN CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END
                END) * 100, 0) -
             COALESCE(AVG(CASE WHEN cs.score < 0
                THEN CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END
                END) * 100, 50)) DESC;
    """, params)

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def fetch_ticker_breakdown(verdict=None):
    """Per-ticker accuracy breakdown."""
    conn = get_connection()
    cur  = conn.cursor()

    conditions = ["a.is_backtest = TRUE", "o.would_have_profited IS NOT NULL"]
    params     = []

    if verdict:
        conditions.append("a.verdict = %s")
        params.append(verdict)

    where = " AND ".join(conditions)

    cur.execute(f"""
        SELECT
            a.ticker,
            COUNT(*)                                                            as total,
            SUM(CASE WHEN a.verdict = 'VIABLE' THEN 1 ELSE 0 END)              as viable,
            ROUND(AVG(CASE WHEN a.verdict = 'VIABLE'
                THEN CASE WHEN o.would_have_profited THEN 1.0 ELSE 0.0 END
                END) * 100, 1)                                                  as viable_accuracy,
            ROUND(AVG(o.pct_change_30d)::numeric, 2)                           as avg_return_pct,
            ROUND(AVG(a.score_pct)::numeric, 1)                                as avg_score_pct
        FROM analysis a
        JOIN outcomes o ON o.analysis_id = a.id
        WHERE {where}
        GROUP BY a.ticker
        ORDER BY viable_accuracy DESC NULLS LAST;
    """, params)

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def fetch_score_threshold_analysis():
    """Analyze win rate at different score thresholds."""
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT
            CASE
                WHEN a.score_pct >= 80 THEN '80-100%'
                WHEN a.score_pct >= 70 THEN '70-80%'
                WHEN a.score_pct >= 60 THEN '60-70%'
                WHEN a.score_pct >= 50 THEN '50-60%'
                WHEN a.score_pct >= 40 THEN '40-50%'
                WHEN a.score_pct >= 30 THEN '30-40%'
                ELSE 'Below 30%'
            END                                                                 as score_band,
            COUNT(*)                                                            as total,
            ROUND(AVG(CASE WHEN o.would_have_profited
                THEN 1.0 ELSE 0.0 END) * 100, 1)                               as win_rate_pct,
            ROUND(AVG(o.pct_change_30d)::numeric, 2)                           as avg_return_pct
        FROM analysis a
        JOIN outcomes o ON o.analysis_id = a.id
        WHERE a.is_backtest = TRUE
          AND o.would_have_profited IS NOT NULL
        GROUP BY score_band
        ORDER BY MIN(a.score_pct) DESC;
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def fetch_monthly_breakdown():
    """Win rate by month."""
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT
            TO_CHAR(a.backtest_date, 'YYYY-MM')                                as month,
            COUNT(*)                                                            as total,
            SUM(CASE WHEN a.verdict = 'VIABLE' THEN 1 ELSE 0 END)              as viable,
            ROUND(AVG(CASE WHEN o.would_have_profited
                THEN 1.0 ELSE 0.0 END) * 100, 1)                               as win_rate_pct,
            ROUND(AVG(o.pct_change_30d)::numeric, 2)                           as avg_return_pct
        FROM analysis a
        JOIN outcomes o ON o.analysis_id = a.id
        WHERE a.is_backtest = TRUE
          AND o.would_have_profited IS NOT NULL
        GROUP BY month
        ORDER BY month ASC;
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION — compare multiple scoring param files
# ══════════════════════════════════════════════════════════════════════════════

def simulate_scoring_files(param_files):
    """
    Apply scoring parameters from multiple JSON files to existing
    backtest data and compare results side by side.

    Does NOT re-run the backtest — uses stored criteria_scores from DB
    and re-applies different weights to project outcomes.
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT
            a.id,
            a.verdict                           as old_verdict,
            o.would_have_profited,
            o.pct_change_30d,
            json_object_agg(cs.criterion, cs.score) as scores
        FROM analysis a
        JOIN criteria_scores cs ON cs.analysis_id = a.id
        LEFT JOIN outcomes o ON o.analysis_id = a.id
        WHERE a.is_backtest = TRUE
        GROUP BY a.id, a.verdict,
                 o.would_have_profited, o.pct_change_30d;
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    total = len(rows)
    print(f"\n  Processing {total:,} records across {len(param_files)} param files...\n")
    print(f"  {'FILE':<30} {'VIABLE':>8} {'VBL%':>6} {'WIN_RATE':>9} {'AVG_RET':>9}")
    print(f"  {'─' * 66}")

    for param_file in param_files:
        with open(param_file) as f:
            p = json.load(f)

        threshold = p["thresholds"]["viable"]
        score_max = p["score_max"]

        viable_total = 0
        viable_wins  = []

        for row in rows:
            _, old_verdict, profited, pct_change, scores = row
            scores = scores or {}

            new_score = 0

            # trend_25d
            s = scores.get("trend_25d", 0)
            new_score += p["trend_25d"]["bullish"] if s > 0 else \
                         p["trend_25d"]["bearish"] if s < 0 else 0

            # moving_averages
            s = scores.get("moving_averages", 0)
            if s >= 2:   new_score += p["moving_averages"]["above_both"]
            elif s == 1: new_score += p["moving_averages"]["above_sma50"]
            elif s < 0:  new_score += p["moving_averages"]["below_both"]

            # sma50_direction
            s = scores.get("sma50_direction", 0)
            if s > 0:   new_score += p["sma50_direction"]["rising"]
            elif s < 0: new_score += p["sma50_direction"]["falling"]
            else:       new_score += p["sma50_direction"]["flat"]

            # rsi
            s = scores.get("rsi", 0)
            if s == 2:    new_score += p["rsi"]["score_neutral"]
            elif s == 1:  new_score += p["rsi"]["score_caution"]
            elif s == -1: new_score += p["rsi"]["score_extreme"]

            # hv
            s = scores.get("hv", 0)
            if s == 2:    new_score += p["hv"]["score_low"]
            elif s == 1:  new_score += p["hv"]["score_normal"]
            elif s == -1: new_score += p["hv"]["score_high"]

            # iv_vs_hv
            s = scores.get("iv_vs_hv", 0)
            if s == 2:    new_score += p["iv_vs_hv"]["score_cheap"]
            elif s == 1:  new_score += p["iv_vs_hv"]["score_normal"]
            elif s == -1: new_score += p["iv_vs_hv"]["score_expensive"]

            # iv_percentile
            s = scores.get("iv_percentile", 0)
            if s == 2:    new_score += p["iv_percentile"]["score_cheap"]
            elif s == 1:  new_score += p["iv_percentile"]["score_normal_low"]
            elif s == 0:  new_score += p["iv_percentile"]["score_normal_high"]
            elif s == -1: new_score += p["iv_percentile"]["score_expensive"]

            # beta
            s = scores.get("beta", 0)
            if s == 1:    new_score += p["beta"]["score_normal"]
            elif s == -1: new_score += p["beta"]["score_high"]
            else:         new_score += p["beta"]["score_low"]

            # put_call_ratio
            s = scores.get("put_call_ratio", 0)
            if s == 1:    new_score += p["put_call_ratio"]["score_fear"]
            elif s == -1: new_score += p["put_call_ratio"]["score_euphoria"]
            elif s == 0:  new_score += p["put_call_ratio"]["score_neutral"]
            else:         new_score += p["put_call_ratio"]["score_optimism"]

            # open_interest
            s = scores.get("open_interest", 0)
            if s == 1:    new_score += p["open_interest"]["score_high"]
            elif s == -1: new_score += p["open_interest"]["score_low"]
            else:         new_score += p["open_interest"]["score_normal"]

            # week_52
            s = scores.get("week_52", 0)
            if s == 1:    new_score += p["week_52"]["score_near_low"]
            elif s == -1: new_score += p["week_52"]["score_near_high"]
            else:         new_score += p["week_52"]["score_mid"]

            # support_resistance
            s = scores.get("support_resistance", 0)
            if s == 2:    new_score += p["support_resistance"]["score_near_support"]
            elif s == 1:  new_score += p["support_resistance"]["score_middle"]
            elif s == -1: new_score += p["support_resistance"]["score_near_resistance"]
            else:         new_score += p["support_resistance"]["score_no_data"]

            # candlestick
            s = scores.get("candlestick", 0)
            if s == 2:    new_score += p["candlestick"]["score_strong_bullish"]
            elif s == 1:  new_score += p["candlestick"]["score_weak_bullish"]
            elif s == 0:  new_score += p["candlestick"]["score_neutral"]
            elif s == -1: new_score += p["candlestick"]["score_weak_bearish"]
            elif s == -2: new_score += p["candlestick"]["score_strong_bearish"]

            # earnings
            s = scores.get("earnings", 0)
            if s == 1:    new_score += p["earnings"]["score_safe"]
            elif s == 0:  new_score += p["earnings"]["score_caution"]
            elif s == -2: new_score += p["earnings"]["score_danger"]

            # volume
            s = scores.get("volume", 0)
            if s == 1:    new_score += p["volume"]["score_normal"]
            elif s == 0:  new_score += p["volume"]["score_high"]
            elif s == -1: new_score += p["volume"]["score_low"]

            # pe
            s = scores.get("pe", 0)
            if s == 2:    new_score += p["pe"]["score_cheap"]
            elif s == 1:  new_score += p["pe"]["score_normal"]
            elif s == 0:  new_score += p["pe"]["score_expensive"]
            elif s == -1: new_score += p["pe"]["score_very_expensive"]

            # eps_growth
            s = scores.get("eps_growth", 0)
            if s == 2:    new_score += p["eps_growth"]["score_strong"]
            elif s == 1:  new_score += p["eps_growth"]["score_stable"]
            elif s == -1: new_score += p["eps_growth"]["score_declining"]
            elif s == -2: new_score += p["eps_growth"]["score_deteriorating"]

            # debt_equity
            s = scores.get("debt_equity", 0)
            if s == 1:    new_score += p["debt_equity"]["score_low"]
            elif s == 0:  new_score += p["debt_equity"]["score_moderate"]
            elif s == -1: new_score += p["debt_equity"]["score_high"]

            # profit_margin
            s = scores.get("profit_margin", 0)
            if s == 2:    new_score += p["profit_margin"]["score_high"]
            elif s == 1:  new_score += p["profit_margin"]["score_normal"]
            elif s == 0:  new_score += p["profit_margin"]["score_low"]
            elif s == -2: new_score += p["profit_margin"]["score_negative"]

            # Verdict
            pct = new_score / score_max if score_max > 0 else 0
            if pct >= threshold:
                viable_total += 1
                if profited is not None:
                    viable_wins.append((profited, pct_change or 0))

        win_rate   = sum(1 for w, _ in viable_wins if w) / len(viable_wins) * 100 \
                     if viable_wins else 0
        avg_ret    = sum(r for _, r in viable_wins) / len(viable_wins) \
                     if viable_wins else 0
        viable_pct = viable_total / total * 100

        fname = Path(param_file).name
        print(f"  {fname:<30} {viable_total:>8,} {viable_pct:>5.1f}% "
              f"{win_rate:>8.1f}% {avg_ret:>+9.2f}%")

    print(f"\n{'═' * 70}\n")


# ══════════════════════════════════════════════════════════════════════════════
# REPORT PRINTING
# ══════════════════════════════════════════════════════════════════════════════

def print_report(ticker=None, verdict=None):
    """Print full audit report to console."""
    timestamp  = datetime.now().strftime("%Y-%m-%d %H:%M")
    filter_str = ""
    if ticker:
        filter_str += f" | Ticker: {ticker}"
    if verdict:
        filter_str += f" | Verdict: {verdict}"

    print(f"\n{'═' * 70}")
    print(f"  AUDIT REPORT — {timestamp}{filter_str}")
    print(f"{'═' * 70}")

    # ── Overview ──────────────────────────────────────────────────────────────
    summary = fetch_backtest_summary(ticker, verdict)
    if not summary or summary[0] == 0:
        print("\n  No backtest data found.")
        print("  Run: python backtest.py\n")
        return

    total, viable, caution, dnt, date_from, date_to, tickers, avg_score = summary

    print(f"\n  OVERVIEW")
    print(f"  {'─' * 40}")
    print(f"  Period:          {date_from} → {date_to}")
    print(f"  Tickers:         {tickers}")
    print(f"  Total signals:   {total:,}")
    print(f"  VIABLE:          {viable:,}  ({viable/total*100:.1f}%)")
    print(f"  CAUTION:         {caution:,}  ({caution/total*100:.1f}%)")
    print(f"  DO NOT TRADE:    {dnt:,}  ({dnt/total*100:.1f}%)")
    print(f"  Avg score:       {avg_score:.1f}%")

    # ── Verdict accuracy ──────────────────────────────────────────────────────
    print(f"\n  VERDICT ACCURACY (30-day outcome)")
    print(f"  {'─' * 66}")
    print(f"  {'VERDICT':<16} {'SIGNALS':>8} {'PROFITABLE':>11} "
          f"{'WIN RATE':>9} {'AVG RET':>8} {'MIN':>7} {'MAX':>7}")
    print(f"  {'─' * 66}")

    accuracy = fetch_verdict_accuracy(ticker)
    if accuracy:
        for row in accuracy:
            v, total_v, profitable, win_rate, avg_ret, min_ret, max_ret = row
            print(f"  {v:<16} {total_v:>8,} {profitable:>11,} "
                  f"{win_rate:>8.1f}% {avg_ret:>+8.2f}% "
                  f"{min_ret:>+7.2f}% {max_ret:>+7.2f}%")
    else:
        print("  No outcome data yet")

    # ── Score threshold analysis ──────────────────────────────────────────────
    print(f"\n  SCORE THRESHOLD ANALYSIS")
    print(f"  {'─' * 50}")
    print(f"  {'SCORE BAND':<12} {'SIGNALS':>8} {'WIN RATE':>9} {'AVG RETURN':>11}")
    print(f"  {'─' * 50}")

    thresholds = fetch_score_threshold_analysis()
    if thresholds:
        for row in thresholds:
            band, total_t, win_rate, avg_ret = row
            marker = " ← current VIABLE threshold" if band == "70-80%" else ""
            print(f"  {band:<12} {total_t:>8,} {win_rate:>8.1f}% "
                  f"{avg_ret:>+10.2f}%{marker}")
    else:
        print("  No outcome data yet")

    # ── Criterion analysis ────────────────────────────────────────────────────
    print(f"\n  CRITERION ANALYSIS")
    print(f"  {'─' * 78}")
    print(f"  {'CRITERION':<25} {'AVG':>5} {'BULL_N':>7} {'BULL_WIN':>9} "
          f"{'BEAR_N':>7} {'BEAR_WIN':>9} {'DIFF':>7}")
    print(f"  {'─' * 78}")

    criteria = fetch_criterion_analysis(ticker)
    if criteria:
        for row in criteria:
            (criterion, total_c, avg_score_c, bull_n, bull_win,
             bear_n, bear_win, neut_n, neut_win, overall_win,
             bull_ret, bear_ret) = row

            diff         = (bull_win or 0) - (bear_win or 50)
            bull_win_str = f"{bull_win:.1f}%" if bull_win is not None else "  N/A"
            bear_win_str = f"{bear_win:.1f}%" if bear_win is not None else "  N/A"
            diff_str     = f"{diff:+.1f}%" if bull_win and bear_win else "  N/A"

            flag = ""
            if bull_win and bear_win:
                if abs(diff) < 5:
                    flag = " ← LOW PREDICTIVE VALUE"
                elif diff > 15:
                    flag = " ← STRONG SIGNAL"

            print(f"  {criterion:<25} {avg_score_c:>+5.2f} "
                  f"{bull_n or 0:>7,} {bull_win_str:>9} "
                  f"{bear_n or 0:>7,} {bear_win_str:>9} "
                  f"{diff_str:>7}{flag}")
    else:
        print("  No outcome data yet")

    # ── Ticker breakdown ──────────────────────────────────────────────────────
    print(f"\n  TICKER BREAKDOWN")
    print(f"  {'─' * 60}")
    print(f"  {'TICKER':<8} {'TOTAL':>7} {'VIABLE':>7} "
          f"{'VBL ACC':>8} {'AVG RET':>8} {'AVG SCORE':>10}")
    print(f"  {'─' * 60}")

    tickers_data = fetch_ticker_breakdown(verdict)
    if tickers_data:
        for row in tickers_data:
            ticker_name, total_t, viable_t, viable_acc, avg_ret, avg_score_t = row
            viable_acc_str = f"{viable_acc:.1f}%" if viable_acc is not None else "N/A"
            avg_ret_str    = f"{avg_ret:+.2f}%" if avg_ret is not None else "N/A"
            print(f"  {ticker_name:<8} {total_t:>7,} {viable_t:>7,} "
                  f"{viable_acc_str:>8} {avg_ret_str:>8} {avg_score_t:>9.1f}%")
    else:
        print("  No data")

    # ── Monthly breakdown ─────────────────────────────────────────────────────
    print(f"\n  MONTHLY BREAKDOWN")
    print(f"  {'─' * 55}")
    print(f"  {'MONTH':<10} {'TOTAL':>7} {'VIABLE':>7} {'WIN RATE':>9} {'AVG RET':>8}")
    print(f"  {'─' * 55}")

    monthly = fetch_monthly_breakdown()
    if monthly:
        for row in monthly:
            month, total_m, viable_m, win_rate, avg_ret = row
            win_str = f"{win_rate:.1f}%" if win_rate is not None else "N/A"
            ret_str = f"{avg_ret:+.2f}%" if avg_ret is not None else "N/A"
            print(f"  {month:<10} {total_m:>7,} {viable_m:>7,} "
                  f"{win_str:>9} {ret_str:>8}")
    else:
        print("  No data")

    print(f"\n  {'═' * 70}")
    print(f"  COPY THIS REPORT AND BRING IT TO CLAUDE FOR INTERPRETATION")
    print(f"  Claude will suggest specific changes to scoring_params JSON files")
    print(f"  {'═' * 70}\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Audit backtest results and generate metrics report"
    )
    parser.add_argument(
        "--ticker",
        type=str,
        default=None,
        help="Filter by ticker (default: all)"
    )
    parser.add_argument(
        "--verdict",
        type=str,
        default=None,
        choices=["VIABLE", "CAUTION", "DO_NOT_TRADE"],
        help="Filter by verdict"
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Compare all scoring_params_v*.json files in score/ folder"
    )
    args = parser.parse_args()

    if args.simulate:
        files = sorted(glob.glob("score/scoring_params_v*.json"))
        if not files:
            print("\n  No scoring_params_v*.json files found in score/ folder.")
            print("  Make sure JSON files are in scripts/score/\n")
        else:
            print(f"\n{'═' * 70}")
            print(f"  SCORING SIMULATION — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            print(f"  Files found: {len(files)}")
            print(f"{'═' * 70}")
            simulate_scoring_files(files)
    else:
        print_report(ticker=args.ticker, verdict=args.verdict)