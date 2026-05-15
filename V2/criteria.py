"""
criteria.py
===========
Pure market data extraction — returns raw numeric values only.
No interpretation, no labels, no scoring.

Two public functions:
    get_all_criteria(ticker)
        → For scanner: downloads today's data and returns raw values.

    get_raw_criteria(data, as_of_date)
        → For backtest: receives pre-downloaded OHLCV slice and
          returns raw values as of that date.
          Does NOT download anything — all data comes from the caller.

The 9 reliable criteria (calculable from OHLCV only):
    1.  trend_25d          — pct change over last 25 days
    2.  sma50              — SMA50 value
    3.  sma200             — SMA200 value
    4.  above_sma50        — price > SMA50 (bool)
    5.  above_sma200       — price > SMA200 (bool)
    6.  sma50_direction    — RISING / FLAT / FALLING
    7.  rsi                — RSI 14-day value
    8.  week_52_position   — position in 52-week range (0-100%)
    9.  nearest_support_pct    — % distance to nearest support below price
    10. nearest_resistance_pct — % distance to nearest resistance above price
    11. candlestick_signal — BULLISH / BEARISH / NEUTRAL
    12. candlestick_pattern — pattern name (HAMMER, DOJI, etc.)
    13. hv_30d             — historical volatility 30-day annualized %
    14. volume_ratio        — today's volume / 20-day avg volume

Note: criteria 1-14 map to the 9 conceptual criteria
(moving_averages expands to sma50, sma200, above_sma50, above_sma200, sma50_direction)
(support_resistance expands to nearest_support_pct, nearest_resistance_pct)
(candlestick expands to candlestick_signal, candlestick_pattern)
"""

import sys
import io
import datetime
import warnings

import yfinance as yf
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")

# Minimum data points needed for reliable indicators
MIN_DATA_POINTS = 60


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _silence_stderr():
    old = sys.stderr
    sys.stderr = io.StringIO()
    return old

def _restore_stderr(old):
    sys.stderr = old


# ══════════════════════════════════════════════════════════════════════════════
# INDIVIDUAL CALCULATORS
# Each function receives a closes Series (and data DataFrame where needed).
# Each function returns raw numeric values ONLY.
# ══════════════════════════════════════════════════════════════════════════════

def calc_trend_25d(closes):
    """
    25-day price trend.

    Returns:
        float — pct change over last 25 days
                positive = price went up, negative = price went down
        None  — insufficient data
    """
    if len(closes) < 26:
        return None

    price_now = float(closes.iloc[-1])
    price_25d = float(closes.iloc[-25])

    if price_25d == 0:
        return None

    return round((price_now - price_25d) / price_25d * 100, 4)


def calc_moving_averages(closes):
    """
    SMA50, SMA200, and SMA50 direction.

    Returns:
        dict with keys:
            sma50           (float)         — current SMA50 value
            sma200          (float | None)  — current SMA200 value, None if < 200 days
            above_sma50     (bool)          — price > SMA50
            above_sma200    (bool | None)   — price > SMA200, None if < 200 days
            sma50_pct       (float)         — % distance of price from SMA50
            sma200_pct      (float | None)  — % distance of price from SMA200
            sma50_direction (str)           — "RISING" | "FLAT" | "FALLING"
    """
    if len(closes) < 50:
        return {
            "sma50": None, "sma200": None,
            "above_sma50": None, "above_sma200": None,
            "sma50_pct": None, "sma200_pct": None,
            "sma50_direction": None,
        }

    price         = float(closes.iloc[-1])
    sma50_series  = closes.rolling(window=50).mean()
    sma50         = float(sma50_series.iloc[-1])

    sma200        = None
    above_sma200  = None
    sma200_pct    = None

    if len(closes) >= 200:
        sma200_series = closes.rolling(window=200).mean()
        sma200        = float(sma200_series.iloc[-1])
        above_sma200  = price > sma200
        sma200_pct    = round((price - sma200) / sma200 * 100, 4)

    # SMA50 direction: compare current vs 10 trading days ago
    sma50_direction = "FLAT"
    if len(sma50_series.dropna()) >= 10:
        sma50_10d = float(sma50_series.dropna().iloc[-10])
        if sma50_10d > 0:
            if sma50 > sma50_10d * 1.001:
                sma50_direction = "RISING"
            elif sma50 < sma50_10d * 0.999:
                sma50_direction = "FALLING"

    return {
        "sma50":           round(sma50, 4),
        "sma200":          round(sma200, 4) if sma200 else None,
        "above_sma50":     price > sma50,
        "above_sma200":    above_sma200,
        "sma50_pct":       round((price - sma50) / sma50 * 100, 4),
        "sma200_pct":      sma200_pct,
        "sma50_direction": sma50_direction,
    }


