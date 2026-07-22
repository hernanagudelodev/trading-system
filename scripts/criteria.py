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
Strategy selection:      select_strategy() — determines optimal strategy per ticker
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
# TASTYTRADE SESSION
# ══════════════════════════════════════════════════════════════════════════════

_tt_session = None

def _get_tt_session():
    global _tt_session
    if _tt_session is not None:
        return _tt_session
    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not client_secret or not refresh_token:
        return None
    try:
        from tastytrade import Session as TTSession
        _tt_session = TTSession(client_secret, refresh_token)
        return _tt_session
    except Exception:
        return None


async def _fetch_market_metrics(symbols):
    from tastytrade import Session as TTSession
    from tastytrade.metrics import get_market_metrics
    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not client_secret or not refresh_token:
        return {}
    try:
        session = TTSession(client_secret, refresh_token)
        metrics = await get_market_metrics(session, symbols)
        return {m.symbol: m for m in metrics}
    except Exception as e:
        print(f"  _fetch_market_metrics error for {symbols}: {e}")
        return {}


def _get_market_metrics_sync(symbols):
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
        "sma50_direction": direction,
        "sma50_rising":    direction == "RISING",
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

    body3       = abs(c3 - o3)
    range3      = h3 - l3
    upper_wick3 = h3 - max(o3, c3)
    lower_wick3 = min(o3, c3) - l3

    if is_green(o1,c1) and is_red(o2,c2) and is_green(o3,c3) and c3 > c1:
        return {"pattern": "MORNING STAR",       "signal": "BULLISH", "sentiment": "BULLISH", "candles": 3}
    if is_red(o1,c1)   and is_green(o2,c2) and is_red(o3,c3) and c3 < c1:
        return {"pattern": "EVENING STAR",       "signal": "BEARISH", "sentiment": "BEARISH", "candles": 3}
    if is_green(o1,c1) and is_green(o2,c2) and is_green(o3,c3):
        return {"pattern": "THREE WHITE SOLDIERS","signal": "BULLISH", "sentiment": "BULLISH", "candles": 3}
    if is_red(o1,c1)   and is_red(o2,c2)   and is_red(o3,c3):
        return {"pattern": "THREE BLACK CROWS",  "signal": "BEARISH", "sentiment": "BEARISH", "candles": 3}
    if is_green(o2,c2) and is_red(o3,c3) and c3 < o2 and o3 > c2:
        return {"pattern": "BEARISH ENGULFING",  "signal": "BEARISH", "sentiment": "BEARISH", "candles": 2}
    if is_red(o2,c2)   and is_green(o3,c3) and c3 > o2 and o3 < c2:
        return {"pattern": "BULLISH ENGULFING",  "signal": "BULLISH", "sentiment": "BULLISH", "candles": 2}
    if is_green(o3,c3) and is_large(body3,range3) and upper_wick3 < body3*0.1 and lower_wick3 < body3*0.1:
        return {"pattern": "MARUBOZU GREEN",     "signal": "BULLISH", "sentiment": "BULLISH", "candles": 1}
    if is_red(o3,c3)   and is_large(body3,range3) and upper_wick3 < body3*0.1 and lower_wick3 < body3*0.1:
        return {"pattern": "MARUBOZU RED",       "signal": "BEARISH", "sentiment": "BEARISH", "candles": 1}
    if lower_wick3 > body3*2 and upper_wick3 < body3*0.5:
        return {"pattern": "HAMMER",             "signal": "BULLISH", "sentiment": "BULLISH", "candles": 1}
    if upper_wick3 > body3*2 and lower_wick3 < body3*0.5:
        return {"pattern": "SHOOTING STAR",      "signal": "BEARISH", "sentiment": "BEARISH", "candles": 1}
    if is_doji(body3, range3):
        return {"pattern": "DOJI",               "signal": "NEUTRAL", "sentiment": "NEUTRAL", "candles": 1}
    if is_green(o3, c3):
        return {"pattern": "GREEN CANDLE",       "signal": "BULLISH", "sentiment": "BULLISH", "candles": 1}
    return     {"pattern": "RED CANDLE",         "signal": "BEARISH", "sentiment": "BEARISH", "candles": 1}


# ══════════════════════════════════════════════════════════════════════════════
# VOLATILITY — Tastytrade API
# ══════════════════════════════════════════════════════════════════════════════

def get_historical_volatility(closes, period=30):
    returns = closes.pct_change().dropna()
    hv = returns.rolling(window=period).std().iloc[-1]
    return round(float(hv * (252 ** 0.5) * 100), 2)


