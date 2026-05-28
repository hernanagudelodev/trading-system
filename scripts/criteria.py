"""
criteria.py
===========
Pure data extraction module for options trading system.

This file is responsible ONLY for fetching and calculating raw market data.
It does NOT score, rate, or make any trading decisions.

Data sources:
    yfinance        → price history, technicals, fundamentals, earnings
    Tastytrade API  → IV exact, IV percentile, IV rank, put/call ratio,
                      open interest, beta (more accurate than yfinance)

Scoring logic lives in:  scoring.py
Hard filters live in:    passes_hard_filters() — bottom of this file
"""

import sys
import io
import asyncio
import datetime
import warnings
import os

import yfinance as yf
import pandas as pd

warnings.filterwarnings("ignore")


# ══════════════════════════════════════════════════════════════════════════════
# TASTYTRADE SESSION — singleton to avoid re-authenticating per ticker
# ══════════════════════════════════════════════════════════════════════════════

_tt_session = None

def _get_tt_session():
    """
    Return a cached Tastytrade session, creating one if needed.
    Uses asyncio.run() to stay compatible with synchronous callers.
    """
    global _tt_session
    if _tt_session is not None:
        return _tt_session

    client_secret   = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token   = os.getenv("TASTYTRADE_REFRESH_TOKEN")

    if not client_secret or not refresh_token:
        return None

    try:
        from tastytrade import Session as TTSession
        _tt_session = TTSession(client_secret, refresh_token)
        return _tt_session
    except Exception:
        return None


async def _fetch_market_metrics(symbols):
    """
    Async helper — fetch MarketMetricInfo for a list of symbols.
    Returns dict: {symbol: MarketMetricInfo}
    """
    from tastytrade.metrics import get_market_metrics
    session = _get_tt_session()
    if session is None:
        return {}
    try:
        metrics = await get_market_metrics(session, symbols)
        return {m.symbol: m for m in metrics}
    except Exception:
        return {}


def _get_market_metrics_sync(symbols):
    """
    Synchronous wrapper around _fetch_market_metrics.
    Safe to call from synchronous code.
    """
    try:
        return asyncio.run(_fetch_market_metrics(symbols))
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _silence_stderr():
    old = sys.stderr
    sys.stderr = io.StringIO()
    return old

def _restore_stderr(old):
    sys.stderr = old

def _nearest_expiration(expirations, target_days=30):
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
# PRICE HISTORY
# ══════════════════════════════════════════════════════════════════════════════

def get_price_history(ticker, retries=3, delay=3):
    """Download 1 year of daily OHLCV data from yfinance."""
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
# TECHNICAL INDICATORS  (unchanged — still from yfinance price history)
# ══════════════════════════════════════════════════════════════════════════════

def get_trend_25d(closes):
    price_now = float(closes.iloc[-1])
    price_25d = float(closes.iloc[-25])
    pct = (price_now - price_25d) / price_25d * 100
    return {
        "price_now":  price_now,
        "price_25d":  price_25d,
        "pct_change": round(pct, 2),
        "is_bullish": price_now > price_25d
    }


def get_moving_averages(closes):
    sma50_series  = closes.rolling(window=50).mean()
    sma200_series = closes.rolling(window=200).mean()

    sma50  = float(sma50_series.iloc[-1])
    sma200 = float(sma200_series.iloc[-1])
    price  = float(closes.iloc[-1])

    sma50_10d = float(sma50_series.iloc[-10])
    if sma50 > sma50_10d * 1.001:
        direction = "RISING"
    elif sma50 < sma50_10d * 0.999:
        direction = "FALLING"
    else:
        direction = "FLAT"

    return {
        "sma50":           round(sma50, 2),
        "sma200":          round(sma200, 2),
        "above_sma50":     price > sma50,
        "above_sma200":    price > sma200,
        "sma50_direction": direction
    }