def calc_rsi(closes, period=14):
    """
    RSI — Relative Strength Index.

    Returns:
        float — RSI value (0 to 100)
        None  — insufficient data
    """
    if len(closes) < period + 1:
        return None

    delta      = closes.diff()
    gains      = delta.where(delta > 0, 0.0)
    losses     = -delta.where(delta < 0, 0.0)
    avg_gains  = gains.rolling(window=period).mean()
    avg_losses = losses.rolling(window=period).mean()

    last_loss = float(avg_losses.iloc[-1])
    if last_loss == 0:
        return 100.0

    rs  = avg_gains / avg_losses
    rsi = 100 - (100 / (1 + rs))

    return round(float(rsi.iloc[-1]), 4)


def calc_week_52_position(closes):
    """
    Position of current price within the 52-week (252 trading days) range.

    Returns:
        float — 0.0 = at annual low, 100.0 = at annual high
        None  — insufficient data
    """
    if len(closes) < 30:
        return None

    window  = min(252, len(closes))
    history = closes.tail(window)
    max_52w = float(history.max())
    min_52w = float(history.min())
    price   = float(closes.iloc[-1])
    rng     = max_52w - min_52w

    if rng == 0:
        return 50.0

    return round((price - min_52w) / rng * 100, 4)


def calc_support_resistance(closes, window=5, zone_pct=0.02):
    """
    Nearest support and resistance levels using local pivot detection.

    Returns:
        dict with keys:
            nearest_support_pct     (float | None) — % price is above support
            nearest_resistance_pct  (float | None) — % price is below resistance
    """
    price  = float(closes.iloc[-1])
    prices = closes.values

    pivot_highs = []
    pivot_lows  = []

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
            pivot_highs.append(float(prices[i]))
        if is_low:
            pivot_lows.append(float(prices[i]))

    def group_zones(pivots):
        if not pivots:
            return []
        zones = []
        for p in sorted(pivots):
            if not zones or abs(p - zones[-1]) / zones[-1] > zone_pct:
                zones.append(p)
        return zones

    supports    = [z for z in group_zones(pivot_lows)  if z < price]
    resistances = [z for z in group_zones(pivot_highs) if z > price]

    nearest_support    = max(supports)    if supports    else None
    nearest_resistance = min(resistances) if resistances else None

    support_pct    = round((price - nearest_support) / price * 100, 4)    if nearest_support    else None
    resistance_pct = round((nearest_resistance - price) / price * 100, 4) if nearest_resistance else None

    return {
        "nearest_support_pct":    support_pct,
        "nearest_resistance_pct": resistance_pct,
    }


