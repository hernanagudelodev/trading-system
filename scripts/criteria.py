"""
criteria.py
===========
Pure data extraction module for Bull Call Spread analysis system.

This file is responsible ONLY for fetching and calculating raw market data.
It does NOT score, rate, or make any trading decisions.

Scoring logic lives in: scoring.py
History management lives in: history.py
Audit logic lives in: audit.py
Orchestration lives in: main.py

Data sources: yfinance (Yahoo Finance)
"""

import sys
import io
import datetime
import warnings

import yfinance as yf
import pandas as pd

warnings.filterwarnings("ignore")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _silence_stderr():
    """Redirect stderr to suppress yfinance HTTP warnings."""
    old = sys.stderr
    sys.stderr = io.StringIO()
    return old

def _restore_stderr(old):
    """Restore stderr after silencing."""
    sys.stderr = old

def _nearest_expiration(expirations, target_days=30):
    """
    Given a list of expiration date strings (YYYY-MM-DD),
    return the one closest to target_days from today.
    """
    today = datetime.date.today()
    best = None
    best_diff = 9999
    for exp in expirations:
        exp_date = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
        diff = abs((exp_date - today).days - target_days)
        if diff < best_diff:
            best_diff = diff
            best = exp
    return best


# ══════════════════════════════════════════════════════════════════════════════
# PRICE HISTORY — download 1 year of daily OHLCV data
# ══════════════════════════════════════════════════════════════════════════════

def get_price_history(ticker, retries=3, delay=3):
    """
    Download 1 year of daily price data for a ticker using yfinance.

    Retries up to `retries` times with `delay` seconds between attempts
    to handle transient connection timeouts.

    Returns:
        pd.DataFrame with columns [Open, High, Low, Close, Volume]
        or empty DataFrame if all attempts fail.
    """
    import time
    data = pd.DataFrame()
    for attempt in range(retries):
        try:
            data = yf.download(
                ticker,
                period="1y",
                interval="1d",
                progress=False,
                timeout=20
            )
            if not data.empty:
                break
        except Exception:
            if attempt < retries - 1:
                time.sleep(delay)
    return data


# ══════════════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def get_trend_25d(closes):
    """
    25-day price trend.

    Compares today's closing price against 25 trading days ago.
    A simple directional measure of short-to-medium term momentum.

    Returns:
        dict with keys:
            price_now   (float) — current close
            price_25d   (float) — close 25 days ago
            pct_change  (float) — % change over 25 days
            is_bullish  (bool)
    """
    price_now = float(closes.iloc[-1])
    price_25d = float(closes.iloc[-25])
    pct = (price_now - price_25d) / price_25d * 100
    return {
        "price_now": price_now,
        "price_25d": price_25d,
        "pct_change": round(pct, 2),
        "is_bullish": price_now > price_25d
    }


def get_moving_averages(closes):
    """
    SMA 50 and SMA 200 — Simple Moving Averages.

    SMA 50  → medium-term trend (last 50 trading days average)
    SMA 200 → long-term trend   (last 200 trading days average)

    Price above SMA → bullish signal for that timeframe.
    Price below SMA → bearish signal.

    Also computes SMA50 direction by comparing today's SMA50
    vs 10 days ago (threshold ±0.1% to filter noise).

    Returns:
        dict with keys:
            sma50           (float)
            sma200          (float)
            above_sma50     (bool)
            above_sma200    (bool)
            sma50_direction (str) — "RISING" | "FALLING" | "FLAT"
    """
    sma50_series = closes.rolling(window=50).mean()
    sma200_series = closes.rolling(window=200).mean()

    sma50 = float(sma50_series.iloc[-1])
    sma200 = float(sma200_series.iloc[-1])
    price = float(closes.iloc[-1])

    sma50_10d = float(sma50_series.iloc[-10])
    if sma50 > sma50_10d * 1.001:
        direction = "RISING"
    elif sma50 < sma50_10d * 0.999:
        direction = "FALLING"
    else:
        direction = "FLAT"

    return {
        "sma50": round(sma50, 2),
        "sma200": round(sma200, 2),
        "above_sma50": price > sma50,
        "above_sma200": price > sma200,
        "sma50_direction": direction
    }


