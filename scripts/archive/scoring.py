"""
scoring.py
==========
Scoring and qualification module for options trading system.

This file receives raw criteria data from criteria.py and applies
scoring rules to produce a verdict for each ticker.

IMPORTANT: This is the ONLY file that should be modified when:
    - Adjusting thresholds for any criterion
    - Changing weights (point values) for any criterion
    - Modifying verdict thresholds (VIABLE / CAUTION / DO_NOT_TRADE)
    - Incorporating audit feedback to improve the system

Raw data comes from:    criteria.py
Audit logic:            audit.py

NOTE: criteria.py now uses Tastytrade API for volatility data.
      iv_percentile, put_call_ratio, open_interest arrive as
      direct floats/ints — not dicts. score_criteria handles both.
"""


# ══════════════════════════════════════════════════════════════════════════════
# SCORING CONSTANTS — edit these to tune the system
# ══════════════════════════════════════════════════════════════════════════════

# ── Verdict thresholds (as % of SCORE_MAX) ───────────────────────────────────
THRESHOLD_VIABLE      = 0.55
THRESHOLD_CAUTION     = 0.35

# ── Maximum possible score (sum of all max positive scores) ──────────────────
SCORE_MAX = 21

# ── Trend 25d (max +1) ───────────────────────────────────────────────────────
SCORE_TREND_BULLISH   =  1
SCORE_TREND_BEARISH   = -1

# ── Moving Averages — SMA50/200 position (max +1) ────────────────────────────
SCORE_SMA_ABOVE_BOTH  =  1
SCORE_SMA_ABOVE_50    =  1
SCORE_SMA_BELOW_BOTH  = -1

# ── SMA50 Direction (max +1) ─────────────────────────────────────────────────
SCORE_SMA50_RISING    =  1
SCORE_SMA50_FLAT      =  0
SCORE_SMA50_FALLING   = -1

# ── RSI (max +1) ─────────────────────────────────────────────────────────────
RSI_OVERBOUGHT        = 70
RSI_OVERSOLD          = 30
RSI_NEUTRAL_LOW       = 40
RSI_NEUTRAL_HIGH      = 60

SCORE_RSI_NEUTRAL     =  1
SCORE_RSI_CAUTION     =  0
SCORE_RSI_EXTREME     =  0

# ── Historical Volatility (max +1) ───────────────────────────────────────────
HV_LOW_THRESHOLD      = 20
HV_HIGH_THRESHOLD     = 35

SCORE_HV_LOW          =  1
SCORE_HV_NORMAL       =  1
SCORE_HV_HIGH         =  0

# ── IV vs HV (max +2) ────────────────────────────────────────────────────────
IV_NORMAL_THRESHOLD   = 1.3

SCORE_IV_CHEAP        =  2
SCORE_IV_NORMAL       =  1
SCORE_IV_EXPENSIVE    = -1

# ── IV Percentile (max +2) ───────────────────────────────────────────────────
IV_PCT_CHEAP          = 25
IV_PCT_NORMAL_LOW     = 50
IV_PCT_NORMAL_HIGH    = 75

SCORE_IVP_CHEAP       =  2
SCORE_IVP_NORMAL_LOW  =  1
SCORE_IVP_NORMAL_HIGH =  0
SCORE_IVP_EXPENSIVE   = -1

# ── Beta (max +0) ────────────────────────────────────────────────────────────
BETA_HIGH_THRESHOLD   = 1.5
BETA_LOW_THRESHOLD    = 0.8

SCORE_BETA_NORMAL     =  0
SCORE_BETA_LOW        =  0
SCORE_BETA_HIGH       =  0

# ── Put/Call Ratio (max +0) ──────────────────────────────────────────────────
PCR_FEAR_THRESHOLD    = 1.3
PCR_NEUTRAL_LOW       = 0.7
PCR_OPTIMISM          = 0.5

SCORE_PCR_FEAR        =  0
SCORE_PCR_NEUTRAL     =  0
SCORE_PCR_OPTIMISM    =  0
SCORE_PCR_EUPHORIA    =  0

# ── Open Interest (max +0) ───────────────────────────────────────────────────
OI_HIGH_THRESHOLD     = 10000
OI_LOW_THRESHOLD      = 1000

SCORE_OI_HIGH         =  0
SCORE_OI_NORMAL       =  0
SCORE_OI_LOW          =  0

# ── 52-Week Position (max +1) ────────────────────────────────────────────────
SCORE_52W_NEAR_LOW    =  1
SCORE_52W_MID         =  1
SCORE_52W_NEAR_HIGH   =  0

