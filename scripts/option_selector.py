"""
option_selector.py
==================
Selects optimal option structures for tickers that passed hard filters.

Strategy selection is driven by criteria.py → select_strategy():
    IV < 30%   → Long Call (high Beta + momentum) or Bull Call Spread
    30-60%     → Bull Call Spread
    IV >= 60%  → Bull Put Spread (sell premium, time works FOR us)

For each ticker:
    1. Fetches option chain from Tastytrade API
    2. Finds best expiration in DTE range (20-40 days)
    3. Captures Greeks + Quotes for all candidate strikes
    4. Builds strategy-appropriate candidates
    5. Returns compact markdown for AI interpretation

Called by scanner.py after passes_hard_filters().
"""

import asyncio
import os
from datetime import date, datetime


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DTE_MIN = 20
DTE_MAX = 40

# Long Call / Bull Call Spread — call side
DELTA_MIN        = 0.35
DELTA_MAX        = 0.65
DELTA_IDEAL_LOW  = 0.40
DELTA_IDEAL_HIGH = 0.60

SPREAD_LONG_DELTA_TARGET  = 0.60
SPREAD_SHORT_DELTA_TARGET = 0.30
SPREAD_LONG_DELTA_RANGE   = (0.50, 0.70)
SPREAD_SHORT_DELTA_RANGE  = (0.20, 0.40)

# Bull Put Spread — put side
# Short put: slightly OTM (Delta 0.25-0.45, absolute value)
# Long put: further OTM (Delta 0.10-0.25, absolute value) — protection
PUT_SHORT_DELTA_RANGE = (0.25, 0.45)   # sell this put
PUT_LONG_DELTA_RANGE  = (0.10, 0.25)   # buy this put (lower strike)

MAX_LONG_CALLS = 4
MAX_SPREADS    = 4

# ── Gates de riesgo / calidad — RECHAZAN, no solo advierten ───────────────────
# Pérdida máxima por trade ≤ X% del capital. Atado a env var para escalar solo.
CAPITAL          = float(os.getenv("ACCOUNT_NLV", "14100"))   # net liquidating value
MAX_RISK_PCT     = 0.03                       # 3% del capital
MAX_RISK_DOLLARS = CAPITAL * MAX_RISK_PCT     # ~$423 con $14,100
MIN_RR_DEBIT     = 1.0                         # Bull Call Spread / Long Call: R/R mínimo
MIN_POP_CREDIT   = 60                          # Bull Put Spread: POP mínimo (%)
MIN_SPREAD_WIDTH = 3                           # ancho mínimo ($): evita spreads de $1-2
                                               # donde el stop de -65% salta a -130% entre chequeos


# ══════════════════════════════════════════════════════════════════════════════
# EXPIRACIÓN REAL — fuente única de verdad para la fecha (el LLM NO la elige)
# ══════════════════════════════════════════════════════════════════════════════

def get_real_expiration(ticker):
    """
    Devuelve la fecha de expiración REAL de la cadena: la misma regla que usa
    el spread builder (más cercana a 30 DTE dentro de 20-40). Crea sesión fresca.
    Devuelve un datetime.date, o None si no se pudo obtener.
    """
    try:
        return asyncio.run(_get_real_expiration_async(ticker))
    except Exception as e:
        print(f"  get_real_expiration error for {ticker}: {e}")
        return None


async def _get_real_expiration_async(ticker):
    from tastytrade import Session
    from tastytrade.instruments import NestedOptionChain

    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not client_secret or not refresh_token:
        return None

    session = Session(client_secret, refresh_token)
    chains  = await NestedOptionChain.get(session, ticker)
    if not chains:
        return None
    chain = chains[0]

    target_exp = None
    best_diff  = 9999
    for exp in chain.expirations:
        dte = exp.days_to_expiration
        if DTE_MIN <= dte <= DTE_MAX:
            diff = abs(dte - 30)
            if diff < best_diff:
                best_diff  = diff
                target_exp = exp

    return target_exp.expiration_date if target_exp else None


