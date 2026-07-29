"""
universe.py
===========
Provides the ticker universe for the scanner.

Instead of a fixed list of 32 hand-picked tickers, this module:
    1. Fetches the full S&P 500 constituent list
    2. Applies a FAST pre-filter (price + SMA50 only, no API calls)
    3. Returns a reduced candidate list for the full scanner

The fast pre-filter uses ONLY price history from yfinance — no Tastytrade,
no option chains, no fundamentals. This keeps it fast enough to run on
500 tickers in a few minutes.

Two-stage funnel:
    S&P 500 (500)  →  fast pre-filter  →  ~150-250 candidates  →  full scanner

Usage:
    from universe import get_sp500_tickers, fast_prefilter

    all_tickers = get_sp500_tickers()
    candidates  = fast_prefilter(all_tickers)  # bullish + above SMA50
"""

import io
import sys
import time
import warnings

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")


# ══════════════════════════════════════════════════════════════════════════════
# S&P 500 CONSTITUENT LIST
# ══════════════════════════════════════════════════════════════════════════════

SP500_GITHUB_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
SP500_WIKI_URL   = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Cache file to avoid re-fetching every run
SP500_CACHE = "sp500_tickers.txt"


def get_sp500_tickers(use_cache=True):
    """
    Fetch the current S&P 500 constituent tickers.

    Tries GitHub CSV first, then Wikipedia with headers, then cache.
    Caches the result to sp500_tickers.txt.

    Returns:
        list[str] — ticker symbols (e.g. ['AAPL', 'MSFT', ...])
    """
    import os

    # Try cache first if requested
    if use_cache and os.path.exists(SP500_CACHE):
        cache_age = time.time() - os.path.getmtime(SP500_CACHE)
        if cache_age < 7 * 24 * 3600:
            with open(SP500_CACHE) as f:
                tickers = [line.strip() for line in f if line.strip()]
            if tickers:
                print(f"  S&P 500 from cache: {len(tickers)} tickers")
                return tickers

    # ── Try GitHub CSV first (most reliable) ──────────────────────────────────
    try:
        import urllib.request
        req = urllib.request.Request(
            SP500_GITHUB_CSV,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read().decode("utf-8")

        df = pd.read_csv(io.StringIO(content))
        tickers = df["Symbol"].tolist()
        tickers = [t.replace(".", "-").strip() for t in tickers]

        with open(SP500_CACHE, "w") as f:
            f.write("\n".join(tickers))

        print(f"  S&P 500 from GitHub: {len(tickers)} tickers")
        return tickers
    except Exception as e:
        print(f"  GitHub CSV failed: {e}")

    # ── Try Wikipedia with headers ────────────────────────────────────────────
    try:
        import urllib.request
        req = urllib.request.Request(
            SP500_WIKI_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")

        tables = pd.read_html(io.StringIO(html))
        df = tables[0]
        tickers = df["Symbol"].tolist()
        tickers = [t.replace(".", "-").strip() for t in tickers]

        with open(SP500_CACHE, "w") as f:
            f.write("\n".join(tickers))

        print(f"  S&P 500 from Wikipedia: {len(tickers)} tickers")
        return tickers
    except Exception as e:
        print(f"  Wikipedia failed: {e}")

    # ── Fall back to stale cache ──────────────────────────────────────────────
    if os.path.exists(SP500_CACHE):
        with open(SP500_CACHE) as f:
            tickers = [line.strip() for line in f if line.strip()]
        print(f"  Using stale cache: {len(tickers)} tickers")
        return tickers

    return []


# ══════════════════════════════════════════════════════════════════════════════
# FAST PRE-FILTER — price + SMA50 only
# ══════════════════════════════════════════════════════════════════════════════

def fast_prefilter(tickers, batch_size=50, min_price=5.0, max_price=2000.0,
                   top_n=50):
    """
    Apply a fast pre-filter using only price history, then rank by momentum.

    Filters (pass/fail):
        1. Price between min_price and max_price
        2. Bullish trend (price > price 25 days ago)
        3. Above SMA50

    Then RANKS survivors by a momentum score and returns only the top_n.

    Momentum score = combination of:
        - % above SMA50 (trend strength)
        - 25-day price change (recent momentum)
        - penalized if RSI > 70 (avoid overbought)

    Args:
        tickers    (list[str]) — tickers to filter
        batch_size (int)       — how many to download per batch
        min_price  (float)     — minimum stock price
        max_price  (float)     — maximum stock price
        top_n      (int)       — max tickers to return after ranking

    Returns:
        list[str] — top_n tickers ranked by momentum (strongest first)
    """
    scored = []
    total  = len(tickers)

    print(f"\n  Fast pre-filter on {total} tickers (batch size {batch_size})...")

    for i in range(0, total, batch_size):
        batch = tickers[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size

        print(f"  Batch {batch_num}/{total_batches} "
              f"({len(batch)} tickers)...", end=" ", flush=True)

        try:
            old_stderr = sys.stderr
            sys.stderr = io.StringIO()

            data = yf.download(
                batch,
                period="3mo",
                interval="1d",
                progress=False,
                group_by="ticker",
                timeout=30,
                threads=True,
            )

            sys.stderr = old_stderr

            batch_passed = 0
            for ticker in batch:
                try:
                    if len(batch) == 1:
                        closes = data["Close"]
                    else:
                        closes = data[ticker]["Close"]

                    closes = closes.dropna()
                    if len(closes) < 50:
                        continue

                    price     = float(closes.iloc[-1])
                    price_25d = float(closes.iloc[-25])
                    sma50     = float(closes.rolling(window=50).mean().iloc[-1])

                    # Pass/fail filters
                    if not (min_price <= price <= max_price):
                        continue
                    if price <= price_25d:
                        continue
                    if price <= sma50:
                        continue

                    # ── Momentum score ────────────────────────────────────────
                    pct_above_sma50 = (price - sma50) / sma50 * 100
                    pct_change_25d  = (price - price_25d) / price_25d * 100

                    # RSI (14)
                    delta      = closes.diff()
                    gains      = delta.where(delta > 0, 0).rolling(14).mean()
                    losses     = (-delta.where(delta < 0, 0)).rolling(14).mean()
                    rs         = gains.iloc[-1] / losses.iloc[-1] if losses.iloc[-1] != 0 else 0
                    rsi        = 100 - (100 / (1 + rs)) if rs else 50

                    # ── Composite momentum score ──────────────────────────────
                    # Base: trend strength + recent momentum
                    # But we want SUSTAINABLE momentum, not exhausted runners.
                    # Cap the raw momentum contribution and heavily favor
                    # healthy RSI (40-65) over overbought extremes.

                    # Cap each component to avoid extreme runners dominating
                    capped_sma50   = min(pct_above_sma50, 30)   # cap at 30%
                    capped_25d     = min(pct_change_25d, 30)    # cap at 30%
                    base_momentum  = capped_sma50 + capped_25d

                    # RSI health bonus/penalty — this is now the key driver
                    if 45 <= rsi <= 60:
                        rsi_factor = 20      # ideal zone — strong bonus
                    elif 40 <= rsi < 45 or 60 < rsi <= 65:
                        rsi_factor = 10      # good zone
                    elif 65 < rsi <= 70:
                        rsi_factor = 0       # neutral
                    elif 70 < rsi <= 75:
                        rsi_factor = -20     # overbought — penalize
                    elif rsi > 75:
                        rsi_factor = -50     # extreme — heavy penalty
                    elif 35 <= rsi < 40:
                        rsi_factor = -10     # weak
                    else:  # rsi < 35
                        rsi_factor = -30     # too weak

                    momentum = base_momentum + rsi_factor

                    scored.append({
                        "ticker":   ticker,
                        "momentum": round(momentum, 2),
                        "rsi":      round(rsi, 1),
                        "pct_25d":  round(pct_change_25d, 1),
                        "above_sma50_pct": round(pct_above_sma50, 1),
                    })
                    batch_passed += 1

                except Exception:
                    continue

            print(f"{batch_passed} passed")

        except Exception as e:
            sys.stderr = old_stderr
            print(f"batch error: {e}")
            continue

        time.sleep(1)

    # ── Rank by momentum, take top_n ──────────────────────────────────────────
    scored.sort(key=lambda x: x["momentum"], reverse=True)
    top = scored[:top_n]

    print(f"\n  Fast pre-filter: {len(scored)} passed filters, "
          f"taking top {len(top)} by momentum")
    print(f"\n  Top 10 by momentum:")
    for s in top[:10]:
        print(f"    {s['ticker']:<6} momentum {s['momentum']:>6.1f} | "
              f"RSI {s['rsi']:>5.1f} | +{s['pct_25d']:>5.1f}% 25d | "
              f"+{s['above_sma50_pct']:>5.1f}% vs SMA50")

    return [s["ticker"] for s in top]


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE — full universe pipeline
# ══════════════════════════════════════════════════════════════════════════════

def get_scanner_candidates(use_cache=True):
    """
    Full pipeline: S&P 500 → fast pre-filter → candidates.

    Returns:
        list[str] — candidate tickers ready for the full scanner
    """
    tickers    = get_sp500_tickers(use_cache=use_cache)
    if not tickers:
        return []
    candidates = fast_prefilter(tickers)
    return candidates


if __name__ == "__main__":
    # Test the pipeline
    candidates = get_scanner_candidates()
    print(f"\n  Candidates for full scan: {len(candidates)}")
    print(f"  {', '.join(candidates[:30])}{'...' if len(candidates) > 30 else ''}")