def calc_candlestick(data):
    """
    Detect candlestick pattern from last 3 candles.

    Returns:
        dict with keys:
            signal  (str) — "BULLISH" | "BEARISH" | "NEUTRAL"
            pattern (str) — pattern name
    """
    if len(data) < 3:
        return {"signal": "NEUTRAL", "pattern": "NO DATA"}

    opens  = data["Open"].squeeze()
    highs  = data["High"].squeeze()
    lows   = data["Low"].squeeze()
    closes = data["Close"].squeeze()

    def body(o, c):      return abs(c - o)
    def rng(o, h, l, c): return (h - l) if h > l else 0.0001
    def is_green(o, c):  return c > o
    def is_red(o, c):    return c < o
    def is_large(b, r):  return b > r * 0.6
    def is_doji(b, r):   return b < r * 0.1

    o1, h1, l1, c1 = float(opens.iloc[-3]), float(highs.iloc[-3]), float(lows.iloc[-3]), float(closes.iloc[-3])
    o2, h2, l2, c2 = float(opens.iloc[-2]), float(highs.iloc[-2]), float(lows.iloc[-2]), float(closes.iloc[-2])
    o3, h3, l3, c3 = float(opens.iloc[-1]), float(highs.iloc[-1]), float(lows.iloc[-1]), float(closes.iloc[-1])

    body3       = body(o3, c3)
    range3      = rng(o3, h3, l3, c3)
    upper_wick3 = h3 - max(o3, c3)
    lower_wick3 = min(o3, c3) - l3

    # 3-candle patterns
    if (is_green(o1, c1) and abs(c2 - o2) < rng(o2, h2, l2, c2) * 0.3
            and is_red(o3, c3) and c3 < (o1 + c1) / 2):
        return {"signal": "BEARISH", "pattern": "EVENING STAR"}

    if (is_red(o1, c1) and abs(c2 - o2) < rng(o2, h2, l2, c2) * 0.3
            and is_green(o3, c3) and c3 > (o1 + c1) / 2):
        return {"signal": "BULLISH", "pattern": "MORNING STAR"}

    if is_red(o2, c2) and is_green(o3, c3) and o3 < c2 and c3 > o2:
        return {"signal": "BULLISH", "pattern": "BULLISH ENGULFING"}

    if is_green(o2, c2) and is_red(o3, c3) and o3 > c2 and c3 < o2:
        return {"signal": "BEARISH", "pattern": "BEARISH ENGULFING"}

    # 1-candle patterns
    if (is_green(o3, c3) and is_large(body3, range3)
            and upper_wick3 < body3 * 0.1 and lower_wick3 < body3 * 0.1):
        return {"signal": "BULLISH", "pattern": "MARUBOZU GREEN"}

    if (is_red(o3, c3) and is_large(body3, range3)
            and upper_wick3 < body3 * 0.1 and lower_wick3 < body3 * 0.1):
        return {"signal": "BEARISH", "pattern": "MARUBOZU RED"}

    if lower_wick3 > body3 * 2 and upper_wick3 < body3 * 0.5:
        return {"signal": "BULLISH", "pattern": "HAMMER"}

    if upper_wick3 > body3 * 2 and lower_wick3 < body3 * 0.5:
        return {"signal": "BEARISH", "pattern": "SHOOTING STAR"}

    if is_doji(body3, range3):
        return {"signal": "NEUTRAL", "pattern": "DOJI"}

    if is_green(o3, c3):
        return {"signal": "BULLISH", "pattern": "GREEN CANDLE"}

    return {"signal": "BEARISH", "pattern": "RED CANDLE"}


def calc_hv(closes, period=30):
    """
    Historical Volatility — annualized, 30-day default.

    Returns:
        float — HV as percentage (e.g. 25.3 means 25.3% annualized)
        None  — insufficient data
    """
    if len(closes) < period + 1:
        return None

    returns = closes.pct_change().dropna()
    hv      = returns.rolling(window=period).std().iloc[-1]

    if pd.isna(hv):
        return None

    return round(float(hv * (252 ** 0.5) * 100), 4)


def calc_volume_ratio(data):
    """
    Today's volume vs 20-day average.

    Returns:
        float — ratio as percentage (100.0 = average, 150.0 = 50% above avg)
        None  — insufficient data
    """
    if len(data) < 20:
        return None

    vols      = data["Volume"].squeeze()
    vol_today = float(vols.iloc[-1])
    avg_20d   = float(vols.iloc[-20:].mean())

    if avg_20d == 0:
        return None

    return round(vol_today / avg_20d * 100, 4)


# ══════════════════════════════════════════════════════════════════════════════
# PRICE HISTORY DOWNLOAD
# Used by scanner only. Backtest manages its own downloads.
# ══════════════════════════════════════════════════════════════════════════════