def get_rsi(closes, period=14):
    delta      = closes.diff()
    gains      = delta.where(delta > 0, 0)
    losses     = -delta.where(delta < 0, 0)
    avg_gains  = gains.rolling(window=period).mean()
    avg_losses = losses.rolling(window=period).mean()
    rs  = avg_gains / avg_losses
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def get_52_week_position(closes, price):
    max_52w = float(closes.tail(252).max())
    min_52w = float(closes.tail(252).min())
    rng     = max_52w - min_52w
    pos     = (price - min_52w) / rng * 100 if rng > 0 else 50.0

    if pos >= 95:
        tag = "near high"
    elif pos <= 10:
        tag = "near low"
    else:
        tag = "mid range"

    return {
        "max_52w":      round(max_52w, 2),
        "min_52w":      round(min_52w, 2),
        "position_pct": round(pos, 1),
        "near_high":    price >= max_52w * 0.95,
        "near_low":     price <= min_52w * 1.05,
        "tag":          tag
    }


def get_support_resistance(closes, price, window=5, zone_pct=0.02):
    prices = closes.values
    highs, lows = [], []

    for i in range(window, len(prices) - window):
        if all(prices[i] >= prices[i-j] for j in range(1, window+1)) and \
           all(prices[i] >= prices[i+j] for j in range(1, window+1)):
            highs.append(prices[i])
        if all(prices[i] <= prices[i-j] for j in range(1, window+1)) and \
           all(prices[i] <= prices[i+j] for j in range(1, window+1)):
            lows.append(prices[i])

    def cluster(levels, zone_pct):
        if not levels:
            return []
        levels = sorted(levels)
        zones, current = [], [levels[0]]
        for level in levels[1:]:
            if (level - current[0]) / current[0] <= zone_pct:
                current.append(level)
            else:
                zones.append(sum(current) / len(current))
                current = [level]
        zones.append(sum(current) / len(current))
        return zones

    support_zones    = cluster(lows,  zone_pct)
    resistance_zones = cluster(highs, zone_pct)

    support    = max((z for z in support_zones    if z < price), default=None)
    resistance = min((z for z in resistance_zones if z > price), default=None)

    support_pct    = round((price - support)    / price * 100, 1) if support    else None
    resistance_pct = round((resistance - price) / price * 100, 1) if resistance else None

    return {
        "support":         round(support,    2) if support    else None,
        "resistance":      round(resistance, 2) if resistance else None,
        "support_pct":     support_pct,
        "resistance_pct":  resistance_pct,
    }