def get_rsi(closes, period=14):
    """
    RSI — Relative Strength Index (14-day default).

    Measures how fast and aggressively the price has moved.
    Ranges from 0 to 100.

    Calculation:
        1. Compute daily price changes
        2. Separate gains and losses
        3. Average gains and losses over `period` days
        4. RS = avg_gain / avg_loss
        5. RSI = 100 - (100 / (1 + RS))

    Interpretation:
        RSI > 70 → overbought  → potential correction
        RSI < 30 → oversold    → potential bounce
        RSI 40-60 → neutral    → healthy range for entry

    Returns:
        float — RSI value (0 to 100)
    """
    delta = closes.diff()
    gains = delta.where(delta > 0, 0)
    losses = -delta.where(delta < 0, 0)
    avg_gains = gains.rolling(window=period).mean()
    avg_losses = losses.rolling(window=period).mean()
    rs = avg_gains / avg_losses
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def get_52_week_position(closes, price):
    """
    Position of current price within the 52-week (252 trading days) range.

    Identifies whether the price is near a major annual high or low,
    which often act as strong resistance or support levels respectively.

    Calculation:
        position_pct = (price - min_52w) / (max_52w - min_52w) * 100

    Returns:
        dict with keys:
            max_52w         (float) — highest close in last 252 days
            min_52w         (float) — lowest close in last 252 days
            position_pct    (float) — 0% = at annual low, 100% = at annual high
            near_high       (bool)  — price within 5% of annual high
            near_low        (bool)  — price within 5% of annual low
    """
    max_52w = float(closes.tail(252).max())
    min_52w = float(closes.tail(252).min())
    rng = max_52w - min_52w
    position = (price - min_52w) / rng * 100 if rng > 0 else 50.0

    return {
        "max_52w": round(max_52w, 2),
        "min_52w": round(min_52w, 2),
        "position_pct": round(position, 1),
        "near_high": price >= max_52w * 0.95,
        "near_low": price <= min_52w * 1.05
    }


def get_support_resistance(closes, price, window=5, zone_pct=0.02):
    """
    Automatic support and resistance detection using local price pivots.

    Algorithm:
        1. Scan all prices in the series
        2. A price is a HIGH PIVOT if it's greater than the `window`
           candles before AND after it (local maximum)
        3. A price is a LOW PIVOT if it's less than the `window`
           candles before AND after it (local minimum)
        4. Group nearby pivots into zones (within zone_pct % of each other)
           because the market never bounces at the exact same price twice
        5. Find the nearest support zone below current price
        6. Find the nearest resistance zone above current price
        7. Calculate distance from current price to each zone

    Returns:
        dict with keys:
            support_level       (float | None) — nearest support price
            support_dist_pct    (float | None) — % distance to support
            support_strength    (int   | None) — how many times price bounced
            resistance_level    (float | None) — nearest resistance price
            resistance_dist_pct (float | None) — % distance to resistance
            resistance_strength (int   | None) — how many times price bounced
    """
    prices = closes.values
    high_pivots = []
    low_pivots = []

    for i in range(window, len(prices) - window):
        is_high = all(
            prices[i] >= prices[i - j] and prices[i] >= prices[i + j]
            for j in range(1, window + 1)
        )
        is_low = all(
            prices[i] <= prices[i - j] and prices[i] <= prices[i + j]
            for j in range(1, window + 1)
        )
        if is_high:
            high_pivots.append(prices[i])
        if is_low:
            low_pivots.append(prices[i])

    def group_zones(pivots):
        if not pivots:
            return []
        zones = []
        for p in sorted(pivots):
            if zones and (p - zones[-1]["level"]) / zones[-1]["level"] <= zone_pct:
                zones[-1]["prices"].append(p)
                zones[-1]["level"] = sum(zones[-1]["prices"]) / len(zones[-1]["prices"])
                zones[-1]["strength"] = len(zones[-1]["prices"])
            else:
                zones.append({"level": p, "prices": [p], "strength": 1})
        return zones

    supports = [z for z in group_zones(low_pivots) if z["level"] < price]
    resistances = [z for z in group_zones(high_pivots) if z["level"] > price]

    nearest_support = max(supports, key=lambda z: z["level"]) if supports else None
    nearest_resistance = min(resistances, key=lambda z: z["level"]) if resistances else None

    return {
        "support_level": round(nearest_support["level"], 2) if nearest_support else None,
        "support_dist_pct": round((price - nearest_support["level"]) / price * 100, 2) if nearest_support else None,
        "support_strength": nearest_support["strength"] if nearest_support else None,
        "resistance_level": round(nearest_resistance["level"], 2) if nearest_resistance else None,
        "resistance_dist_pct": round((nearest_resistance["level"] - price) / price * 100, 2) if nearest_resistance else None,
        "resistance_strength": nearest_resistance["strength"] if nearest_resistance else None,
    }