def get_price_history(ticker, period="1y", retries=3, delay=3):
    """Download OHLCV daily data for a ticker."""
    import time
    data = pd.DataFrame()
    for attempt in range(retries):
        try:
            data = yf.download(
                ticker,
                period=period,
                interval="1d",
                progress=False,
                auto_adjust=True,
                timeout=20,
            )
            if not data.empty:
                break
        except Exception:
            if attempt < retries - 1:
                time.sleep(delay)
    return data


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT FOR BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def get_raw_criteria(data, as_of_date):
    """
    Calculate all 9 criteria for a specific historical date.
    Uses a slice of pre-downloaded OHLCV data — no API calls.

    This is the ONLY function backtest.py should call.

    Args:
        data        (pd.DataFrame) — full OHLCV data downloaded by backtest.py
        as_of_date  (date)         — the date to simulate

    Returns:
        dict — raw numeric values for all 9 criteria
        None — if insufficient data for this date
    """
    # Slice data up to and including as_of_date
    mask = data.index.date <= as_of_date
    sliced = data[mask].copy()

    if len(sliced) < MIN_DATA_POINTS:
        return None

    closes = sliced["Close"].squeeze()
    price  = float(closes.iloc[-1])

    if price <= 0 or pd.isna(price):
        return None

    try:
        trend   = calc_trend_25d(closes)
        ma      = calc_moving_averages(closes)
        rsi     = calc_rsi(closes)
        w52     = calc_week_52_position(closes)
        sr      = calc_support_resistance(closes)
        candle  = calc_candlestick(sliced)
        hv      = calc_hv(closes)
        vol     = calc_volume_ratio(sliced)
    except Exception as e:
        raise ValueError(f"Criteria calculation error on {as_of_date}: {e}")

    return {
        # Metadata
        "price":  round(price, 4),
        "date":   as_of_date,

        # 1. Trend
        "trend_25d_pct":          trend,

        # 2-6. Moving averages
        "sma50":                  ma["sma50"],
        "sma200":                 ma["sma200"],
        "above_sma50":            ma["above_sma50"],
        "above_sma200":           ma["above_sma200"],
        "sma50_pct":              ma["sma50_pct"],
        "sma200_pct":             ma["sma200_pct"],
        "sma50_direction":        ma["sma50_direction"],

        # 7. RSI
        "rsi":                    rsi,

        # 8. 52-week position
        "week_52_position":       w52,

        # 9. Support / Resistance
        "nearest_support_pct":    sr["nearest_support_pct"],
        "nearest_resistance_pct": sr["nearest_resistance_pct"],

        # 10. Candlestick (stored separately as raw_extra)
        "candlestick_signal":     candle["signal"],
        "candlestick_pattern":    candle["pattern"],

        # 11. Historical Volatility
        "hv_30d":                 hv,

        # 12. Volume
        "volume_ratio":           vol,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT FOR SCANNER
# ══════════════════════════════════════════════════════════════════════════════

def get_all_criteria(ticker):
    """
    Calculate all criteria for a ticker using today's data.
    Used by scanner.py only.

    Downloads data internally — scanner does not manage downloads.

    Returns:
        dict — same structure as get_raw_criteria()
               plus iv, iv_percentile, beta, pcr, oi, fundamentals
               when available from yfinance
        None — if data unavailable or insufficient
    """
    data = get_price_history(ticker)

    if data.empty or len(data) < MIN_DATA_POINTS:
        return None

    raw = get_raw_criteria(data, datetime.date.today())

    if raw is None:
        return None

    raw["ticker"]    = ticker
    raw["timestamp"] = datetime.datetime.now().isoformat(timespec="minutes")

    # Scanner also gets IV, beta, PCR, OI, fundamentals (today only)
    old = _silence_stderr()
    try:
        tk   = yf.Ticker(ticker)
        info = tk.info

        # IV from ATM call ~30 DTE
        iv = None
        try:
            expirations = tk.options
            if expirations:
                today      = datetime.date.today()
                best_exp   = min(expirations, key=lambda e: abs(
                    (datetime.datetime.strptime(e, "%Y-%m-%d").date() - today).days - 30
                ))
                chain      = tk.option_chain(best_exp)
                calls      = chain.calls[chain.calls["strike"] > 0]
                price      = raw["price"]
                idx        = (calls["strike"] - price).abs().idxmin()
                iv         = round(float(calls.loc[idx, "impliedVolatility"]) * 100, 4)
        except Exception:
            pass

        raw["iv"]              = iv
        raw["beta"]            = round(float(info.get("beta") or 0), 4) or None
        raw["pe"]              = round(float(info.get("trailingPE") or 0), 4) or None
        raw["eps_growth_pct"]  = None
        raw["profit_margin"]   = round(float(info.get("profitMargins") or 0) * 100, 4) or None
        raw["debt_to_equity"]  = round(float(info.get("debtToEquity") or 0) / 100, 4) or None

        eps_t = info.get("trailingEps")
        eps_f = info.get("forwardEps")
        if eps_t and eps_f and eps_t != 0:
            raw["eps_growth_pct"] = round((eps_f - eps_t) / abs(eps_t) * 100, 4)

        _restore_stderr(old)

    except Exception:
        _restore_stderr(old)
        raw["iv"] = raw["beta"] = raw["pe"] = None
        raw["eps_growth_pct"] = raw["profit_margin"] = raw["debt_to_equity"] = None

    return raw