def get_candlestick_pattern(data):
    def is_green(o, c): return c > o
    def is_red(o, c):   return c < o
    def is_large(body, rng): return body > rng * 0.6 if rng > 0 else False
    def is_doji(body, rng):  return body < rng * 0.1 if rng > 0 else False

    opens  = data["Open"].squeeze()
    highs  = data["High"].squeeze()
    lows   = data["Low"].squeeze()
    closes = data["Close"].squeeze()

    o1, h1, l1, c1 = float(opens.iloc[-3]), float(highs.iloc[-3]), float(lows.iloc[-3]), float(closes.iloc[-3])
    o2, h2, l2, c2 = float(opens.iloc[-2]), float(highs.iloc[-2]), float(lows.iloc[-2]), float(closes.iloc[-2])
    o3, h3, l3, c3 = float(opens.iloc[-1]), float(highs.iloc[-1]), float(lows.iloc[-1]), float(closes.iloc[-1])

    body3        = abs(c3 - o3)
    range3       = h3 - l3
    upper_wick3  = h3 - max(o3, c3)
    lower_wick3  = min(o3, c3) - l3

    # 3-candle patterns
    if is_green(o1,c1) and is_red(o2,c2) and is_green(o3,c3) and c3 > c1:
        return {"pattern": "MORNING STAR",      "signal": "BULLISH",  "sentiment": "BULLISH", "candles": 3}
    if is_red(o1,c1)   and is_green(o2,c2) and is_red(o3,c3)   and c3 < c1:
        return {"pattern": "EVENING STAR",      "signal": "BEARISH",  "sentiment": "BEARISH", "candles": 3}
    if is_green(o1,c1) and is_green(o2,c2) and is_green(o3,c3):
        return {"pattern": "THREE WHITE SOLDIERS","signal": "BULLISH", "sentiment": "BULLISH", "candles": 3}
    if is_red(o1,c1)   and is_red(o2,c2)   and is_red(o3,c3):
        return {"pattern": "THREE BLACK CROWS",  "signal": "BEARISH",  "sentiment": "BEARISH", "candles": 3}

    # 2-candle patterns
    body2  = abs(c2 - o2)
    range2 = h2 - l2
    if is_green(o2,c2) and is_red(o3,c3) and c3 < o2 and o3 > c2:
        return {"pattern": "BEARISH ENGULFING",  "signal": "BEARISH",  "sentiment": "BEARISH", "candles": 2}
    if is_red(o2,c2)   and is_green(o3,c3) and c3 > o2 and o3 < c2:
        return {"pattern": "BULLISH ENGULFING",  "signal": "BULLISH",  "sentiment": "BULLISH", "candles": 2}

    # 1-candle patterns
    if is_green(o3,c3) and is_large(body3,range3) and upper_wick3 < body3*0.1 and lower_wick3 < body3*0.1:
        return {"pattern": "MARUBOZU GREEN",    "signal": "BULLISH",  "sentiment": "BULLISH", "candles": 1}
    if is_red(o3,c3)   and is_large(body3,range3) and upper_wick3 < body3*0.1 and lower_wick3 < body3*0.1:
        return {"pattern": "MARUBOZU RED",      "signal": "BEARISH",  "sentiment": "BEARISH", "candles": 1}
    if lower_wick3 > body3*2 and upper_wick3 < body3*0.5:
        return {"pattern": "HAMMER",            "signal": "BULLISH",  "sentiment": "BULLISH", "candles": 1}
    if upper_wick3 > body3*2 and lower_wick3 < body3*0.5:
        return {"pattern": "SHOOTING STAR",     "signal": "BEARISH",  "sentiment": "BEARISH", "candles": 1}
    if is_doji(body3, range3):
        return {"pattern": "DOJI",              "signal": "NEUTRAL",  "sentiment": "NEUTRAL", "candles": 1}
    if is_green(o3, c3):
        return {"pattern": "GREEN CANDLE",      "signal": "BULLISH",  "sentiment": "BULLISH", "candles": 1}
    return     {"pattern": "RED CANDLE",        "signal": "BEARISH",  "sentiment": "BEARISH", "candles": 1}


# ══════════════════════════════════════════════════════════════════════════════
# VOLATILITY — Tastytrade API (replaces yfinance approximations)
# ══════════════════════════════════════════════════════════════════════════════

def get_historical_volatility(closes, period=30):
    """HV 30d — still calculated from price history (no Tastytrade equivalent)."""
    returns = closes.pct_change().dropna()
    hv = returns.rolling(window=period).std().iloc[-1]
    return round(float(hv * (252 ** 0.5) * 100), 2)