# ══════════════════════════════════════════════════════════════════════════════
# ASYNC CORE — fetch option chain + Greeks for one ticker
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_option_data(session, ticker, price, strategy):
    """
    Fetch option chain with real-time Greeks for a single ticker.
    strategy: 'Long Call' | 'Bull Call Spread' | 'Bull Put Spread'
    """
    from tastytrade.instruments import NestedOptionChain
    from tastytrade.dxfeed import Greeks, Quote
    from tastytrade import DXLinkStreamer

    empty = {"long_calls": [], "spreads": [], "put_spreads": [],
             "exp_date": None, "dte": None, "strategy": strategy}

    try:
        chains = await NestedOptionChain.get(session, ticker)
        if not chains:
            return empty
        chain = chains[0]

        # Best expiration in DTE range (closest to 30d)
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

        # Candidate strikes within ±25% of price
        price_low  = price * 0.75
        price_high = price * 1.25
        candidate_strikes = [
            s for s in target_exp.strikes
            if price_low <= float(s.strike_price) <= price_high
        ]
        if not candidate_strikes:
            return empty

        # For Bull Put Spread we need put symbols; others need call symbols
        if strategy == "Bull Put Spread":
            symbols = [s.put_streamer_symbol for s in candidate_strikes]
        else:
            symbols = [s.call_streamer_symbol for s in candidate_strikes]

        greeks_map = {}
        quotes_map = {}

        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Greeks, symbols)
            await streamer.subscribe(Quote, symbols)

            for _ in symbols:
                try:
                    g = await asyncio.wait_for(streamer.get_event(Greeks), timeout=10)
                    greeks_map[g.event_symbol] = g
                except asyncio.TimeoutError:
                    break

            for _ in symbols:
                try:
                    q = await asyncio.wait_for(streamer.get_event(Quote), timeout=10)
                    quotes_map[q.event_symbol] = q
                except asyncio.TimeoutError:
                    break

        # Build unified strike table
        strike_table = []
        for s in candidate_strikes:
            sym = s.put_streamer_symbol if strategy == "Bull Put Spread" \
                  else s.call_streamer_symbol
            g   = greeks_map.get(sym)
            q   = quotes_map.get(sym)
            if g is None or g.delta is None:
                continue

            delta        = float(g.delta)
            strike_price = float(s.strike_price)
            theta = float(g.theta)       if g.theta      else None
            iv    = float(g.volatility) * 100 if g.volatility else None
            bid   = float(q.bid_price)   if q and q.bid_price else 0.0
            ask   = float(q.ask_price)   if q and q.ask_price else 0.0
            theo  = float(g.price)       if g.price      else None
            mid   = round((bid + ask) / 2, 2) if (bid and ask) else (theo or 0.0)
            spread_pct = round((ask - bid) / ask * 100, 1) if ask > 0 else None

            strike_table.append({
                "strike":     strike_price,
                "delta":      delta,
                "theta":      theta,
                "iv":         iv,
                "bid":        bid,
                "ask":        ask,
                "mid":        mid,
                "spread_pct": spread_pct,
            })

        strike_table.sort(key=lambda x: x["strike"])

        # Build strategy-specific candidates
        long_calls  = []
        spreads     = []
        put_spreads = []

        if strategy == "Long Call":
            long_calls = _build_long_calls(strike_table, price)
        elif strategy == "Bull Call Spread":
            long_calls = _build_long_calls(strike_table, price)
            spreads    = _build_call_spreads(strike_table, price)
        elif strategy == "Bull Put Spread":
            put_spreads = _build_put_spreads(strike_table, price)

        return {
            "long_calls":  long_calls,
            "spreads":     spreads,
            "put_spreads": put_spreads,
            "exp_date":    exp_date,
            "dte":         dte_selected,
            "strategy":    strategy,
        }

    except Exception as e:
        print(f"  option_selector error for {ticker}: {e}")
        return empty


# ══════════════════════════════════════════════════════════════════════════════
# LONG CALL BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_long_calls(strike_table, price):
    results = []
    for s in strike_table:
        delta = s["delta"]
        if not (DELTA_MIN <= delta <= DELTA_MAX):
            continue

        mid           = s["mid"]
        breakeven     = round(s["strike"] + mid, 2)
        breakeven_pct = round((breakeven - price) / price * 100, 2)
        premium_total = round(mid * 100, 0)

        # Gate de riesgo: la prima ES la pérdida máxima de un Long Call
        if premium_total > MAX_RISK_DOLLARS:
            continue

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
            "within_budget": True,
        })

    results.sort(key=lambda x: (not x["ideal_delta"], x["breakeven_pct"]))
    return results[:MAX_LONG_CALLS]