def get_volatility_from_tastytrade(ticker):
    metrics_map = _get_market_metrics_sync([ticker])
    m = metrics_map.get(ticker)

    if m is None:
        return {
            "iv": None, "iv_30d": None, "iv_percentile": None,
            "iv_rank": None, "iv_hv_diff": None, "beta": None,
            "put_call_ratio": None, "open_interest": None,
            "earnings_date": None, "pe": None, "eps": None,
            "market_cap": None, "dividend_ex_date": None,
            "liquidity_rating": None,
        }

    def pct_to_100(val):
        if val is None:
            return None
        try:
            f = float(val)
            return round(f * 100, 1) if f <= 1.0 else round(f, 1)
        except Exception:
            return None

    def safe_float(val, multiplier=1.0, decimals=2):
        if val is None:
            return None
        try:
            return round(float(val) * multiplier, decimals)
        except (ValueError, TypeError):
            return None

    earnings_date = None
    try:
        if hasattr(m, "earnings") and m.earnings and hasattr(m.earnings, "expected_report_date"):
            earnings_date = m.earnings.expected_report_date
    except Exception:
        pass

    iv_raw   = getattr(m, "implied_volatility_index",        None)
    iv_30d   = getattr(m, "implied_volatility_30_day",       None)
    iv_pct   = getattr(m, "implied_volatility_percentile",   None)
    iv_rank  = getattr(m, "tw_implied_volatility_index_rank",None)
    iv_hv    = getattr(m, "iv_hv_30_day_difference",         None)
    beta_raw = getattr(m, "beta",                            None)
    pe_raw   = getattr(m, "price_earnings_ratio",            None)
    eps_raw  = getattr(m, "earnings_per_share",              None)
    liq_raw  = getattr(m, "liquidity_rating",                None)

    return {
        "iv":             safe_float(iv_raw,  100.0),
        "iv_30d":         safe_float(iv_30d),
        "iv_percentile":  pct_to_100(iv_pct),
        "iv_rank":        safe_float(iv_rank),
        "iv_hv_diff":     safe_float(iv_hv),
        "beta":           safe_float(beta_raw),
        "put_call_ratio": None,
        "open_interest":  None,
        "earnings_date":  earnings_date,
        "pe":             safe_float(pe_raw),
        "eps":            safe_float(eps_raw),
        "market_cap":     None,
        "dividend_ex_date": None,
        "liquidity_rating": int(liq_raw) if liq_raw is not None else None,
    }


# Legacy for backtest
def get_implied_volatility(ticker, price):
    old = _silence_stderr()
    try:
        tk          = yf.Ticker(ticker)
        expirations = tk.options
        _restore_stderr(old)
        if not expirations:
            return {"iv": None, "expiration": None}
        best_exp = _nearest_expiration(expirations, target_days=30)
        chain    = tk.option_chain(best_exp)
        calls    = chain.calls.copy()
        calls["dist"] = (calls["strike"] - price).abs()
        atm5     = calls.nsmallest(5, "dist")
        iv       = atm5["impliedVolatility"].dropna().mean()
        return {"iv": round(float(iv) * 100, 2) if pd.notna(iv) else None,
                "expiration": best_exp}
    except Exception:
        _restore_stderr(old)
        return {"iv": None, "expiration": None}


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
# EARNINGS, VOLUME, FUNDAMENTALS
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
        d    = dates[0] if isinstance(dates, list) else dates
        days = (d - datetime.date.today()).days
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
        eps_growth   = None
        if eps_trailing and eps_forward and eps_trailing != 0:
            eps_growth = round((eps_forward - eps_trailing) / abs(eps_trailing) * 100, 2)
        return {
            "pe":             round(float(pe), 2)           if pe           else None,
            "eps_trailing":   round(float(eps_trailing), 2) if eps_trailing else None,
            "eps_forward":    round(float(eps_forward), 2)  if eps_forward  else None,
            "eps_growth_pct": eps_growth,
            "debt_to_equity": round(float(de_raw) / 100, 2) if de_raw is not None else None,
            "profit_margin_pct": round(float(margin) * 100, 2) if margin else None
        }
    except Exception:
        _restore_stderr(old)
        return {
            "pe": None, "eps_trailing": None, "eps_forward": None,
            "eps_growth_pct": None, "debt_to_equity": None, "profit_margin_pct": None
        }


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


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def get_all_criteria(ticker):
    """
    Fetch and compute ALL criteria for a given ticker.
    Volatility data from Tastytrade API. Technical/fundamental from yfinance.
    Returns structured dict or None if insufficient price data.
    """
    data = get_price_history(ticker)
    if data.empty or len(data) < 50:
        return None

    closes = data["Close"].squeeze()
    price  = float(closes.iloc[-1])

    tt_vol = get_volatility_from_tastytrade(ticker)
    hv_value = get_historical_volatility(closes)

    earnings_yf = get_earnings_info(ticker)
    if tt_vol.get("earnings_date"):
        days_to_earn = (tt_vol["earnings_date"] - datetime.date.today()).days
        if 0 < days_to_earn <= 365:
            earnings_info = {"days_to_earnings": days_to_earn, "is_etf": False}
        else:
            earnings_info = earnings_yf
    else:
        earnings_info = earnings_yf

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
            # Legacy keys for scoring.py / backtest compatibility
            "implied":              {"iv": tt_vol["iv"], "expiration": None},
            "iv_percentile_legacy": {"percentile": tt_vol["iv_percentile"]},
        },

        "earnings":    earnings_info,
        "volume":      get_volume(data),
        "fundamental": fund_yf,
    }


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY SELECTION
# ══════════════════════════════════════════════════════════════════════════════

