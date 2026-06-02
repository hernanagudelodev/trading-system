"""
option_selector.py
==================
Selects optimal option structures for tickers that passed hard filters.

Evaluates TWO bullish strategies per ticker:
    1. Long Call         → single strike, Delta 0.40-0.60
    2. Bull Call Spread  → buy Delta ~0.60 + sell Delta ~0.30

For each ticker:
    1. Fetches option chain from Tastytrade API
    2. Finds best expiration in DTE range (20-40 days)
    3. Captures Greeks + Quotes for all candidate strikes
    4. Builds Long Call candidates (Delta 0.40-0.60)
    5. Builds Bull Call Spread candidates (long ~0.60 / short ~0.30)
    6. Returns compact markdown for AI interpretation

Called by scanner.py after passes_hard_filters().

Usage:
    from option_selector import get_options_for_tickers
    markdown = get_options_for_tickers(session, tickers_dict)

ROADMAP (future):
    - Bearish path: Long Put + Bear Put Spread (when market_context bearish)
    - Uncertain path: Long Straddle (when VIX high / no clear direction)

Dependencies:
    tastytrade SDK
    Tastytrade session (passed in — no re-auth)
"""

import asyncio
import os
from datetime import date, datetime


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DTE_MIN          = 20    # minimum days to expiration
DTE_MAX          = 40    # maximum days to expiration

# Long Call delta range
DELTA_MIN        = 0.35
DELTA_MAX        = 0.65
DELTA_IDEAL_LOW  = 0.40
DELTA_IDEAL_HIGH = 0.60

# Bull Call Spread leg targets
SPREAD_LONG_DELTA_TARGET  = 0.60   # buy leg — ITM-ish
SPREAD_SHORT_DELTA_TARGET = 0.30   # sell leg — OTM
SPREAD_LONG_DELTA_RANGE   = (0.50, 0.70)
SPREAD_SHORT_DELTA_RANGE  = (0.20, 0.40)

MAX_LONG_CALLS   = 4     # max long call strikes to show
MAX_SPREADS      = 4     # max spreads to show
MAX_COST         = 300   # max cost per position (risk limit) — flagged if exceeded


# ══════════════════════════════════════════════════════════════════════════════
# ASYNC CORE — fetch option chain + Greeks for one ticker
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_option_data(session, ticker, price):
    """
    Fetch option chain with real-time Greeks for a single ticker.

    Returns dict with 'long_calls' and 'spreads' lists, or empty structure.
    """
    from tastytrade.instruments import NestedOptionChain
    from tastytrade.dxfeed import Greeks, Quote
    from tastytrade import DXLinkStreamer

    empty = {"long_calls": [], "spreads": [], "exp_date": None, "dte": None}

    try:
        chains = await NestedOptionChain.get(session, ticker)
        if not chains:
            return empty
        chain = chains[0]

        # ── Find best expiration in DTE range ─────────────────────────────────
        target_exp = None
        best_diff  = 9999
        for exp in chain.expirations:
            dte = exp.days_to_expiration
            if DTE_MIN <= dte <= DTE_MAX:
                diff = abs(dte - 30)
                if diff < best_diff:
                    best_diff  = diff
                    target_exp = exp

        if target_exp is None:
            return empty

        dte_selected = target_exp.days_to_expiration
        exp_date     = target_exp.expiration_date

        # ── Candidate strikes within ±25% of price ────────────────────────────
        price_low  = price * 0.75
        price_high = price * 1.25
        candidate_strikes = [
            s for s in target_exp.strikes
            if price_low <= float(s.strike_price) <= price_high
        ]
        if not candidate_strikes:
            return empty

        call_symbols = [s.call_streamer_symbol for s in candidate_strikes]

        # ── Fetch Greeks + Quotes via DXLink ──────────────────────────────────
        greeks_map = {}
        quotes_map = {}

        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Greeks, call_symbols)
            await streamer.subscribe(Quote, call_symbols)

            for _ in call_symbols:
                try:
                    g = await asyncio.wait_for(streamer.get_event(Greeks), timeout=10)
                    greeks_map[g.event_symbol] = g
                except asyncio.TimeoutError:
                    break

            for _ in call_symbols:
                try:
                    q = await asyncio.wait_for(streamer.get_event(Quote), timeout=10)
                    quotes_map[q.event_symbol] = q
                except asyncio.TimeoutError:
                    break

        # ── Build a unified strike table ──────────────────────────────────────
        strike_table = []
        for s in candidate_strikes:
            sym = s.call_streamer_symbol
            g   = greeks_map.get(sym)
            q   = quotes_map.get(sym)
            if g is None or g.delta is None:
                continue

            delta = float(g.delta)
            strike_price = float(s.strike_price)
            theta = float(g.theta)      if g.theta      else None
            vega  = float(g.vega)       if g.vega       else None
            iv    = float(g.volatility) * 100 if g.volatility else None
            theo  = float(g.price)      if g.price      else None

            bid = float(q.bid_price) if q and q.bid_price else 0.0
            ask = float(q.ask_price) if q and q.ask_price else 0.0
            mid = round((bid + ask) / 2, 2) if (bid and ask) else (theo or 0.0)
            spread_pct = round((ask - bid) / ask * 100, 1) if ask > 0 else None

            strike_table.append({
                "strike": strike_price,
                "delta":  delta,
                "theta":  theta,
                "vega":   vega,
                "iv":     iv,
                "bid":    bid,
                "ask":    ask,
                "mid":    mid,
                "spread_pct": spread_pct,
            })

        strike_table.sort(key=lambda x: x["strike"])

        # ── Build Long Call candidates ────────────────────────────────────────
        long_calls = _build_long_calls(strike_table, price)

        # ── Build Bull Call Spread candidates ─────────────────────────────────
        spreads = _build_spreads(strike_table, price)

        return {
            "long_calls": long_calls,
            "spreads":    spreads,
            "exp_date":   exp_date,
            "dte":        dte_selected,
        }

    except Exception as e:
        print(f"  option_selector error for {ticker}: {e}")
        return empty