# ══════════════════════════════════════════════════════════════════════════════
# BULL CALL SPREAD BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_call_spreads(strike_table, price):
    long_cands  = [s for s in strike_table
                   if SPREAD_LONG_DELTA_RANGE[0] <= s["delta"] <= SPREAD_LONG_DELTA_RANGE[1]]
    short_cands = [s for s in strike_table
                   if SPREAD_SHORT_DELTA_RANGE[0] <= s["delta"] <= SPREAD_SHORT_DELTA_RANGE[1]]

    spreads = []
    for long_leg in long_cands:
        for short_leg in short_cands:
            if short_leg["strike"] <= long_leg["strike"]:
                continue
            spread_width = short_leg["strike"] - long_leg["strike"]
            if spread_width < MIN_SPREAD_WIDTH or spread_width > price * 0.15:
                continue

            net_debit = round(long_leg["mid"] - short_leg["mid"], 2)
            if net_debit <= 0:
                continue

            max_profit    = round((spread_width - net_debit) * 100, 0)
            max_loss      = round(net_debit * 100, 0)
            breakeven     = round(long_leg["strike"] + net_debit, 2)
            breakeven_pct = round((breakeven - price) / price * 100, 2)
            rr            = round(max_profit / max_loss, 2) if max_loss > 0 else 0

            # Gates: riesgo ≤ tope y R/R mínimo (débito)
            if max_loss > MAX_RISK_DOLLARS:
                continue
            if rr < MIN_RR_DEBIT:
                continue

            spreads.append({
                "long_strike":   long_leg["strike"],
                "short_strike":  short_leg["strike"],
                "long_delta":    round(long_leg["delta"], 3),
                "short_delta":   round(short_leg["delta"], 3),
                "net_debit":     net_debit,
                "cost_total":    max_loss,
                "max_profit":    max_profit,
                "max_loss":      max_loss,
                "breakeven":     breakeven,
                "breakeven_pct": breakeven_pct,
                "risk_reward":   rr,
                "profit_50":     round(max_profit * 0.50, 0),
                "profit_70":     round(max_profit * 0.70, 0),
                "within_budget": True,
            })

    spreads.sort(key=lambda x: (not x["within_budget"], -x["risk_reward"]))
    return spreads[:MAX_SPREADS]


# ══════════════════════════════════════════════════════════════════════════════
# BULL PUT SPREAD BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_put_spreads(strike_table, price):
    """
    Build Bull Put Spread candidates.

    Structure:
        Sell OTM put (higher strike, Delta 0.25-0.45 abs) — collect premium
        Buy  OTM put (lower strike,  Delta 0.10-0.25 abs) — limit risk

    Note: put deltas are negative. We use absolute values for comparison.
    Profit = net credit received (if price stays above short put at expiry)
    Max loss = spread width - net credit

    We want:
        - Short put slightly OTM (below current price)
        - Long put further OTM (more below current price)
        - Short strike > Long strike (both below price)
    """
    # For puts: delta is negative, abs(delta) is what we compare
    # Puts with higher absolute delta = closer to ATM = higher strike
    short_cands = [s for s in strike_table
                   if PUT_SHORT_DELTA_RANGE[0] <= abs(s["delta"]) <= PUT_SHORT_DELTA_RANGE[1]
                   and s["strike"] < price]  # must be OTM (below price)

    long_cands  = [s for s in strike_table
                   if PUT_LONG_DELTA_RANGE[0] <= abs(s["delta"]) <= PUT_LONG_DELTA_RANGE[1]
                   and s["strike"] < price]  # further OTM

    put_spreads = []
    for short_leg in short_cands:
        for long_leg in long_cands:
            # Long put must have lower strike than short put
            if long_leg["strike"] >= short_leg["strike"]:
                continue

            spread_width = short_leg["strike"] - long_leg["strike"]
            if spread_width < MIN_SPREAD_WIDTH or spread_width > price * 0.12:
                continue

            # Net credit = what we receive for selling short - what we pay for long
            net_credit = round(short_leg["mid"] - long_leg["mid"], 2)
            if net_credit <= 0:
                continue

            max_profit  = round(net_credit * 100, 0)       # credit received
            max_loss    = round((spread_width - net_credit) * 100, 0)
            # Breakeven: short put strike - net credit
            breakeven   = round(short_leg["strike"] - net_credit, 2)
            # How far below current price is breakeven? (negative = below price)
            be_pct      = round((breakeven - price) / price * 100, 2)
            # R/R for credit spreads: max_profit / max_loss
            rr          = round(max_profit / max_loss, 2) if max_loss > 0 else 0
            # Probability of profit ≈ 1 - abs(short delta)
            pop_approx  = round((1 - abs(short_leg["delta"])) * 100, 0)

            # Gates: riesgo ≤ tope y POP mínimo (crédito)
            if max_loss > MAX_RISK_DOLLARS:
                continue
            if pop_approx < MIN_POP_CREDIT:
                continue

            put_spreads.append({
                "short_strike":   short_leg["strike"],   # sell this (higher)
                "long_strike":    long_leg["strike"],    # buy this (lower, protection)
                "short_delta":    round(short_leg["delta"], 3),
                "long_delta":     round(long_leg["delta"], 3),
                "short_mid":      short_leg["mid"],
                "long_mid":       long_leg["mid"],
                "net_credit":     net_credit,
                "max_profit":     max_profit,
                "max_loss":       max_loss,
                "breakeven":      breakeven,
                "breakeven_pct":  be_pct,
                "risk_reward":    rr,
                "pop_approx":     pop_approx,
                "stop_loss_2x":   round(net_credit * 2 * 100, 0),  # close if spread costs 2x credit
            })

    # Sort: best R/R first
    put_spreads.sort(key=lambda x: -x["risk_reward"])
    return put_spreads[:MAX_SPREADS]