def select_strategy(criteria):
    """
    Determine optimal options strategy based on IV percentile and context.

        IV < 30%    → Long Call (if Beta >1.5 + trend >10% + RSI <65)
                    → Bull Call Spread (all other cases)
        30-60%      → Bull Call Spread
        IV >= 60%   → Bull Put Spread (sell expensive premium)

    Returns: 'Long Call' | 'Bull Call Spread' | 'Bull Put Spread'
    """
    if criteria is None:
        return "Bull Call Spread"

    vol  = criteria.get("volatility", {})
    tech = criteria.get("technical", {})

    ivp = vol.get("iv_percentile")
    if isinstance(ivp, dict):
        ivp = ivp.get("percentile")

    beta      = vol.get("beta") or 0
    trend     = tech.get("trend_25d", {})
    rsi       = tech.get("rsi") or 50
    trend_pct = abs(trend.get("pct_change", 0))

    if ivp is None:
        return "Bull Call Spread"

    if ivp >= 60:
        return "Bull Put Spread"
    elif ivp < 30:
        # beta 1.2 (antes 1.5): abre un poco más de Long Calls manteniendo la
        # exigencia de IV baja + momentum. Sigue siendo la estrategia más
        # arriesgada (prima completa a riesgo), por eso el resto de filtros se mantiene.
        if beta > 1.2 and trend_pct > 10 and rsi < 65:
            return "Long Call"
        return "Bull Call Spread"
    else:
        return "Bull Call Spread"


# ══════════════════════════════════════════════════════════════════════════════
# HARD FILTERS
# ══════════════════════════════════════════════════════════════════════════════

def passes_hard_filters(criteria):
    """
    Apply deterministic entry conditions.
    Returns (passed: bool, reasons: list[str])

    Filters:
        1. Trend bearish        → eliminate
        2. Below SMA50          → eliminate
        2b. Below SMA200        → eliminate (rebote en tendencia bajista larga)
        3. IV percentile        → strategy-aware:
           - Buying strategies (Long Call, Bull Call Spread): eliminate if IV > 80%
           - Selling strategies (Bull Put Spread): eliminate only if IV > 95% (extreme)
        4. Earnings < 21 days   → IV crush risk (all strategies)
        5. Open interest < 200  → insufficient liquidity
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

    # 2. SMA50 y SMA200 — el precio tiene que estar sobre AMBAS.
    #
    # POR QUÉ SMA200 TAMBIÉN — VEEV, 22-jul-2026
    #     Sólo SMA50 + trend 25d deja pasar el REBOTE dentro de una tendencia
    #     bajista larga. VEEV entró con trend 25d +16.6% (alcista) y sobre SMA50,
    #     estando -21.7% desde enero y BAJO la SMA200. La ventana de 25 días vio
    #     fuerza donde había un rebote técnico. El reporte ya lo mostraba como
    #     'MAs: Mixed' — el dato estaba a la vista y el LLM abrió igual.
    #     Las reglas duras van en código, no en el prompt.
    #
    #     El dato ya existía: get_moving_averages() calcula above_sma200 desde
    #     siempre; este filtro simplemente lo usa.
    ma = tech.get("moving_averages", {})
    if not ma.get("above_sma50", False):
        reasons.append("Below SMA50")

    # NaN explícito, no rechazo silencioso: get_all_criteria acepta tickers con
    # >=50 días de historia, pero SMA200 necesita 200. Con menos, sma200 es NaN
    # y `price > NaN` da False — rechazaría por "Below SMA200" a una acción que
    # sólo carece de historia. El motivo del rechazo no puede mentir.
    sma200 = ma.get("sma200")
    if sma200 is None or pd.isna(sma200):
        reasons.append("SMA200 no disponible (histórico < 200 días)")
    elif not ma.get("above_sma200", False):
        reasons.append("Below SMA200 — rebote dentro de tendencia bajista larga")

    # 3. IV percentile — strategy-aware
    ivp = vol.get("iv_percentile")
    if isinstance(ivp, dict):
        ivp = ivp.get("percentile")

    if ivp is not None:
        strategy = select_strategy(criteria)
        if strategy == "Bull Put Spread":
            # For selling: only extreme IV is a problem
            if ivp > 95:
                reasons.append(f"IV percentile extreme (P{ivp:.0f}) — even selling is risky")
        else:
            # For buying: eliminate if IV > 80%
            if ivp > 80:
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