# ══════════════════════════════════════════════════════════════════════════════
# LONG CALL BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_long_calls(strike_table, price):
    """Build Long Call candidates with Delta 0.35-0.65."""
    results = []
    for s in strike_table:
        delta = s["delta"]
        if not (DELTA_MIN <= delta <= DELTA_MAX):
            continue

        mid           = s["mid"]
        breakeven     = round(s["strike"] + mid, 2)
        breakeven_pct = round((breakeven - price) / price * 100, 2)
        premium_total = round(mid * 100, 0)
        profit_50     = round(mid * 0.50 * 100, 0)
        profit_70     = round(mid * 0.70 * 100, 0)
        ideal         = DELTA_IDEAL_LOW <= delta <= DELTA_IDEAL_HIGH
        theta_day     = round(abs(s["theta"]) * 100, 2) if s["theta"] else 0

        results.append({
            "strike":        s["strike"],
            "delta":         round(delta, 3),
            "bid":           s["bid"],
            "ask":           s["ask"],
            "mid":           mid,
            "iv":            round(s["iv"], 1) if s["iv"] else None,
            "theta_day":     theta_day,
            "premium_total": premium_total,
            "breakeven":     breakeven,
            "breakeven_pct": breakeven_pct,
            "profit_50":     profit_50,
            "profit_70":     profit_70,
            "ideal_delta":   ideal,
            "itm":           s["strike"] < price,
            "spread_pct":    s["spread_pct"],
            "within_budget": premium_total <= MAX_COST,
        })

    # Sort: ideal delta first, then lowest breakeven_pct
    results.sort(key=lambda x: (not x["ideal_delta"], x["breakeven_pct"]))
    return results[:MAX_LONG_CALLS]