# ══════════════════════════════════════════════════════════════════════════════
# MARKDOWN BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_markdown(tickers_data, options_results):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines     = [
        f"# Option Selector — {timestamp}",
        f"DTE: {DTE_MIN}-{DTE_MAX} | Delta ranges auto-selected by strategy",
        "",
    ]

    for ticker, criteria in tickers_data.items():
        data        = options_results.get(ticker, {})
        long_calls  = data.get("long_calls", [])
        spreads     = data.get("spreads", [])
        put_spreads = data.get("put_spreads", [])
        exp_date    = data.get("exp_date")
        dte         = data.get("dte")
        strategy    = data.get("strategy", "Bull Call Spread")

        price = criteria.get("price", 0)
        vol   = criteria.get("volatility", {})
        tech  = criteria.get("technical", {})
        earn  = criteria.get("earnings", {})

        lines.append(f"## {ticker} — ${price:.2f}")
        lines.append("")

        # Criteria summary
        trend     = tech.get("trend_25d", {})
        ma        = tech.get("moving_averages", {})
        rsi       = tech.get("rsi")
        iv        = vol.get("iv")
        ivp       = vol.get("iv_percentile")
        iv_rank   = vol.get("iv_rank")
        hv        = vol.get("hv_30d")
        iv_hv     = vol.get("iv_hv_diff")
        beta      = vol.get("beta")
        pcr       = vol.get("put_call_ratio")
        oi        = vol.get("open_interest")
        days_earn = earn.get("days_to_earnings")

        trend_str = f"{'BULLISH' if trend.get('is_bullish') else 'BEARISH'} ({trend.get('pct_change', 0):+.1f}% 25d)"
        sma_str   = 'Above both' if ma.get('above_sma50') and ma.get('above_sma200') else 'Above SMA50'
        rsi_str   = f"{rsi:.1f}" if rsi else "N/A"

        lines.append("**Criteria:**")
        lines.append(f"Trend: {trend_str} | SMAs: {sma_str} | RSI: {rsi_str}")
        if all(x is not None for x in [iv, ivp, iv_rank, hv, iv_hv]):
            lines.append(f"IV: {iv:.1f}% (P{ivp:.0f} / Rank {iv_rank:.2f}) | HV: {hv:.1f}% | IV-HV: {iv_hv:+.1f}%")
        beta_str  = f"{beta:.2f}"   if beta      is not None else "N/A"
        pcr_str   = f"{pcr:.2f}"    if pcr       is not None else "N/A"
        oi_str    = f"{oi:,.0f}"    if oi        is not None else "N/A"
        earn_str  = f"{days_earn}d" if days_earn             else "N/A"
        lines.append(f"Beta: {beta_str} | P/C: {pcr_str} | OI: {oi_str} | Earnings: {earn_str}")
        lines.append("")
        lines.append(f"**Estrategia recomendada: {strategy}**")
        lines.append("")

        if not long_calls and not spreads and not put_spreads:
            lines.append("_No hay estructuras viables (DTE 20-40 / Delta / riesgo ≤3% / R-R / POP)._")
            lines.append("")
            continue

        lines.append(f"**Exp {exp_date} ({dte} DTE)**")
        lines.append("")

        # ── Long Call ─────────────────────────────────────────────────────────
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

        # ── Bull Call Spread ──────────────────────────────────────────────────
        if spreads:
            lines.append("### Bull Call Spread")
            lines.append("")
            lines.append("| Compra/Vende | Δ long/short | Débito | Costo | Ganancia máx | R/R | Breakeven | +50% | +70% |")
            lines.append("|--------------|--------------|--------|-------|--------------|-----|-----------|------|------|")
            best_idx = 0
            for i, s in enumerate(spreads):
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
            best = spreads[best_idx]
            lines.append("")
            lines.append(
                f"**Mejor spread:** Compra ${best['long_strike']:.1f} / Vende ${best['short_strike']:.1f} "
                f"| Débito ${best['net_debit']:.2f} (${best['cost_total']:.0f}) "
                f"| Ganancia máx +${best['max_profit']:.0f} "
                f"| R/R {best['risk_reward']:.2f} "
                f"| Breakeven ${best['breakeven']:.2f} ({best['breakeven_pct']:+.1f}%)"
            )
            lines.append("")

        # ── Bull Put Spread ───────────────────────────────────────────────────
        if put_spreads:
            lines.append("### Bull Put Spread (Credit Spread)")
            lines.append("")
            lines.append("_Vendes el put de strike alto, compras el de strike bajo. "
                         "Cobras crédito desde el día 1. "
                         "Ganas si el precio se mantiene sobre el breakeven._")
            lines.append("")
            lines.append("| Vende/Compra | Δ short/long | Crédito | Max Ganancia | Max Pérdida | R/R | Breakeven | POP | Stop 2x |")
            lines.append("|-------------|--------------|---------|--------------|-------------|-----|-----------|-----|---------|")
            for s in put_spreads:
                lines.append(
                    f"| ${s['short_strike']:.1f}/${s['long_strike']:.1f} "
                    f"| {s['short_delta']:.2f}/{s['long_delta']:.2f} "
                    f"| ${s['net_credit']:.2f} "
                    f"| +${s['max_profit']:.0f} "
                    f"| -${s['max_loss']:.0f} "
                    f"| {s['risk_reward']:.2f} "
                    f"| ${s['breakeven']:.2f} ({s['breakeven_pct']:+.1f}%) "
                    f"| ~{s['pop_approx']:.0f}% "
                    f"| ${s['stop_loss_2x']:.0f} |"
                )
            best = put_spreads[0]
            lines.append("")
            lines.append(
                f"**Mejor spread:** Vende ${best['short_strike']:.1f} / Compra ${best['long_strike']:.1f} "
                f"| Crédito ${best['net_credit']:.2f} (${best['max_profit']:.0f}) "
                f"| Max pérdida -${best['max_loss']:.0f} "
                f"| R/R {best['risk_reward']:.2f} "
                f"| Breakeven ${best['breakeven']:.2f} ({best['breakeven_pct']:+.1f}%) "
                f"| POP ~{best['pop_approx']:.0f}%"
            )
            lines.append("")

    lines.append(f"---")
    lines.append(f"_Generated {timestamp} · option_selector.py_")
    return "\n".join(lines)


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
    from criteria import select_strategy

    results = {}
    for ticker, criteria in tickers_data.items():
        strategy = select_strategy(criteria)
        results[ticker] = await _fetch_option_data(session, ticker,
                                                    criteria.get("price", 0),
                                                    strategy)
    return _build_markdown(tickers_data, results)