def get_candlestick_pattern(data):
    """
    Detect candlestick patterns from the last 3 candles.

    Uses 3 candles because:
        - 1 candle alone can be noise
        - 2-3 candles provide confirmation of direction

    Patterns detected (in priority order):
        3-candle: Morning Star (bullish reversal), Evening Star (bearish reversal)
        2-candle: Bullish Engulfing, Bearish Engulfing
        1-candle: Marubozu Green/Red, Hammer, Shooting Star, Doji

    Definitions:
        Large body    → body > 60% of full candle range
        Doji          → body < 10% of full candle range
        Hammer        → lower wick > 2x body, upper wick < 0.5x body
        Shooting Star → upper wick > 2x body, lower wick < 0.5x body
        Engulfing     → second candle's body completely contains first candle's body

    Returns:
        dict with keys:
            pattern     (str)  — pattern name
            signal      (str)  — "BULLISH" | "BEARISH" | "NEUTRAL"
            candles     (int)  — how many candles form the pattern
    """
    opens = data["Open"].squeeze()
    closes_d = data["Close"].squeeze()
    highs = data["High"].squeeze()
    lows = data["Low"].squeeze()

    o1, o2, o3 = float(opens.iloc[-3]), float(opens.iloc[-2]), float(opens.iloc[-1])
    c1, c2, c3 = float(closes_d.iloc[-3]), float(closes_d.iloc[-2]), float(closes_d.iloc[-1])
    h3 = float(highs.iloc[-1])
    l3 = float(lows.iloc[-1])

    body1, body2, body3 = abs(c1 - o1), abs(c2 - o2), abs(c3 - o3)
    range1 = float(highs.iloc[-3]) - float(lows.iloc[-3])
    range2 = float(highs.iloc[-2]) - float(lows.iloc[-2])
    range3 = h3 - l3

    is_green = lambda o, c: c > o
    is_red   = lambda o, c: c < o
    is_large = lambda body, rng: rng > 0 and body > rng * 0.6
    is_doji  = lambda body, rng: rng > 0 and body < rng * 0.1

    upper_wick3 = h3 - max(o3, c3)
    lower_wick3 = min(o3, c3) - l3

    # ── 3-candle patterns ─────────────────────────────────────────────────────
    if (is_red(o1, c1) and is_large(body1, range1) and
            is_doji(body2, range2) and
            is_green(o3, c3) and is_large(body3, range3)):
        return {"pattern": "MORNING STAR", "signal": "BULLISH", "candles": 3}

    if (is_green(o1, c1) and is_large(body1, range1) and
            is_doji(body2, range2) and
            is_red(o3, c3) and is_large(body3, range3)):
        return {"pattern": "EVENING STAR", "signal": "BEARISH", "candles": 3}

    # ── 2-candle patterns ─────────────────────────────────────────────────────
    if is_red(o2, c2) and is_green(o3, c3) and o3 <= c2 and c3 >= o2:
        return {"pattern": "BULLISH ENGULFING", "signal": "BULLISH", "candles": 2}

    if is_green(o2, c2) and is_red(o3, c3) and o3 >= c2 and c3 <= o2:
        return {"pattern": "BEARISH ENGULFING", "signal": "BEARISH", "candles": 2}

    # ── 1-candle patterns ─────────────────────────────────────────────────────
    if (is_green(o3, c3) and is_large(body3, range3) and
            upper_wick3 < body3 * 0.1 and lower_wick3 < body3 * 0.1):
        return {"pattern": "MARUBOZU GREEN", "signal": "BULLISH", "candles": 1}

    if (is_red(o3, c3) and is_large(body3, range3) and
            upper_wick3 < body3 * 0.1 and lower_wick3 < body3 * 0.1):
        return {"pattern": "MARUBOZU RED", "signal": "BEARISH", "candles": 1}

    if lower_wick3 > body3 * 2 and upper_wick3 < body3 * 0.5:
        return {"pattern": "HAMMER", "signal": "BULLISH", "candles": 1}

    if upper_wick3 > body3 * 2 and lower_wick3 < body3 * 0.5:
        return {"pattern": "SHOOTING STAR", "signal": "BEARISH", "candles": 1}

    if is_doji(body3, range3):
        return {"pattern": "DOJI", "signal": "NEUTRAL", "candles": 1}

    if is_green(o3, c3):
        return {"pattern": "GREEN CANDLE", "signal": "BULLISH", "candles": 1}

    return {"pattern": "RED CANDLE", "signal": "BEARISH", "candles": 1}


