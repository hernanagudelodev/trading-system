"""
option_selector.py
==================
Selects optimal option strikes for tickers that passed hard filters.

For each ticker:
    1. Fetches option chain from Tastytrade API
    2. Finds expirations in DTE range (20-40 days)
    3. Filters strikes with Delta 0.40-0.60
    4. Calculates Expected Value per strike
    5. Returns compact markdown for AI interpretation

Called by scanner.py after passes_hard_filters().

Usage:
    from option_selector import get_options_for_tickers
    markdown = get_options_for_tickers(session, tickers_dict)

Dependencies:
    tastytrade SDK
    Tastytrade session (passed in — no re-auth)
"""

import asyncio
import os
from datetime import date, datetime
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DTE_MIN       = 20       # minimum days to expiration
DTE_MAX       = 40       # maximum days to expiration
DELTA_MIN     = 0.35     # minimum delta (slightly below 0.40 for flexibility)
DELTA_MAX     = 0.65     # maximum delta (slightly above 0.60 for flexibility)
DELTA_IDEAL_LOW  = 0.40  # ideal delta range low
DELTA_IDEAL_HIGH = 0.60  # ideal delta range high
MAX_STRIKES   = 5        # max strikes to show per ticker
EV_MIN        = 0        # only show strikes with positive expected value