# ── Support / Resistance (max +2) ────────────────────────────────────────────
SR_NEAR_THRESHOLD_PCT = 3.0

SCORE_SR_NEAR_SUPPORT     =  2
SCORE_SR_MIDDLE           =  1
SCORE_SR_NEAR_RESISTANCE  =  0
SCORE_SR_NO_DATA          =  0

# ── Candlestick Pattern (max +1) ─────────────────────────────────────────────
SCORE_CANDLE_STRONG_BULLISH =  1
SCORE_CANDLE_WEAK_BULLISH   =  1
SCORE_CANDLE_NEUTRAL        =  0
SCORE_CANDLE_WEAK_BEARISH   =  0
SCORE_CANDLE_STRONG_BEARISH = -1

# ── Earnings (max +1) ────────────────────────────────────────────────────────
EARNINGS_SAFE_DAYS    = 35
EARNINGS_CAUTION_DAYS = 20

SCORE_EARNINGS_SAFE   =  1
SCORE_EARNINGS_CAUTION=  0
SCORE_EARNINGS_DANGER = -2

# ── Volume (max +1) ──────────────────────────────────────────────────────────
VOLUME_HIGH_PCT       = 150
VOLUME_LOW_PCT        = 80

SCORE_VOLUME_NORMAL   =  1
SCORE_VOLUME_HIGH     =  0
SCORE_VOLUME_LOW      =  0

# ── P/E Ratio (max +1) ───────────────────────────────────────────────────────
PE_CHEAP_THRESHOLD    = 15
PE_NORMAL_THRESHOLD   = 25
PE_EXPENSIVE_THRESHOLD= 40

SCORE_PE_CHEAP        =  1
SCORE_PE_NORMAL       =  1
SCORE_PE_EXPENSIVE    =  0
SCORE_PE_VERY_EXP     =  0

# ── EPS Growth (max +2) ──────────────────────────────────────────────────────
EPS_STRONG_GROWTH     = 15
EPS_MILD_DECLINE      = -10

SCORE_EPS_STRONG      =  2
SCORE_EPS_STABLE      =  1
SCORE_EPS_DECLINING   = -1
SCORE_EPS_DETERIORATING= -2

# ── Debt to Equity (max +0) ──────────────────────────────────────────────────
DE_LOW_THRESHOLD      = 1.0
DE_HIGH_THRESHOLD     = 2.0

SCORE_DE_LOW          =  0
SCORE_DE_MODERATE     =  0
SCORE_DE_HIGH         =  0

# ── Profit Margin (max +2) ───────────────────────────────────────────────────
MARGIN_HIGH_THRESHOLD = 20
MARGIN_LOW_THRESHOLD  = 10

SCORE_MARGIN_HIGH     =  2
SCORE_MARGIN_NORMAL   =  1
SCORE_MARGIN_LOW      =  0
SCORE_MARGIN_NEGATIVE = -2


# ══════════════════════════════════════════════════════════════════════════════
# INDIVIDUAL CRITERION SCORERS
# Each returns (score: int, label: str)
# ══════════════════════════════════════════════════════════════════════════════

def score_trend(trend_data):
    if trend_data.get("is_bullish"):
        return SCORE_TREND_BULLISH, "BULLISH"
    return SCORE_TREND_BEARISH, "BEARISH"


def score_moving_averages(ma_data):
    above_50  = ma_data.get("above_sma50", False)
    above_200 = ma_data.get("above_sma200", False)
    if above_50 and above_200:
        return SCORE_SMA_ABOVE_BOTH, "ABOVE BOTH"
    elif above_50:
        return SCORE_SMA_ABOVE_50, "ABOVE SMA50 ONLY"
    return SCORE_SMA_BELOW_BOTH, "BELOW BOTH"


def score_sma50_direction(ma_data):
    direction = ma_data.get("sma50_direction", "FLAT")
    if direction == "RISING":
        return SCORE_SMA50_RISING, "RISING"
    elif direction == "FALLING":
        return SCORE_SMA50_FALLING, "FALLING"
    return SCORE_SMA50_FLAT, "FLAT"


def score_rsi(rsi_value):
    if rsi_value is None:
        return 0, "NO DATA"
    if rsi_value > RSI_OVERBOUGHT:
        return SCORE_RSI_EXTREME, f"OVERBOUGHT ({rsi_value:.1f})"
    elif rsi_value < RSI_OVERSOLD:
        return SCORE_RSI_EXTREME, f"OVERSOLD ({rsi_value:.1f})"
    elif RSI_NEUTRAL_LOW <= rsi_value <= RSI_NEUTRAL_HIGH:
        return SCORE_RSI_NEUTRAL, f"NEUTRAL ({rsi_value:.1f})"
    return SCORE_RSI_CAUTION, f"CAUTION ({rsi_value:.1f})"