def position_max_loss(strike_low, strike_high, debit, contracts=1) -> float:
    """
    Pérdida máxima en DÓLARES de un spread vertical de 2 patas.
    Fuente ÚNICA: la usan el gate de cartera (auto_run) y check_open.py.

    Manda el SIGNO de `debit`, no el string de strategy:
        debit > 0  -> débito  (BCS): pérdida máx = lo que pagaste
        debit < 0  -> crédito (BPS): pérdida máx = ancho - crédito

    `premium_paid` en la DB ya trae ese signo, así que una fila se pasa directo.
    """
    width = abs(float(strike_high) - float(strike_low))
    d     = float(debit)
    n     = int(contracts or 1)
    if d > 0:
        return round(d * 100 * n, 2)
    return round((width - abs(d)) * 100 * n, 2)

def portfolio_risk_pct() -> float:
    """
    Tope de riesgo AGREGADO de cartera, en % del capital.
    Fuente ÚNICA: la usan el gate de auto_run y check_open.py.

    Obligatoria en los DOS libros (paper y live). Sin default: un tope ausente
    no es "sin tope", es un bug. El default silencioso es exactamente cómo
    MAX_COST terminó siendo decorativo y dejó pasar el GS de $3,945 (§12.3).
    """
    raw = os.getenv("MAX_PORTFOLIO_RISK_PCT")
    if raw is None:
        raise RuntimeError(
            "MAX_PORTFOLIO_RISK_PCT no está definida. Obligatoria en paper y en "
            "live: sin ella el gate de cartera no rechazaría nada."
        )
    return float(raw)