# ══════════════════════════════════════════════════════════════════════════════
# ASYNC CORE — fetch option chain + Greeks for one ticker
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_option_data(session, ticker, price):
    """
    Fetch option chain with real-time Greeks for a single ticker.

    Returns list of dicts with strike data, or empty list on failure.
    """
    from tastytrade.instruments import NestedOptionChain
    from tastytrade.dxfeed import Greeks, Quote
    from tastytrade import DXLinkStreamer

    try:
        # ── Get option chain ──────────────────────────────────────────────────
        chains = await NestedOptionChain.get(session, ticker)
        if not chains:
            return []
        chain = chains[0]

        # ── Find best expiration in DTE range ─────────────────────────────────
        target_exp = None
        best_diff  = 9999
        for exp in chain.expirations:
            dte = exp.days_to_expiration
            if DTE_MIN <= dte <= DTE_MAX:
                # Prefer DTE closest to 30 days
                diff = abs(dte - 30)
                if diff < best_diff:
                    best_diff  = diff
                    target_exp = exp

        if target_exp is None:
            return []

        dte_selected = target_exp.days_to_expiration
        exp_date     = target_exp.expiration_date

        # ── Get all strikes ───────────────────────────────────────────────────
        # Filter to strikes within ±20% of current price to limit API calls
        price_low  = price * 0.80
        price_high = price * 1.20

        candidate_strikes = [
            s for s in target_exp.strikes
            if price_low <= float(s.strike_price) <= price_high
        ]

        if not candidate_strikes:
            return []

        call_symbols = [s.call_streamer_symbol for s in candidate_strikes]

        # ── Fetch Greeks + Quotes via DXLink ──────────────────────────────────
        greeks_map = {}
        quotes_map = {}

        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Greeks, call_symbols)
            await streamer.subscribe(Quote, call_symbols)

            # Collect Greeks
            for _ in call_symbols:
                try:
                    g = await asyncio.wait_for(streamer.get_event(Greeks), timeout=10)
                    greeks_map[g.event_symbol] = g
                except asyncio.TimeoutError:
                    break

            # Collect Quotes
            for _ in call_symbols:
                try:
                    q = await asyncio.wait_for(streamer.get_event(Quote), timeout=10)
                    quotes_map[q.event_symbol] = q
                except asyncio.TimeoutError:
                    break

        # ── Build strike results ──────────────────────────────────────────────
        results = []
        for s in candidate_strikes:
            sym = s.call_streamer_symbol
            g   = greeks_map.get(sym)
            q   = quotes_map.get(sym)

            if g is None:
                continue

            delta = float(g.delta) if g.delta else None
            if delta is None:
                continue

            # Filter by delta range
            if not (DELTA_MIN <= delta <= DELTA_MAX):
                continue

            strike_price = float(s.strike_price)
            theta        = float(g.theta)      if g.theta      else None
            vega         = float(g.vega)       if g.vega       else None
            iv           = float(g.volatility) * 100 if g.volatility else None
            theo_price   = float(g.price)      if g.price      else None

            bid  = float(q.bid_price) if q and q.bid_price else 0.0
            ask  = float(q.ask_price) if q and q.ask_price else 0.0
            mid  = round((bid + ask) / 2, 2) if bid and ask else theo_price or 0.0
            spread_pct = round((ask - bid) / ask * 100, 1) if ask > 0 else None

            # ── Breakeven calculation ─────────────────────────────────────────
            # Breakeven = strike + mid (precio al que recuperas el premium)
            # Breakeven % = cuánto debe subir el subyacente para breakeven
            breakeven     = round(strike_price + mid, 2)
            breakeven_pct = round((breakeven - price) / price * 100, 2)

            # ── Profit at 50% and 70% target ─────────────────────────────────
            profit_50 = round(mid * 0.50 * 100, 0)   # cerrar al 50% del premium
            profit_70 = round(mid * 0.70 * 100, 0)   # cerrar al 70% del premium

            # ── Delta quality flag ────────────────────────────────────────────
            ideal = DELTA_IDEAL_LOW <= delta <= DELTA_IDEAL_HIGH

            results.append({
                "ticker":        ticker,
                "exp_date":      exp_date,
                "dte":           dte_selected,
                "strike":        strike_price,
                "symbol":        sym,
                "delta":         round(delta, 3),
                "theta":         round(theta, 4) if theta else None,
                "vega":          round(vega,  4) if vega  else None,
                "iv":            round(iv,    1) if iv    else None,
                "bid":           bid,
                "ask":           ask,
                "mid":           mid,
                "spread_pct":    spread_pct,
                "premium_total": round(mid * 100, 0),
                "breakeven":     breakeven,
                "breakeven_pct": breakeven_pct,
                "profit_50":     profit_50,
                "profit_70":     profit_70,
                "ideal_delta":   ideal,
                "itm":           strike_price < price,
            })

        # Sort by: ideal delta first, then by breakeven_pct ascending (less movement needed)
        results.sort(key=lambda x: (not x["ideal_delta"], x["breakeven_pct"]))
        return results[:MAX_STRIKES]

    except Exception as e:
        print(f"  option_selector error for {ticker}: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# SYNC WRAPPER — called by scanner.py (synchronous)
# ══════════════════════════════════════════════════════════════════════════════

def get_options_for_tickers(session, tickers_data):
    """
    Synchronous entry point for scanner.py.

    Args:
        session      — Tastytrade session object
        tickers_data — dict: {ticker: criteria_dict} for tickers that passed filters

    Returns:
        str — markdown report with option recommendations per ticker
    """
    try:
        return asyncio.run(_get_options_async(session, tickers_data))
    except Exception as e:
        return f"option_selector error: {e}"


async def _get_options_async(session, tickers_data):
    """Async implementation — fetch options for all tickers concurrently."""

    tasks = {
        ticker: _fetch_option_data(session, ticker, data.get("price", 0))
        for ticker, data in tickers_data.items()
    }

    results = {}
    for ticker, coro in tasks.items():
        results[ticker] = await coro

    return _build_markdown(tickers_data, results)


# ══════════════════════════════════════════════════════════════════════════════
# MARKDOWN BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_markdown(tickers_data, options_results):
    """
    Build compact markdown report for AI interpretation.
    No scores, no verdicts — just raw data.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines     = []

    lines.append(f"# Option Selector — {timestamp}")
    lines.append(f"DTE range: {DTE_MIN}-{DTE_MAX} | Delta range: {DELTA_IDEAL_LOW}-{DELTA_IDEAL_HIGH}")
    lines.append("")

    any_results = False

    for ticker, criteria in tickers_data.items():
        strikes = options_results.get(ticker, [])
        price   = criteria.get("price", 0)
        vol     = criteria.get("volatility", {})
        tech    = criteria.get("technical", {})
        earn    = criteria.get("earnings", {})

        lines.append(f"## {ticker} — ${price:.2f}")
        lines.append("")

        # ── Criteria summary ──────────────────────────────────────────────────
        trend   = tech.get("trend_25d", {})
        ma      = tech.get("moving_averages", {})
        rsi     = tech.get("rsi")
        iv      = vol.get("iv")
        ivp     = vol.get("iv_percentile")
        iv_rank = vol.get("iv_rank")
        hv      = vol.get("hv_30d")
        iv_hv   = vol.get("iv_hv_diff")
        beta    = vol.get("beta")
        pcr     = vol.get("put_call_ratio")
        oi      = vol.get("open_interest")
        days_earn = earn.get("days_to_earnings")

        lines.append("**Criteria:**")
        lines.append(
            f"Trend: {'BULLISH' if trend.get('is_bullish') else 'BEARISH'} "
            f"({trend.get('pct_change', 0):+.1f}% 25d) | "
            f"SMAs: {'Above both' if ma.get('above_sma50') and ma.get('above_sma200') else 'Above SMA50'} | "
            f"RSI: {rsi:.1f}" if rsi else "RSI: N/A"
        )
        lines.append(
            f"IV: {iv:.1f}% (P{ivp:.0f} / Rank {iv_rank:.2f}) | "
            f"HV: {hv:.1f}% | IV-HV: {iv_hv:+.1f}%"
            if all(x is not None for x in [iv, ivp, iv_rank, hv, iv_hv])
            else "IV: N/A"
        )
        lines.append(
            f"Beta: {beta:.2f} | "
            f"P/C: {pcr:.2f} | "
            f"OI: {oi:,.0f} | "
            f"Earnings: {days_earn}d"
            if all(x is not None for x in [beta, pcr, oi, days_earn])
            else f"Beta: {beta or 'N/A'} | Earnings: {days_earn or 'N/A'}d"
        )
        lines.append("")

        # ── Option strikes ────────────────────────────────────────────────────
        if not strikes:
            lines.append("_No strikes found in Delta 0.40-0.60 range for DTE 20-40_")
            lines.append("")
            continue

        any_results = True
        exp_date = strikes[0]["exp_date"]
        dte      = strikes[0]["dte"]
        lines.append(f"**Calls — Exp {exp_date} ({dte} DTE):**")
        lines.append("")
        lines.append("| Strike | Delta | Bid | Ask | Mid | Costo | θ/día | IV | Breakeven | +50% | +70% |")
        lines.append("|--------|-------|-----|-----|-----|-------|-------|----|-----------|------|------|")

        for s in strikes:
            itm_tag   = " ITM" if s["itm"] else ""
            ideal_tag = " ★"   if s["ideal_delta"] else ""
            theta_day = abs(s["theta"]) * 100 if s["theta"] else 0
            lines.append(
                f"| ${s['strike']:.1f}{itm_tag}{ideal_tag} "
                f"| {s['delta']:.3f} "
                f"| ${s['bid']:.2f} "
                f"| ${s['ask']:.2f} "
                f"| ${s['mid']:.2f} "
                f"| ${s['premium_total']:.0f} "
                f"| -${theta_day:.2f} "
                f"| {s['iv']:.1f}% "
                f"| ${s['breakeven']:.2f} ({s['breakeven_pct']:+.1f}%) "
                f"| +${s['profit_50']:.0f} "
                f"| +${s['profit_70']:.0f} |"
            )

        lines.append("")

        # ── Best strike recommendation ────────────────────────────────────────
        best = strikes[0]
        lines.append(
            f"**Best strike:** ${best['strike']:.1f} | "
            f"Delta {best['delta']:.3f} | "
            f"Mid ${best['mid']:.2f} | "
            f"Costo ${best['premium_total']:.0f}/contrato | "
            f"Breakeven ${best['breakeven']:.2f} ({best['breakeven_pct']:+.1f}%) | "
            f"Target +50%: +${best['profit_50']:.0f} | "
            f"Target +70%: +${best['profit_70']:.0f}"
        )
        lines.append("")

    if not any_results:
        lines.append("_No actionable strikes found today._")
        lines.append("")

    lines.append("---")
    lines.append(f"_Generated {timestamp} · option_selector.py_")

    return "\n".join(lines)