# ══════════════════════════════════════════════════════════════════════════════
# BULL CALL SPREAD BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_spreads(strike_table, price):
    """
    Build Bull Call Spread candidates.

    Long leg:  Delta in SPREAD_LONG_DELTA_RANGE (~0.60)
    Short leg: Delta in SPREAD_SHORT_DELTA_RANGE (~0.30), strike above long

    For each valid (long, short) pair:
        net_debit   = long_mid - short_mid
        max_profit  = (short_strike - long_strike) - net_debit
        max_loss    = net_debit
        breakeven   = long_strike + net_debit
    """
    long_candidates  = [s for s in strike_table
                        if SPREAD_LONG_DELTA_RANGE[0] <= s["delta"] <= SPREAD_LONG_DELTA_RANGE[1]]
    short_candidates = [s for s in strike_table
                        if SPREAD_SHORT_DELTA_RANGE[0] <= s["delta"] <= SPREAD_SHORT_DELTA_RANGE[1]]

    spreads = []
    for long_leg in long_candidates:
        for short_leg in short_candidates:
            # Short strike must be above long strike
            if short_leg["strike"] <= long_leg["strike"]:
                continue

            spread_width = short_leg["strike"] - long_leg["strike"]
            # Skip very wide spreads (cost control) and very narrow
            if spread_width < 1 or spread_width > price * 0.15:
                continue

            net_debit = round(long_leg["mid"] - short_leg["mid"], 2)
            if net_debit <= 0:
                continue

            max_profit = round((spread_width - net_debit) * 100, 0)
            max_loss   = round(net_debit * 100, 0)
            breakeven  = round(long_leg["strike"] + net_debit, 2)
            breakeven_pct = round((breakeven - price) / price * 100, 2)
            risk_reward = round(max_profit / max_loss, 2) if max_loss > 0 else 0

            # Profit targets (50-70% of max profit)
            profit_50 = round(max_profit * 0.50, 0)
            profit_70 = round(max_profit * 0.70, 0)

            spreads.append({
                "long_strike":   long_leg["strike"],
                "short_strike":  short_leg["strike"],
                "long_delta":    round(long_leg["delta"], 3),
                "short_delta":   round(short_leg["delta"], 3),
                "long_mid":      long_leg["mid"],
                "short_mid":     short_leg["mid"],
                "spread_width":  spread_width,
                "net_debit":     net_debit,
                "cost_total":    max_loss,
                "max_profit":    max_profit,
                "max_loss":      max_loss,
                "breakeven":     breakeven,
                "breakeven_pct": breakeven_pct,
                "risk_reward":   risk_reward,
                "profit_50":     profit_50,
                "profit_70":     profit_70,
                "within_budget": max_loss <= MAX_COST,
            })

    # Sort: best risk/reward first among those within budget
    spreads.sort(key=lambda x: (not x["within_budget"], -x["risk_reward"]))
    return spreads[:MAX_SPREADS]


# ══════════════════════════════════════════════════════════════════════════════
# SYNC WRAPPER
# ══════════════════════════════════════════════════════════════════════════════

def get_options_for_tickers(session, tickers_data):
    """Synchronous entry point for scanner.py."""
    try:
        return asyncio.run(_get_options_async(session, tickers_data))
    except Exception as e:
        return f"option_selector error: {e}"