def get_volatility_from_tastytrade(ticker):
    """
    Fetch all volatility metrics from Tastytrade API in a single call.

    Returns dict with:
        iv              (float | None)  — IV index (current implied volatility)
        iv_30d          (float | None)  — 30-day IV
        iv_percentile   (float | None)  — IV percentile 0-100
        iv_rank         (float | None)  — TW IV rank 0-1
        iv_hv_diff      (float | None)  — IV minus HV (positive = IV expensive)
        beta            (float | None)  — beta vs S&P 500
        put_call_ratio  (float | None)  — put/call ratio
        open_interest   (int   | None)  — total OI near ATM
        earnings_date   (date  | None)  — next earnings date
        pe              (float | None)  — P/E ratio
        eps             (float | None)  — EPS
        market_cap      (float | None)  — market cap
        dividend_ex_date(date  | None)  — ex-dividend date
    """
    metrics_map = _get_market_metrics_sync([ticker])
    m = metrics_map.get(ticker)

    if m is None:
        return {
            "iv": None, "iv_30d": None, "iv_percentile": None,
            "iv_rank": None, "iv_hv_diff": None, "beta": None,
            "put_call_ratio": None, "open_interest": None,
            "earnings_date": None, "pe": None, "eps": None,
            "market_cap": None, "dividend_ex_date": None,
        }

    # IV percentile comes as string "0.84" → convert to 0-100
    def pct_to_100(val):
        if val is None:
            return None
        try:
            f = float(val)
            return round(f * 100, 1) if f <= 1.0 else round(f, 1)
        except Exception:
            return None

    # Earnings date
    earnings_date = None
    if m.earnings and m.earnings.expected_report_date:
        earnings_date = m.earnings.expected_report_date

    return {
        "iv":             round(float(m.implied_volatility_index) * 100, 2)   if m.implied_volatility_index  else None,
        "iv_30d":         round(float(m.implied_volatility_30_day), 2)        if m.implied_volatility_30_day else None,
        "iv_percentile":  pct_to_100(m.implied_volatility_percentile),
        "iv_rank":        round(float(m.tw_implied_volatility_index_rank), 3) if m.tw_implied_volatility_index_rank else None,
        "iv_hv_diff":     round(float(m.iv_hv_30_day_difference), 2)          if m.iv_hv_30_day_difference   else None,
        "beta":           round(float(m.beta), 2)                              if m.beta                      else None,
        "put_call_ratio":  None,   # not in MarketMetrics — fetched from option chain if needed
        "open_interest":   None,   # not in MarketMetrics — fetched from option chain if needed
        "earnings_date":  earnings_date,
        "pe":             round(float(m.price_earnings_ratio), 2)              if m.price_earnings_ratio      else None,
        "eps":            round(float(m.earnings_per_share), 4)                if m.earnings_per_share        else None,
        "market_cap":     float(m.market_cap)                                  if m.market_cap                else None,
        "dividend_ex_date": m.dividend_ex_date,
        "liquidity_rating": m.liquidity_rating,
    }


# Kept for backtest compatibility (uses yfinance approximation)
def get_implied_volatility(ticker, price):
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


# Kept for backtest compatibility
def get_iv_percentile(closes, iv_current):
    if iv_current is None:
        return {"percentile": None}
    returns    = closes.pct_change().dropna()
    hv_history = returns.rolling(window=30).std().dropna() * (252 ** 0.5) * 100
    if len(hv_history) < 30:
        return {"percentile": None}
    percentile = (hv_history < iv_current).sum() / len(hv_history) * 100
    return {"percentile": round(float(percentile), 1)}


# ══════════════════════════════════════════════════════════════════════════════
# EARNINGS, VOLUME, FUNDAMENTALS  (yfinance — unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def get_earnings_info(ticker):
    old = _silence_stderr()
    try:
        tk   = yf.Ticker(ticker)
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


def get_volume(data):
    vols      = data["Volume"].squeeze()
    vol_today = int(vols.iloc[-1])
    avg_20d   = float(vols.iloc[-20:].mean())
    ratio     = round(vol_today / avg_20d * 100, 1) if avg_20d > 0 else 0.0
    return {
        "volume_today":    vol_today,
        "volume_avg_20d":  round(avg_20d, 0),
        "volume_ratio_pct": ratio
    }