def score_hv(hv_value):
    if hv_value is None:
        return 0, "NO DATA"
    if hv_value < HV_LOW_THRESHOLD:
        return SCORE_HV_LOW, f"LOW ({hv_value:.1f}%)"
    elif hv_value <= HV_HIGH_THRESHOLD:
        return SCORE_HV_NORMAL, f"NORMAL ({hv_value:.1f}%)"
    return SCORE_HV_HIGH, f"HIGH ({hv_value:.1f}%)"


def score_iv_vs_hv(iv_value, hv_value):
    if iv_value is None or hv_value is None:
        return 0, "NO DATA"
    if iv_value < hv_value:
        return SCORE_IV_CHEAP, f"CHEAP ({iv_value:.1f}% vs HV {hv_value:.1f}%)"
    elif iv_value < hv_value * IV_NORMAL_THRESHOLD:
        return SCORE_IV_NORMAL, f"NORMAL ({iv_value:.1f}% vs HV {hv_value:.1f}%)"
    return SCORE_IV_EXPENSIVE, f"EXPENSIVE ({iv_value:.1f}% vs HV {hv_value:.1f}%)"


def score_iv_percentile(pct):
    """
    Accepts either:
        - float/int directly (new format from Tastytrade API)
        - dict {"percentile": float} (legacy format)
    """
    if isinstance(pct, dict):
        pct = pct.get("percentile")
    if pct is None:
        return 0, "NO DATA"
    if pct <= IV_PCT_CHEAP:
        return SCORE_IVP_CHEAP, f"CHEAP (P{pct:.0f})"
    elif pct <= IV_PCT_NORMAL_LOW:
        return SCORE_IVP_NORMAL_LOW, f"NORMAL-LOW (P{pct:.0f})"
    elif pct <= IV_PCT_NORMAL_HIGH:
        return SCORE_IVP_NORMAL_HIGH, f"NORMAL-HIGH (P{pct:.0f})"
    return SCORE_IVP_EXPENSIVE, f"EXPENSIVE (P{pct:.0f})"


def score_beta(beta_value):
    if beta_value is None:
        return 0, "NO DATA"
    if beta_value > BETA_HIGH_THRESHOLD:
        return SCORE_BETA_HIGH, f"HIGH ({beta_value:.1f})"
    elif beta_value >= BETA_LOW_THRESHOLD:
        return SCORE_BETA_NORMAL, f"NORMAL ({beta_value:.1f})"
    return SCORE_BETA_LOW, f"LOW ({beta_value:.1f})"


def score_put_call_ratio(pcr):
    """
    Accepts either:
        - float/int directly (new format from Tastytrade API)
        - dict {"pcr": float} (legacy format)
    """
    if isinstance(pcr, dict):
        pcr = pcr.get("pcr")
    if pcr is None:
        return 0, "NO DATA"
    if pcr > PCR_FEAR_THRESHOLD:
        return SCORE_PCR_FEAR, f"FEAR ({pcr:.2f})"
    elif pcr >= PCR_NEUTRAL_LOW:
        return SCORE_PCR_NEUTRAL, f"NEUTRAL ({pcr:.2f})"
    elif pcr >= PCR_OPTIMISM:
        return SCORE_PCR_OPTIMISM, f"OPTIMISTIC ({pcr:.2f})"
    return SCORE_PCR_EUPHORIA, f"EUPHORIA ({pcr:.2f})"


def score_open_interest(oi):
    """
    Accepts either:
        - int/float directly (new format from Tastytrade API)
        - dict {"oi": int} (legacy format)
    """
    if isinstance(oi, dict):
        oi = oi.get("oi")
    if oi is None:
        return 0, "NO DATA"
    if oi > OI_HIGH_THRESHOLD:
        return SCORE_OI_HIGH, f"HIGH ({oi:,})"
    elif oi >= OI_LOW_THRESHOLD:
        return SCORE_OI_NORMAL, f"NORMAL ({oi:,})"
    return SCORE_OI_LOW, f"LOW ({oi:,})"


def score_52_week(week52_data):
    if not week52_data:
        return 0, "NO DATA"
    near_high = week52_data.get("near_high", False)
    near_low  = week52_data.get("near_low",  False)
    pos       = week52_data.get("position_pct", 50)
    if near_low:
        return SCORE_52W_NEAR_LOW, f"NEAR LOW ({pos:.0f}%)"
    elif near_high:
        return SCORE_52W_NEAR_HIGH, f"NEAR HIGH ({pos:.0f}%)"
    return SCORE_52W_MID, f"MID RANGE ({pos:.0f}%)"