async def _get_options_async(session, tickers_data):
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
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines     = []

    lines.append(f"# Option Selector — {timestamp}")
    lines.append(f"DTE: {DTE_MIN}-{DTE_MAX} | Long Call Delta: {DELTA_IDEAL_LOW}-{DELTA_IDEAL_HIGH} | "
                 f"Spread: long ~{SPREAD_LONG_DELTA_TARGET}/short ~{SPREAD_SHORT_DELTA_TARGET} | "
                 f"Max cost: ${MAX_COST}")
    lines.append("")

    any_results = False

    for ticker, criteria in tickers_data.items():
        data    = options_results.get(ticker, {})
        long_calls = data.get("long_calls", [])
        spreads    = data.get("spreads", [])
        exp_date   = data.get("exp_date")
        dte        = data.get("dte")

        price = criteria.get("price", 0)
        vol   = criteria.get("volatility", {})
        tech  = criteria.get("technical", {})
        earn  = criteria.get("earnings", {})

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
        trend_str = f"{'BULLISH' if trend.get('is_bullish') else 'BEARISH'} ({trend.get('pct_change', 0):+.1f}% 25d)"
        sma_str   = 'Above both' if ma.get('above_sma50') and ma.get('above_sma200') else 'Above SMA50'
        rsi_str   = f"{rsi:.1f}" if rsi else "N/A"
        lines.append(f"Trend: {trend_str} | SMAs: {sma_str} | RSI: {rsi_str}")

        if all(x is not None for x in [iv, ivp, iv_rank, hv, iv_hv]):
            lines.append(f"IV: {iv:.1f}% (P{ivp:.0f} / Rank {iv_rank:.2f}) | HV: {hv:.1f}% | IV-HV: {iv_hv:+.1f}%")
        else:
            lines.append("IV: N/A (Tastytrade no devolvió métricas)")

        beta_str = f"{beta:.2f}" if beta is not None else "N/A"
        pcr_str  = f"{pcr:.2f}" if pcr is not None else "N/A"
        oi_str   = f"{oi:,.0f}" if oi is not None else "N/A"
        earn_str = f"{days_earn}d" if days_earn else "N/A"
        lines.append(f"Beta: {beta_str} | P/C: {pcr_str} | OI: {oi_str} | Earnings: {earn_str}")
        lines.append("")

        if not long_calls and not spreads:
            lines.append("_No hay estructuras viables en rango DTE 20-40 / Delta._")
            lines.append("")
            continue

        any_results = True
        lines.append(f"**Exp {exp_date} ({dte} DTE)**")
        lines.append("")

        # ── LONG CALL table ───────────────────────────────────────────────────
        if long_calls:
            lines.append("### Long Call")
            lines.append("")
            lines.append("| Strike | Delta | Bid | Ask | Mid | Costo | θ/día | IV | Breakeven | +50% | +70% |")
            lines.append("|--------|-------|-----|-----|-----|-------|-------|----|-----------|------|------|")
            for s in long_calls:
                itm_tag   = " ITM" if s["itm"] else ""
                ideal_tag = " ★"   if s["ideal_delta"] else ""
                budget    = "" if s["within_budget"] else " ⚠️"
                iv_str    = f"{s['iv']:.1f}%" if s['iv'] else "N/A"
                lines.append(
                    f"| ${s['strike']:.1f}{itm_tag}{ideal_tag} "
                    f"| {s['delta']:.3f} "
                    f"| ${s['bid']:.2f} | ${s['ask']:.2f} | ${s['mid']:.2f} "
                    f"| ${s['premium_total']:.0f}{budget} "
                    f"| -${s['theta_day']:.2f} "
                    f"| {iv_str} "
                    f"| ${s['breakeven']:.2f} ({s['breakeven_pct']:+.1f}%) "
                    f"| +${s['profit_50']:.0f} | +${s['profit_70']:.0f} |"
                )
            lines.append("")

        # ── BULL CALL SPREAD table ────────────────────────────────────────────
        if spreads:
            lines.append("### Bull Call Spread")
            lines.append("")
            lines.append("| Compra/Vende | Δ long/short | Débito | Costo | Ganancia máx | R/R | Breakeven | +50% | +70% |")
            lines.append("|--------------|--------------|--------|-------|--------------|-----|-----------|------|------|")
            for s in spreads:
                budget = "" if s["within_budget"] else " ⚠️"
                lines.append(
                    f"| ${s['long_strike']:.1f}/${s['short_strike']:.1f} "
                    f"| {s['long_delta']:.2f}/{s['short_delta']:.2f} "
                    f"| ${s['net_debit']:.2f} "
                    f"| ${s['cost_total']:.0f}{budget} "
                    f"| +${s['max_profit']:.0f} "
                    f"| {s['risk_reward']:.2f} "
                    f"| ${s['breakeven']:.2f} ({s['breakeven_pct']:+.1f}%) "
                    f"| +${s['profit_50']:.0f} | +${s['profit_70']:.0f} |"
                )
            lines.append("")

            # Best spread recommendation
            best = spreads[0]
            lines.append(
                f"**Mejor spread:** Compra ${best['long_strike']:.1f} / Vende ${best['short_strike']:.1f} | "
                f"Débito ${best['net_debit']:.2f} (${best['cost_total']:.0f}) | "
                f"Ganancia máx +${best['max_profit']:.0f} | "
                f"R/R {best['risk_reward']:.2f} | "
                f"Breakeven ${best['breakeven']:.2f} ({best['breakeven_pct']:+.1f}%)"
            )
            lines.append("")

    if not any_results:
        lines.append("_No actionable structures found today._")
        lines.append("")

    lines.append("---")
    lines.append(f"_Generated {timestamp} · option_selector.py_")

    return "\n".join(lines)