# ══════════════════════════════════════════════════════════════════════════════
# VOLATILITY METRICS
# ══════════════════════════════════════════════════════════════════════════════

def get_historical_volatility(closes, period=30):
    """
    HV — Historical Volatility (annualized, 30-day default).

    Measures how much the stock has actually moved in the past.
    Used as a baseline to compare against Implied Volatility.

    Calculation:
        1. Compute daily log returns: ln(price_today / price_yesterday)
        2. Calculate rolling standard deviation over `period` days
        3. Annualize by multiplying by sqrt(252 trading days)
        4. Convert to percentage

    Returns:
        float — HV as a percentage (e.g. 25.3 means 25.3%)
    """
    returns = closes.pct_change().dropna()
    hv = returns.rolling(window=period).std().iloc[-1]
    return round(float(hv * (252 ** 0.5) * 100), 2)


def get_implied_volatility(ticker, price):
    """
    IV — Implied Volatility from the ATM call option nearest to 30 DTE.

    IV is extracted from the option chain (not calculated from price history).
    It reflects what the market EXPECTS the stock to do in the future.

    Method:
        1. Find the expiration date closest to 30 days from today
        2. Find the call option with strike closest to current price (ATM)
        3. Return the impliedVolatility field from yfinance

    Returns:
        dict with keys:
            iv          (float | None) — IV as percentage
            expiration  (str   | None) — expiration date used
    """
    old = _silence_stderr()
    try:
        tk = yf.Ticker(ticker)
        expirations = tk.options
        _restore_stderr(old)

        if not expirations:
            return {"iv": None, "expiration": None}

        best_exp = _nearest_expiration(expirations, target_days=30)
        if not best_exp:
            return {"iv": None, "expiration": None}

        chain = tk.option_chain(best_exp)
        calls = chain.calls[chain.calls["strike"] > 0]
        idx = (calls["strike"] - price).abs().idxmin()
        iv = float(calls.loc[idx, "impliedVolatility"]) * 100

        return {"iv": round(iv, 2), "expiration": best_exp}

    except Exception:
        _restore_stderr(old)
        return {"iv": None, "expiration": None}


def get_iv_percentile(closes, iv_current):
    """
    IV Percentile — where current IV sits within its own historical range.

    Solves the problem of comparing IV across different stocks:
        - 30% IV may be cheap for TSLA but expensive for SPY
        - Percentile normalizes this: P20 = cheaper than usual, P80 = more expensive than usual

    Method:
        - Approximate historical IV using rolling 30-day HV as a proxy
        - Calculate what % of historical days had lower volatility than today

    Returns:
        dict with keys:
            percentile  (float | None) — 0 to 100
    """
    if iv_current is None:
        return {"percentile": None}

    returns = closes.pct_change().dropna()
    hv_history = returns.rolling(window=30).std().dropna() * (252 ** 0.5) * 100

    if len(hv_history) < 30:
        return {"percentile": None}

    percentile = (hv_history < iv_current).sum() / len(hv_history) * 100
    return {"percentile": round(float(percentile), 1)}


def get_beta(ticker):
    """
    Beta — correlation and amplification vs S&P 500.

    Beta = 1.0  → moves in line with the market
    Beta > 1.0  → moves more than the market (higher risk/reward)
    Beta < 1.0  → moves less than the market (more stable)

    Source: yfinance info field (pre-calculated by Yahoo Finance).

    Returns:
        float | None
    """
    old = _silence_stderr()
    try:
        tk = yf.Ticker(ticker)
        beta = tk.info.get("beta")
        _restore_stderr(old)
        return round(float(beta), 2) if beta is not None else None
    except Exception:
        _restore_stderr(old)
        return None