def spread_pnl(strike_low, strike_high, premium_paid, contracts, spread_value):
    """
    P&L de un spread vertical de 2 patas a un `spread_value` dado.

    FUENTE ÚNICA. Antes esta matemática vivía en DOS lugares:
        monitor.run_paper_monitor  (línea ~971)
        trade.cmd_paper_close      (línea ~440)
    Y ya habían divergido: el monitor NO multiplicaba por `contracts`. En paper
    no se nota —el INSERT lo tiene clavado en 1— pero en live `contracts` viene
    del broker, y el P&L del monitor saldría mal por el multiplicador.
    Es el mismo bug que tenía check_open.py. Las copias se separan solas.

    MANDA EL SIGNO, no el string de strategy:
        premium_paid > 0  débito  (BCS): pagaste al abrir, cobrás al cerrar
        premium_paid < 0  crédito (BPS): cobraste al abrir, pagás al cerrar
    Así un Bear Call Spread —que también es crédito— sale bien sin nombrarlo.

    spread_value : valor actual del spread por acción, POSITIVO
                   (lo que devuelve pricing.get_spread_value)

    Devuelve dict con max_profit, max_loss, current_value, gross_pnl, pnl_pct,
    profit_pct_of_max, strategy_type. pnl_pct y profit_pct_of_max pueden ser
    None si la base es cero: sin dato -> None, nunca un 0 que miente.
    """
    width = abs(float(strike_high) - float(strike_low))
    prem  = float(premium_paid)
    n     = int(contracts or 1)
    sv    = abs(float(spread_value))

    if prem < 0:
        # CRÉDITO. Cobraste `net_credit` al abrir; cerrar cuesta `current_value`.
        net_credit    = abs(prem)
        max_profit    = round(net_credit * n * 100, 2)
        current_value = round(sv * n * 100, 2)          # costo de cerrar
        gross_pnl     = round(max_profit - current_value, 2)
        base_pct      = max_profit
        tipo          = "credit_spread"
    else:
        # DÉBITO. Pagaste `total_cost` al abrir; cerrar te paga `current_value`.
        total_cost    = round(prem * n * 100, 2)
        max_profit    = round((width - prem) * n * 100, 2)
        current_value = round(sv * n * 100, 2)
        gross_pnl     = round(current_value - total_cost, 2)
        base_pct      = total_cost
        tipo          = "debit_spread"

    return {
        "max_profit":        max_profit,
        "max_loss":          position_max_loss(strike_low, strike_high, prem, n),
        "current_value":     current_value,
        "gross_pnl":         gross_pnl,
        "pnl_pct":           round(gross_pnl / base_pct * 100, 2) if base_pct else None,
        "profit_pct_of_max": round(gross_pnl / max_profit, 4) if max_profit else None,
        "strategy_type":     tipo,
    }