def score_support_resistance(sr_data):
    if not sr_data:
        return SCORE_SR_NO_DATA, "NO DATA"
    sup_pct = sr_data.get("support_pct")
    res_pct = sr_data.get("resistance_pct")
    if sup_pct is None and res_pct is None:
        return SCORE_SR_NO_DATA, "NO DATA"
    if sup_pct is not None and sup_pct <= SR_NEAR_THRESHOLD_PCT:
        return SCORE_SR_NEAR_SUPPORT, f"NEAR SUPPORT ({sup_pct:.1f}% away)"
    if res_pct is not None and res_pct <= SR_NEAR_THRESHOLD_PCT:
        return SCORE_SR_NEAR_RESISTANCE, f"NEAR RESISTANCE ({res_pct:.1f}% away)"
    return SCORE_SR_MIDDLE, "MIDDLE RANGE"


def score_candlestick(candle_data):
    if not candle_data:
        return 0, "NO DATA"
    signal = candle_data.get("signal", "NEUTRAL")
    pattern = candle_data.get("pattern", "UNKNOWN")
    if signal == "BULLISH":
        candles = candle_data.get("candles", 1)
        if candles >= 2:
            return SCORE_CANDLE_STRONG_BULLISH, f"{pattern} (STRONG BULLISH)"
        return SCORE_CANDLE_WEAK_BULLISH, f"{pattern} (BULLISH)"
    elif signal == "BEARISH":
        candles = candle_data.get("candles", 1)
        if candles >= 2:
            return SCORE_CANDLE_STRONG_BEARISH, f"{pattern} (STRONG BEARISH)"
        return SCORE_CANDLE_WEAK_BEARISH, f"{pattern} (BEARISH)"
    return SCORE_CANDLE_NEUTRAL, f"{pattern} (NEUTRAL)"


def score_earnings(earn_data):
    if not earn_data:
        return 0, "NO DATA"
    if earn_data.get("is_etf"):
        return SCORE_EARNINGS_SAFE, "ETF (no earnings)"
    days = earn_data.get("days_to_earnings")
    if days is None:
        return 0, "NO DATA"
    if days >= EARNINGS_SAFE_DAYS:
        return SCORE_EARNINGS_SAFE, f"SAFE ({days}d away)"
    elif days >= EARNINGS_CAUTION_DAYS:
        return SCORE_EARNINGS_CAUTION, f"CAUTION ({days}d away)"
    return SCORE_EARNINGS_DANGER, f"DANGER ({days}d away)"


def score_volume(vol_data):
    if not vol_data:
        return 0, "NO DATA"
    ratio = vol_data.get("volume_ratio_pct", 0)
    if ratio >= VOLUME_HIGH_PCT:
        return SCORE_VOLUME_HIGH, f"HIGH ({ratio:.0f}% of avg)"
    elif ratio >= VOLUME_LOW_PCT:
        return SCORE_VOLUME_NORMAL, f"NORMAL ({ratio:.0f}% of avg)"
    return SCORE_VOLUME_LOW, f"LOW ({ratio:.0f}% of avg)"


def score_pe(fund_data):
    if not fund_data:
        return 0, "NO DATA"
    pe = fund_data.get("pe")
    if pe is None:
        return 0, "NO DATA"
    if pe <= PE_CHEAP_THRESHOLD:
        return SCORE_PE_CHEAP, f"CHEAP ({pe:.1f}x)"
    elif pe <= PE_NORMAL_THRESHOLD:
        return SCORE_PE_NORMAL, f"NORMAL ({pe:.1f}x)"
    elif pe <= PE_EXPENSIVE_THRESHOLD:
        return SCORE_PE_EXPENSIVE, f"EXPENSIVE ({pe:.1f}x)"
    return SCORE_PE_VERY_EXP, f"VERY EXPENSIVE ({pe:.1f}x)"


def score_eps_growth(fund_data):
    if not fund_data:
        return 0, "NO DATA"
    growth = fund_data.get("eps_growth_pct")
    if growth is None:
        return 0, "NO DATA"
    if growth >= EPS_STRONG_GROWTH:
        return SCORE_EPS_STRONG, f"STRONG GROWTH ({growth:+.1f}%)"
    elif growth >= 0:
        return SCORE_EPS_STABLE, f"STABLE ({growth:+.1f}%)"
    elif growth >= EPS_MILD_DECLINE:
        return SCORE_EPS_DECLINING, f"DECLINING ({growth:+.1f}%)"
    return SCORE_EPS_DETERIORATING, f"DETERIORATING ({growth:+.1f}%)"