def get_put_call_ratio(ticker, price):
    """
    PCR — Put/Call Ratio from options volume at nearest 30-DTE expiration.

    PCR = total put volume / total call volume

    Interpretation (contrarian indicator):
        PCR > 1.3 → extreme fear    → market may be oversold → bullish contrarian signal
        PCR 0.7-1.3 → neutral
        PCR 0.5-0.7 → optimistic    → mild warning
        PCR < 0.5   → euphoria      → market may be overbought → bearish contrarian signal

    Returns:
        dict with keys:
            pcr             (float | None)
            call_volume     (int)
            put_volume      (int)
            expiration      (str | None)
    """
    old = _silence_stderr()
    try:
        tk = yf.Ticker(ticker)
        expirations = tk.options
        _restore_stderr(old)

        if not expirations:
            return {"pcr": None, "call_volume": 0, "put_volume": 0, "expiration": None}

        best_exp = _nearest_expiration(expirations, target_days=30)
        chain = tk.option_chain(best_exp)

        call_vol = int(chain.calls["volume"].fillna(0).sum())
        put_vol  = int(chain.puts["volume"].fillna(0).sum())

        pcr = round(put_vol / call_vol, 3) if call_vol > 0 else None

        return {
            "pcr": pcr,
            "call_volume": call_vol,
            "put_volume": put_vol,
            "expiration": best_exp
        }

    except Exception:
        _restore_stderr(old)
        return {"pcr": None, "call_volume": 0, "put_volume": 0, "expiration": None}


def get_open_interest(ticker, price):
    """
    Open Interest — total number of open option contracts at ATM strikes (nearest 30 DTE).

    Measures liquidity in the options market for this specific ticker.
    Sums OI across the 5 strikes closest to current price to avoid
    the issue of a single ATM strike having low OI by chance.

    High OI → easy to enter and exit positions, tight bid/ask spreads.
    Low OI  → illiquid, wide spreads, harder to exit at fair price.

    Returns:
        dict with keys:
            oi          (int | None) — total open interest (top 5 ATM strikes)
            expiration  (str | None)
    """
    old = _silence_stderr()
    try:
        tk = yf.Ticker(ticker)
        expirations = tk.options
        _restore_stderr(old)

        if not expirations:
            return {"oi": None, "expiration": None}

        best_exp = _nearest_expiration(expirations, target_days=30)
        chain = tk.option_chain(best_exp)
        calls = chain.calls

        calls_sorted = calls.reindex(
            (calls["strike"] - price).abs().sort_values().index
        )
        top5 = calls_sorted.head(5)
        oi = int(top5["openInterest"].sum())

        return {"oi": oi if oi > 0 else None, "expiration": best_exp}

    except Exception:
        _restore_stderr(old)
        return {"oi": None, "expiration": None}


# ══════════════════════════════════════════════════════════════════════════════
# EARNINGS & CORPORATE EVENTS
# ══════════════════════════════════════════════════════════════════════════════

def get_earnings_info(ticker):
    """
    Days until next earnings report and ETF classification.

    Earnings events cause IV to spike before and collapse after (IV crush).
    Buying options close to earnings is high risk for buyers.

    Method:
        - Fetch calendar data from yfinance
        - Calculate days from today to next earnings date
        - Validate range: ignore dates < 0 or > 365 (data errors)
        - Detect ETFs separately (they have no earnings dates)

    Returns:
        dict with keys:
            days_to_earnings    (int | None) — None if no valid date found
            is_etf              (bool)
    """
    old = _silence_stderr()
    try:
        tk = yf.Ticker(ticker)
        info = tk.info
        is_etf = info.get("quoteType", "") == "ETF"
        _restore_stderr(old)

        if is_etf:
            return {"days_to_earnings": None, "is_etf": True}

        calendar = tk.calendar
        if not calendar or "Earnings Date" not in calendar:
            return {"days_to_earnings": None, "is_etf": False}

        dates = calendar["Earnings Date"]
        if not dates:
            return {"days_to_earnings": None, "is_etf": False}

        date = dates[0] if isinstance(dates, list) else dates
        days = (date - datetime.date.today()).days

        if days < 0 or days > 365:
            return {"days_to_earnings": None, "is_etf": False}

        return {"days_to_earnings": int(days), "is_etf": False}

    except Exception:
        _restore_stderr(old)
        return {"days_to_earnings": None, "is_etf": False}


# ══════════════════════════════════════════════════════════════════════════════
# VOLUME
# ══════════════════════════════════════════════════════════════════════════════

def get_volume(data):
    """
    Volume analysis — today vs 20-day average.

    Compares current session volume against the 20-day average
    to detect unusual activity (very high or very low).

    Very high volume → unusual event (news, institutional activity) — caution
    Normal volume    → healthy liquidity
    Low volume       → illiquid session, wider spreads

    Returns:
        dict with keys:
            volume_today        (int)
            volume_avg_20d      (float)
            volume_ratio_pct    (float) — today / avg * 100
    """
    vols = data["Volume"].squeeze()
    vol_today = int(vols.iloc[-1])
    avg_20d = float(vols.iloc[-20:].mean())
    ratio = round(vol_today / avg_20d * 100, 1) if avg_20d > 0 else 0.0

    return {
        "volume_today": vol_today,
        "volume_avg_20d": round(avg_20d, 0),
        "volume_ratio_pct": ratio
    }