def get_fundamentals(ticker):
    old = _silence_stderr()
    try:
        tk   = yf.Ticker(ticker)
        info = tk.info
        _restore_stderr(old)

        pe           = info.get("trailingPE") or info.get("forwardPE")
        eps_trailing = info.get("trailingEps")
        eps_forward  = info.get("forwardEps")
        de_raw       = info.get("debtToEquity")
        margin       = info.get("profitMargins")

        eps_growth = None
        if eps_trailing and eps_forward and eps_trailing != 0:
            eps_growth = round(
                (eps_forward - eps_trailing) / abs(eps_trailing) * 100, 2
            )

        return {
            "pe":             round(float(pe), 2)           if pe           else None,
            "eps_trailing":   round(float(eps_trailing), 2) if eps_trailing else None,
            "eps_forward":    round(float(eps_forward), 2)  if eps_forward  else None,
            "eps_growth_pct": eps_growth,
            "debt_to_equity": round(float(de_raw) / 100, 2) if de_raw is not None else None,
            "profit_margin_pct": round(float(margin) * 100, 2) if margin   else None
        }
    except Exception:
        _restore_stderr(old)
        return {
            "pe": None, "eps_trailing": None, "eps_forward": None,
            "eps_growth_pct": None, "debt_to_equity": None, "profit_margin_pct": None
        }


def get_beta(ticker):
    old = _silence_stderr()
    try:
        tk   = yf.Ticker(ticker)
        beta = tk.info.get("beta")
        _restore_stderr(old)
        return round(float(beta), 2) if beta is not None else None
    except Exception:
        _restore_stderr(old)
        return None


def get_put_call_ratio(ticker, price):
    old = _silence_stderr()
    try:
        tk          = yf.Ticker(ticker)
        expirations = tk.options
        _restore_stderr(old)
        if not expirations:
            return {"pcr": None, "call_volume": 0, "put_volume": 0, "expiration": None}
        best_exp = _nearest_expiration(expirations, target_days=30)
        chain    = tk.option_chain(best_exp)
        call_vol = int(chain.calls["volume"].fillna(0).sum())
        put_vol  = int(chain.puts["volume"].fillna(0).sum())
        pcr      = round(put_vol / call_vol, 3) if call_vol > 0 else None
        return {"pcr": pcr, "call_volume": call_vol, "put_volume": put_vol, "expiration": best_exp}
    except Exception:
        _restore_stderr(old)
        return {"pcr": None, "call_volume": 0, "put_volume": 0, "expiration": None}


def get_open_interest(ticker, price):
    old = _silence_stderr()
    try:
        tk          = yf.Ticker(ticker)
        expirations = tk.options
        _restore_stderr(old)
        if not expirations:
            return {"oi": None, "expiration": None}
        best_exp = _nearest_expiration(expirations, target_days=30)
        chain    = tk.option_chain(best_exp)
        calls    = chain.calls.copy()
        calls["dist"] = (calls["strike"] - price).abs()
        atm5     = calls.nsmallest(5, "dist")
        oi       = int(atm5["openInterest"].fillna(0).sum())
        return {"oi": oi, "expiration": best_exp}
    except Exception:
        _restore_stderr(old)
        return {"oi": None, "expiration": None}


# ══════════════════════════════════════════════════════════════════════════════
# HARD FILTERS — deterministic, no weights, no scoring
# ══════════════════════════════════════════════════════════════════════════════