def score_debt_equity(fund_data):
    if not fund_data:
        return 0, "NO DATA"
    de = fund_data.get("debt_to_equity")
    if de is None:
        return 0, "NO DATA"
    if de <= DE_LOW_THRESHOLD:
        return SCORE_DE_LOW, f"LOW ({de:.2f}x)"
    elif de <= DE_HIGH_THRESHOLD:
        return SCORE_DE_MODERATE, f"MODERATE ({de:.2f}x)"
    return SCORE_DE_HIGH, f"HIGH ({de:.2f}x)"


def score_profit_margin(fund_data):
    if not fund_data:
        return 0, "NO DATA"
    margin = fund_data.get("profit_margin_pct")
    if margin is None:
        return 0, "NO DATA"
    if margin >= MARGIN_HIGH_THRESHOLD:
        return SCORE_MARGIN_HIGH, f"HIGH ({margin:.1f}%)"
    elif margin >= MARGIN_LOW_THRESHOLD:
        return SCORE_MARGIN_NORMAL, f"NORMAL ({margin:.1f}%)"
    elif margin >= 0:
        return SCORE_MARGIN_LOW, f"LOW ({margin:.1f}%)"
    return SCORE_MARGIN_NEGATIVE, f"NEGATIVE ({margin:.1f}%)"


# ══════════════════════════════════════════════════════════════════════════════
# VERDICT
# ══════════════════════════════════════════════════════════════════════════════

def get_verdict(score, score_max=SCORE_MAX):
    pct = score / score_max if score_max > 0 else 0
    if pct >= THRESHOLD_VIABLE:
        return "VIABLE"
    elif pct >= THRESHOLD_CAUTION:
        return "CAUTION"
    return "DO_NOT_TRADE"


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def score_criteria(criteria):
    """
    Apply all scoring rules to a criteria dict from criteria.py.

    Compatible with both old (yfinance) and new (Tastytrade) data formats.
    iv_percentile, put_call_ratio, open_interest may arrive as direct
    floats/ints or as legacy dicts — scorer functions handle both.
    """
    if criteria is None:
        return None

    tech  = criteria.get("technical", {})
    vol   = criteria.get("volatility", {})
    earn  = criteria.get("earnings", {})
    volum = criteria.get("volume", {})
    fund  = criteria.get("fundamental", {})

    hv_value = vol.get("hv_30d")
    iv_value = vol.get("iv") or vol.get("implied", {}).get("iv")

    results = {}

    results["trend_25d"]          = score_trend(tech.get("trend_25d", {}))
    results["moving_averages"]    = score_moving_averages(tech.get("moving_averages", {}))
    results["sma50_direction"]    = score_sma50_direction(tech.get("moving_averages", {}))
    results["rsi"]                = score_rsi(tech.get("rsi"))
    results["week_52"]            = score_52_week(tech.get("week_52", {}))
    results["support_resistance"] = score_support_resistance(tech.get("support_resistance", {}))
    results["candlestick"]        = score_candlestick(tech.get("candlestick", {}))
    results["hv"]                 = score_hv(hv_value)
    results["iv_vs_hv"]           = score_iv_vs_hv(iv_value, hv_value)
    results["iv_percentile"]      = score_iv_percentile(vol.get("iv_percentile"))
    results["beta"]               = score_beta(vol.get("beta"))
    results["put_call_ratio"]     = score_put_call_ratio(vol.get("put_call_ratio"))
    results["open_interest"]      = score_open_interest(vol.get("open_interest"))
    results["earnings"]           = score_earnings(earn)
    results["volume"]             = score_volume(volum)
    results["pe"]                 = score_pe(fund)
    results["eps_growth"]         = score_eps_growth(fund)
    results["debt_equity"]        = score_debt_equity(fund)
    results["profit_margin"]      = score_profit_margin(fund)

    total_score = sum(v[0] for v in results.values())
    score_pct   = round(total_score / SCORE_MAX * 100, 1)
    verdict     = get_verdict(total_score)

    return {
        "ticker":    criteria["ticker"],
        "timestamp": criteria["timestamp"],
        "price":     criteria["price"],
        "score":     total_score,
        "score_max": SCORE_MAX,
        "score_pct": score_pct,
        "verdict":   verdict,
        "criteria_scores": {
            k: {"score": v[0], "label": v[1]}
            for k, v in results.items()
        }
    }