# ══════════════════════════════════════════════════════════════════════════════
# FUNDAMENTAL ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def get_fundamentals(ticker):
    """
    Key fundamental metrics from yfinance info.

    Fundamentals add context that technical analysis cannot provide:
    a bullish chart pattern on a deteriorating business is risky.

    Metrics:
        PE Ratio (trailing)     → price / earnings per share
                                   Low = cheap, High = expensive
        EPS Growth              → (forward_eps - trailing_eps) / |trailing_eps|
                                   Measures expected earnings growth
        Debt to Equity          → total_debt / shareholder_equity
                                   Low = financially healthy
        Profit Margin           → net_income / revenue
                                   High = efficient, scalable business

    Returns:
        dict with keys:
            pe                  (float | None)
            eps_trailing        (float | None)
            eps_forward         (float | None)
            eps_growth_pct      (float | None)
            debt_to_equity      (float | None)
            profit_margin_pct   (float | None)
    """
    old = _silence_stderr()
    try:
        tk = yf.Ticker(ticker)
        info = tk.info
        _restore_stderr(old)

        pe = info.get("trailingPE") or info.get("forwardPE")
        eps_trailing = info.get("trailingEps")
        eps_forward = info.get("forwardEps")
        de_raw = info.get("debtToEquity")
        margin = info.get("profitMargins")

        eps_growth = None
        if eps_trailing and eps_forward and eps_trailing != 0:
            eps_growth = round(
                (eps_forward - eps_trailing) / abs(eps_trailing) * 100, 2
            )

        return {
            "pe": round(float(pe), 2) if pe else None,
            "eps_trailing": round(float(eps_trailing), 2) if eps_trailing else None,
            "eps_forward": round(float(eps_forward), 2) if eps_forward else None,
            "eps_growth_pct": eps_growth,
            "debt_to_equity": round(float(de_raw) / 100, 2) if de_raw is not None else None,
            "profit_margin_pct": round(float(margin) * 100, 2) if margin else None
        }

    except Exception:
        _restore_stderr(old)
        return {
            "pe": None,
            "eps_trailing": None,
            "eps_forward": None,
            "eps_growth_pct": None,
            "debt_to_equity": None,
            "profit_margin_pct": None
        }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT — collect ALL criteria for one ticker
# ══════════════════════════════════════════════════════════════════════════════

def get_all_criteria(ticker):
    """
    Fetch and compute ALL criteria for a given ticker.

    This is the single public function that main.py / scoring.py should call.
    Returns a structured dict of raw data with NO scoring or rating applied.

    Returns:
        dict with keys:
            ticker      (str)
            timestamp   (str)   — ISO format
            price       (float)
            technical   (dict)
            volatility  (dict)
            earnings    (dict)
            volume      (dict)
            fundamental (dict)

        or None if insufficient price data is available.
    """
    data = get_price_history(ticker)

    if data.empty or len(data) < 50:
        return None

    closes = data["Close"].squeeze()
    price = float(closes.iloc[-1])

    iv_data = get_implied_volatility(ticker, price)
    iv_value = iv_data["iv"]
    hv_value = get_historical_volatility(closes)

    return {
        "ticker": ticker,
        "timestamp": datetime.datetime.now().isoformat(timespec="minutes"),
        "price": round(price, 2),

        "technical": {
            "trend_25d":            get_trend_25d(closes),
            "moving_averages":      get_moving_averages(closes),
            "rsi":                  get_rsi(closes),
            "week_52":              get_52_week_position(closes, price),
            "support_resistance":   get_support_resistance(closes, price),
            "candlestick":          get_candlestick_pattern(data),
        },

        "volatility": {
            "hv_30d":           hv_value,
            "implied":          iv_data,
            "iv_percentile":    get_iv_percentile(closes, iv_value),
            "beta":             get_beta(ticker),
            "put_call_ratio":   get_put_call_ratio(ticker, price),
            "open_interest":    get_open_interest(ticker, price),
        },

        "earnings":     get_earnings_info(ticker),
        "volume":       get_volume(data),
        "fundamental":  get_fundamentals(ticker),
    }