def passes_hard_filters(criteria):
    """
    Apply 5 deterministic entry conditions.
    Returns (passed: bool, reasons: list[str])

    Filters:
        1. Trend bearish          → eliminate
        2. Below SMA50            → eliminate
        3. IV percentile > 80%    → options too expensive for buyers
        4. Earnings < 21 days     → IV crush risk
        5. Open interest < 500    → insufficient liquidity
    """
    if criteria is None:
        return False, ["No data"]

    tech = criteria.get("technical", {})
    vol  = criteria.get("volatility", {})
    earn = criteria.get("earnings", {})

    reasons = []

    # 1. Trend
    trend = tech.get("trend_25d", {})
    if not trend.get("is_bullish", False):
        reasons.append(f"Bearish trend ({trend.get('pct_change', 0):+.1f}% 25d)")

    # 2. SMA50
    ma = tech.get("moving_averages", {})
    if not ma.get("above_sma50", False):
        reasons.append("Below SMA50")

    # 3. IV percentile
    ivp = vol.get("iv_percentile")
    if isinstance(ivp, dict):
        ivp = ivp.get("percentile")
    if ivp is not None and ivp > 80:
        reasons.append(f"IV percentile too high (P{ivp:.0f})")

    # 4. Earnings
    days_to_earn = earn.get("days_to_earnings")
    if days_to_earn is not None and days_to_earn < 21:
        reasons.append(f"Earnings in {days_to_earn}d — IV crush risk")

    # 5. Open interest
    oi_data = vol.get("open_interest", {})
    if isinstance(oi_data, dict):
        oi = oi_data.get("oi")
    else:
        oi = oi_data
    if oi is not None and oi < 200:
        reasons.append(f"Low open interest ({oi:,})")

    passed = len(reasons) == 0
    return passed, reasons


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def get_all_criteria(ticker):
    """
    Fetch and compute ALL criteria for a given ticker.

    Volatility data comes from Tastytrade API (exact).
    Technical, fundamental, earnings, volume from yfinance.

    Returns structured dict or None if insufficient price data.
    """
    data = get_price_history(ticker)

    if data.empty or len(data) < 50:
        return None

    closes = data["Close"].squeeze()
    price  = float(closes.iloc[-1])

    # ── Tastytrade volatility (single API call per ticker) ────────────────────
    tt_vol = get_volatility_from_tastytrade(ticker)

    # ── Historical volatility (calculated locally) ────────────────────────────
    hv_value = get_historical_volatility(closes)

    # ── Earnings: prefer Tastytrade date, fallback to yfinance ────────────────
    earnings_yf = get_earnings_info(ticker)
    if tt_vol.get("earnings_date"):
        days_to_earn = (tt_vol["earnings_date"] - datetime.date.today()).days
        if 0 < days_to_earn <= 365:
            earnings_info = {"days_to_earnings": days_to_earn, "is_etf": False}
        else:
            earnings_info = earnings_yf
    else:
        earnings_info = earnings_yf

    # ── Fundamentals: prefer Tastytrade PE/EPS, fill rest from yfinance ───────
    fund_yf = get_fundamentals(ticker)
    if tt_vol.get("pe") is not None:
        fund_yf["pe"] = tt_vol["pe"]
    if tt_vol.get("eps") is not None:
        fund_yf["eps_trailing"] = tt_vol["eps"]

    return {
        "ticker":    ticker,
        "timestamp": datetime.datetime.now().isoformat(timespec="minutes"),
        "price":     round(price, 2),

        "technical": {
            "trend_25d":          get_trend_25d(closes),
            "moving_averages":    get_moving_averages(closes),
            "rsi":                get_rsi(closes),
            "week_52":            get_52_week_position(closes, price),
            "support_resistance": get_support_resistance(closes, price),
            "candlestick":        get_candlestick_pattern(data),
        },

        "volatility": {
            "hv_30d":          hv_value,
            "iv":              tt_vol["iv"],
            "iv_30d":          tt_vol["iv_30d"],
            "iv_percentile":   tt_vol["iv_percentile"],
            "iv_rank":         tt_vol["iv_rank"],
            "iv_hv_diff":      tt_vol["iv_hv_diff"],
            "beta":            tt_vol["beta"] or get_beta(ticker),
            "put_call_ratio":  get_put_call_ratio(ticker, price).get("pcr"),
            "open_interest":   get_open_interest(ticker, price).get("oi"),
            "liquidity_rating": tt_vol.get("liquidity_rating"),

            # Legacy keys — kept for scoring.py / backtest compatibility
            "implied":               {"iv": tt_vol["iv"], "expiration": None},
            "iv_percentile_legacy":  {"percentile": tt_vol["iv_percentile"]},
        },

        "earnings":    earnings_info,
        "volume":      get_volume(data),
        "fundamental": fund_yf,
    }