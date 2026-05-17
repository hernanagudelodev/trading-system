"""
scoring.py
==========
Applies a scoring parameters JSON to raw criteria values.

Receives a dict of raw numeric values (from criteria.py or from DB)
and a params dict (loaded from a JSON file).

Returns a score, score_pct, and verdict.

This module has NO knowledge of:
    - Where the data came from (backtest or live)
    - What ticker or date this is for
    - Any database operations

Usage:
    from scoring import score_raw, load_params

    params = load_params("score/params_v1.json")
    result = score_raw(raw_criteria, params)

    result = {
        "score":       9,
        "score_max":   14,
        "score_pct":   64.3,
        "verdict":     "CAUTION",
        "breakdown":   {"trend_25d_pct": 1, "rsi": 2, ...}
    }
"""

import json
import os


# ══════════════════════════════════════════════════════════════════════════════
# PARAMS LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_params(path):
    """
    Load scoring parameters from a JSON file.

    Args:
        path (str) — path to params JSON file

    Returns:
        dict — parsed params
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Scoring params not found: {path}")

    with open(path) as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════════
# INDIVIDUAL CRITERION SCORERS
# Each function receives the raw value and the criterion's params dict.
# Returns an int score.
# ══════════════════════════════════════════════════════════════════════════════

def score_trend_25d_pct(value, p):
    """
    Raw value: float — % price change over 25 days.
    Positive = bullish, negative = bearish.
    """
    if value is None:
        return 0
    if value >= p["strong_bullish_threshold"]:
        return p["score_strong_bullish"]
    elif value >= p["bullish_threshold"]:
        return p["score_bullish"]
    elif value >= p["bearish_threshold"]:
        return p["score_neutral"]
    elif value >= p["bearish_threshold"] * 2:
        return p["score_bearish"]
    else:
        return p["score_strong_bearish"]


def score_above_sma50(value, p):
    """
    Raw value: 1.0 = above, 0.0 = below.
    """
    if value is None:
        return 0
    return p["score_above"] if value == 1.0 else p["score_below"]


def score_above_sma200(value, p):
    """
    Raw value: 1.0 = above, 0.0 = below, None = insufficient data.
    """
    if value is None:
        return p["score_no_data"]
    return p["score_above"] if value == 1.0 else p["score_below"]


def score_sma50_direction(value, p):
    """
    Raw value: string stored in raw_extra — "RISING" | "FLAT" | "FALLING".
    """
    if value is None:
        return 0
    v = str(value).upper()
    if v == "RISING":
        return p["score_rising"]
    elif v == "FALLING":
        return p["score_falling"]
    return p["score_flat"]


def score_rsi(value, p):
    """
    Raw value: float RSI (0-100).
    Neutral zone (35-65) is ideal for entry.
    """
    if value is None:
        return 0
    if value >= p["overbought_threshold"]:
        return p["score_overbought"]
    elif value <= p["oversold_threshold"]:
        return p["score_oversold"]
    elif value <= p["caution_low"] or value >= p["caution_high"]:
        return p["score_caution"]
    return p["score_neutral"]


def score_week_52_position(value, p):
    """
    Raw value: float 0-100 (0 = at annual low, 100 = at annual high).
    Mid-range is ideal — not too close to high or low.
    """
    if value is None:
        return 0
    if value >= p["near_high_threshold"]:
        return p["score_near_high"]
    elif value <= p["near_low_threshold"]:
        return p["score_near_low"]
    return p["score_mid"]


def score_nearest_support_pct(value, p):
    """
    Raw value: float — % price is above nearest support.
    Closer to support = better entry (more upside potential).
    """
    if value is None:
        return p["score_no_data"]
    if value <= p["very_close_threshold"]:
        return p["score_very_close"]
    elif value <= p["close_threshold"]:
        return p["score_close"]
    return p["score_far"]


def score_nearest_resistance_pct(value, p):
    """
    Raw value: float — % price is below nearest resistance.
    Tight resistance = less room to run = worse entry.
    """
    if value is None:
        return 0
    if value <= p["tight_threshold"]:
        return p["score_tight"]
    return p["score_room"]


def score_candlestick_signal(value, p):
    """
    Raw value: string stored in raw_extra — "BULLISH" | "NEUTRAL" | "BEARISH".
    """
    if value is None:
        return 0
    v = str(value).upper()
    if v == "BULLISH":
        return p["score_bullish"]
    elif v == "BEARISH":
        return p["score_bearish"]
    return p["score_neutral"]


def score_hv_30d(value, p):
    """
    Raw value: float — HV annualized % (e.g. 25.3 = 25.3%).
    Lower HV = cheaper options = better for buying spreads.
    """
    if value is None:
        return 0
    if value <= p["low_threshold"]:
        return p["score_low"]
    elif value <= p["high_threshold"]:
        return p["score_normal"]
    return p["score_high"]


def score_volume_ratio(value, p):
    """
    Raw value: float — today's volume / 20-day avg * 100.
    100 = average. Very low volume = bad liquidity.
    """
    if value is None:
        return 0
    if value >= p["high_threshold"]:
        return p["score_high"]
    elif value <= p["low_threshold"]:
        return p["score_low"]
    return p["score_normal"]


# ══════════════════════════════════════════════════════════════════════════════
# SCORER MAP
# Maps criterion name to its scoring function.
# Adding a new criterion = add one entry here + add params to JSON.
# ══════════════════════════════════════════════════════════════════════════════

SCORERS = {
    "trend_25d_pct":          score_trend_25d_pct,
    "above_sma50":            score_above_sma50,
    "above_sma200":           score_above_sma200,
    "sma50_direction":        score_sma50_direction,
    "rsi":                    score_rsi,
    "week_52_position":       score_week_52_position,
    "nearest_support_pct":    score_nearest_support_pct,
    "nearest_resistance_pct": score_nearest_resistance_pct,
    "candlestick_signal":     score_candlestick_signal,
    "hv_30d":                 score_hv_30d,
    "volume_ratio":           score_volume_ratio,
}


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCORER
# ══════════════════════════════════════════════════════════════════════════════

def score_raw(raw, params):
    """
    Apply scoring params to a dict of raw criteria values.

    Works with raw values from criteria.py (live scanner)
    or from DB (audit.py simulation).

    Args:
        raw    (dict) — raw criteria values {criterion: value}
        params (dict) — loaded from params_vN.json

    Returns:
        dict with keys:
            score      (int)   — total score
            score_max  (int)   — maximum possible score
            score_pct  (float) — score / score_max * 100
            verdict    (str)   — "VIABLE" | "CAUTION" | "DO_NOT_TRADE"
            breakdown  (dict)  — {criterion: score} per criterion
    """
    score_max = params["score_max"]
    threshold_viable  = params["thresholds"]["viable"]
    threshold_caution = params["thresholds"]["caution"]

    total     = 0
    breakdown = {}

    for criterion, scorer_fn in SCORERS.items():
        # Skip criteria not in this params file
        if criterion not in params:
            continue

        value = raw.get(criterion)
        pts   = scorer_fn(value, params[criterion])

        total           += pts
        breakdown[criterion] = pts

    score_pct = round(total / score_max * 100, 1) if score_max > 0 else 0.0

    if score_pct / 100 >= threshold_viable:
        verdict = "VIABLE"
    elif score_pct / 100 >= threshold_caution:
        verdict = "CAUTION"
    else:
        verdict = "DO_NOT_TRADE"

    return {
        "score":     total,
        "score_max": score_max,
        "score_pct": score_pct,
        "verdict":   verdict,
        "breakdown": breakdown,
    }


# ══════════════════════════════════════════════════════════════════════════════
# QUICK TEST
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import os

    params_path = os.path.join(os.path.dirname(__file__), "score", "params_v1.json")
    params = load_params(params_path)

    # Example raw values similar to what backtest saves
    test_raw = {
        "trend_25d_pct":          3.2,
        "above_sma50":            1.0,
        "above_sma200":           1.0,
        "sma50_direction":        "RISING",
        "rsi":                    54.9,
        "week_52_position":       67.8,
        "nearest_support_pct":    1.8,
        "nearest_resistance_pct": 4.6,
        "candlestick_signal":     "BULLISH",
        "hv_30d":                 19.0,
        "volume_ratio":           116.0,
    }

    result = score_raw(test_raw, params)

    print(f"\nTest scoring result:")
    print(f"  Score:    {result['score']}/{result['score_max']} ({result['score_pct']}%)")
    print(f"  Verdict:  {result['verdict']}")
    print(f"\n  Breakdown:")
    for criterion, pts in result["breakdown"].items():
        bar = "+" * pts if pts > 0 else "-" * abs(pts) if pts < 0 else "."
        print(f"    {criterion:<28} {pts:>+2}  {bar}")