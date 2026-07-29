"""
scanner.py
==========
Market opportunity scanner — simplified, no internal AI.

Tier system:
    Tier 1 — STARS: blue chip tickers scanned first, every day.
              If 2+ candidates pass filters → stop here.
    Tier 2 — UNIVERSE: full S&P 500 via universe.py.
              Only activated with --universe flag if Tier 1 yields < 2 candidates.

Usage:
    python scanner.py                # Tier 1 stars only (default)
    python scanner.py --universe     # expand to S&P 500 if needed
    python scanner.py --tickers AAPL MSFT CAT
    python scanner.py --context      # legacy: from market_context.json
"""

import os
import sys
import json
import argparse
from datetime import datetime

from dotenv import load_dotenv
from tastytrade import Session

from criteria import get_all_criteria, passes_hard_filters
from option_selector import get_options_for_tickers
from db import get_open_positions

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# TIER 1 — STARS
# Blue chip tickers: high liquidity, tight spreads, predictable options market.
# Diversified: Tech, Semiconductors, Financials, Consumer, Health, Software.
# ══════════════════════════════════════════════════════════════════════════════

TIER1_STARS = [
    # Technology — mega caps, most liquid options
    "AAPL", "MSFT", "GOOGL", "META", "AMZN", "NVDA", "TSLA",
    # Semiconductors
    "AVGO", "AMD", "TXN", "QCOM",
    # Financials
    "JPM", "V", "MA", "GS", "BAC",
    # Consumer
    "HD", "WMT", "COST", "MCD", "NKE",
    # Health
    "JNJ", "UNH", "LLY",
    # Enterprise software
    "CRM", "NOW", "ADBE", "INTU",
]

TIER1_MIN_CANDIDATES = 2   # expand to universe if fewer than this pass

# Reports go to reports/ directory (one level up from scripts/)
_BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR      = os.path.join(_BASE_DIR, "reports")
MARKET_CONTEXT_JSON  = os.path.join(REPORTS_DIR, "market_context.json")
REPORT_MD_PATH       = os.path.join(REPORTS_DIR, "scanner_report.md")
REPORT_HTML_PATH     = os.path.join(REPORTS_DIR, "scanner_report.html")
REPORT_AI_PATH       = os.path.join(REPORTS_DIR, "scanner_ai_summary.md")


def _open_browser(path):
    """Open in browser only when running locally — skip on Railway/server."""
    if os.getenv("RAILWAY_ENVIRONMENT"):
        return
    try:
        import webbrowser
        webbrowser.open(f"file:///{path.replace(os.sep, '/')}")
    except Exception:
        pass

# Sector map for concentration warnings
TICKER_SECTOR = {
    "AAPL": "Tech",  "MSFT": "Tech",  "GOOGL": "Tech", "META": "Tech",
    "AMZN": "Tech",  "NVDA": "Tech",  "TSLA": "Tech",  "AVGO": "Tech",
    "AMD":  "Tech",  "TXN":  "Tech",  "QCOM": "Tech",  "CRM":  "Tech",
    "NOW":  "Tech",  "ADBE": "Tech",  "INTU": "Tech",
    "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "GS":  "Financials", "BAC": "Financials",
    "HD":   "Consumer",  "WMT":  "Consumer", "COST": "Consumer",
    "MCD":  "Consumer",  "NKE":  "Consumer",
    "JNJ":  "Health",    "UNH":  "Health",   "LLY":  "Health",
}


# ══════════════════════════════════════════════════════════════════════════════
# MARKET CONTEXT
# ══════════════════════════════════════════════════════════════════════════════

def load_market_context():
    if not os.path.exists(MARKET_CONTEXT_JSON):
        return None
    with open(MARKET_CONTEXT_JSON) as f:
        return json.load(f)


def format_market_context_md(ctx):
    if not ctx:
        return "Market context not available — run market_context.py first.\n"
    vix     = ctx.get("vix", {})
    spy     = ctx.get("spy", {})
    verdict = ctx.get("verdict", "N/A")
    detail  = ctx.get("verdict_detail", "")
    lines   = [
        f"## Contexto Macro",
        f"**Verdict:** {verdict} — {detail}",
        f"**VIX:** {vix.get('current', 'N/A')} ({vix.get('level', 'N/A')}, {vix.get('trend', 'N/A')})",
        f"**SPY:** ${spy.get('price', 'N/A')} | {spy.get('trend', 'N/A')} ({spy.get('pct_25d', 0):+.1f}% 25d)",
    ]
    sectors = ctx.get("sectors", [])
    if sectors:
        lines.append("\n**Sectores (win rates históricos):**")
        for s in sectors:
            icon = "✅" if s["priority"] == "PRIORITY" else \
                   "⚠️" if s["priority"] == "ACCEPTABLE" else "❌"
            lines.append(f"- {icon} {s['sector']}: {s['win_rate']:.1f}% [{s['priority']}]")
    return "\n".join(lines)


def format_open_positions_md(positions):
    if not positions:
        return "## Posiciones Abiertas\nNinguna.\n"
    lines = ["## Posiciones Abiertas", ""]
    for p in positions:
        lines.append(
            f"- **{p['ticker']}** {p['strategy']} | "
            f"${p['strike_low']}/{p['strike_high']} | "
            f"Exp {p['expiration']} | Costo ${p['total_cost']}"
        )
    return "\n".join(lines)


def format_criteria_md(ticker, criteria):
    price = criteria.get("price", 0)
    tech  = criteria.get("technical", {})
    vol   = criteria.get("volatility", {})
    earn  = criteria.get("earnings", {})
    volm  = criteria.get("volume", {})
    fund  = criteria.get("fundamental", {})

    lines = [f"### {ticker} — ${price:.2f}"]

    trend   = tech.get("trend_25d", {})
    ma      = tech.get("moving_averages", {})
    rsi     = tech.get("rsi")
    pos52   = tech.get("week_52_position_pct")
    support = tech.get("support_distance_pct")
    resist  = tech.get("resistance_distance_pct")
    candle  = tech.get("candle_pattern", "N/A")

    lines.append("\n**Technical:**")
    lines.append(f"- Trend 25d: {'BULLISH' if trend.get('is_bullish') else 'BEARISH'} "
                 f"{trend.get('pct_change', 0):+.1f}%")
    above_both = ma.get("above_sma50") and ma.get("above_sma200")
    sma50_dir  = "RISING" if ma.get("sma50_rising") else "FALLING"
    lines.append(f"- MAs: {'Above both' if above_both else 'Below SMA50' if not ma.get('above_sma50') else 'Mixed'} "
                 f"| SMA50 {sma50_dir}")
    lines.append(f"- RSI: {rsi:.1f}" if rsi else "- RSI: N/A")
    lines.append(f"- 52w: {pos52:.1f}%" if pos52 is not None else "- 52w: N/A")
    lines.append(f"- Support: {support:.1f}% away" if support is not None else "- Support: N/A")
    lines.append(f"- Resistance: {resist:.1f}% away" if resist is not None else "- Resistance: N/A")
    lines.append(f"- Candle: {candle}")

    iv     = vol.get("iv")
    ivp    = vol.get("iv_percentile")
    ivrank = vol.get("iv_rank")
    hv30   = vol.get("hv_30d")
    ivhv   = vol.get("iv_hv_spread")
    beta   = vol.get("beta")
    pc     = vol.get("put_call_ratio")
    oi     = vol.get("open_interest_atm")
    liq    = vol.get("liquidity_score")

    lines.append("\n**Volatility:**")
    ivp_str  = f"P{ivp:.0f}" if ivp is not None else "N/A"
    rank_str = f"Rank {ivrank:.2f}" if ivrank is not None else "N/A"
    lines.append(f"- IV: {iv:.1f}% ({ivp_str} / {rank_str})" if iv else "- IV: N/A")
    lines.append(f"- HV 30d: {hv30:.1f}% | IV-HV: {ivhv:+.1f}%" if hv30 and ivhv else "- HV: N/A")
    lines.append(f"- Beta: {beta:.2f}" if beta else "- Beta: N/A")
    lines.append(f"- Put/Call: {pc:.2f}" if pc else "- Put/Call: N/A")
    lines.append(f"- OI (ATM): {oi:,}" if oi else "- OI: N/A")
    lines.append(f"- Liquidity: {liq}/5" if liq else "- Liquidity: N/A")

    days_earn = earn.get("days_to_earnings")
    vol_ratio = volm.get("volume_ratio_pct", 0)
    lines.append("\n**Operational:**")
    lines.append(f"- Earnings: {days_earn}d" if days_earn else
                 "- Earnings: ETF" if earn.get("is_etf") else "- Earnings: N/A")
    lines.append(f"- Volume: {vol_ratio:.0f}% of avg")

    pe     = fund.get("pe")
    eps_g  = fund.get("eps_growth_pct")
    de     = fund.get("debt_to_equity")
    margin = fund.get("profit_margin_pct")
    lines.append("\n**Fundamental:**")
    lines.append(f"- PE: {pe:.1f}x" if pe is not None else "- PE: N/A")
    lines.append(f"- EPS growth: {eps_g:+.1f}%" if eps_g is not None else "- EPS growth: N/A")
    lines.append(f"- Debt/Equity: {de:.2f}x" if de is not None else "- Debt/Equity: N/A")
    lines.append(f"- Profit margin: {margin:.1f}%" if margin is not None else "- Profit margin: N/A")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# SECTOR CONCENTRATION WARNING
# ══════════════════════════════════════════════════════════════════════════════

def check_sector_concentration(open_positions, passed_criteria):
    warnings = []
    open_sectors = {}
    for p in open_positions:
        ticker = p.get("ticker", "")
        sector = TICKER_SECTOR.get(ticker, "Other")
        open_sectors.setdefault(sector, []).append(ticker)

    for ticker in passed_criteria:
        sector = TICKER_SECTOR.get(ticker, "Other")
        if sector in open_sectors:
            existing = ", ".join(open_sectors[sector])
            warnings.append(
                f"⚠️ {ticker} ({sector}) — ya tienes {existing} abierta en este sector"
            )
    return warnings


# ══════════════════════════════════════════════════════════════════════════════
# REPORT GENERATORS
# ══════════════════════════════════════════════════════════════════════════════

def generate_ai_summary(market_ctx, open_positions, passed_criteria,
                        options_md, tier_used):
    """
    Generate ultra-compact AI-friendly summary for Anthropic API consumption.
    Target: ~50 lines max. No tables, no HTML, no eliminated tickers.
    Saved to reports/scanner_ai_summary.md
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines     = [f"SCANNER AI SUMMARY — {timestamp}", f"Tier: {tier_used}", ""]

    # ── Macro context ─────────────────────────────────────────────────────────
    if market_ctx:
        vix     = market_ctx.get("vix", {})
        spy     = market_ctx.get("spy", {})
        verdict = market_ctx.get("verdict", "N/A")
        detail  = market_ctx.get("verdict_detail", "")
        lines.append(
            f"MACRO: {verdict} — {detail}"
        )
        lines.append(
            f"VIX: {vix.get('current', 'N/A')} {vix.get('level', '')} "
            f"{vix.get('trend', '')} | "
            f"SPY: ${spy.get('price', 'N/A')} {spy.get('trend', '')} "
            f"{spy.get('pct_25d', 0):+.1f}% 25d"
        )

        # Macro events
        macro_events = market_ctx.get("macro_events", [])
        if macro_events:
            event_strs = [
                f"{e['event'].split('—')[0].strip()} en {e['days_away']}d ({e['impact']})"
                for e in macro_events
            ]
            lines.append(f"EVENTOS: {' | '.join(event_strs)}")

        # Upcoming earnings risk
        upcoming = market_ctx.get("upcoming_earnings", [])
        if upcoming:
            earn_strs = [f"{e['ticker']} ({e['sector']}) en {e['days_away']}d"
                         for e in upcoming]
            lines.append(f"EARNINGS RIESGO: {' | '.join(earn_strs)}")
    else:
        lines.append("MACRO: no disponible")

    lines.append("")

    # ── Candidates ────────────────────────────────────────────────────────────
    if passed_criteria:
        lines.append(f"CANDIDATOS ({len(passed_criteria)}):")

        # Parse options_md to extract best spread per ticker
        import re
        best_spreads = {}
        if options_md:
            # Find "Mejor spread:" lines
            for m in re.finditer(
                r'\*\*Mejor spread:\*\*\s*(.+?)(?:\n|$)', options_md
            ):
                # Look back to find which ticker this belongs to
                pos   = m.start()
                chunk = options_md[:pos]
                tm    = re.findall(r'^## ([A-Z]+) —', chunk, re.MULTILINE)
                if tm:
                    best_spreads[tm[-1]] = m.group(1).strip()

            # Also find Bull Put Spread best
            for m in re.finditer(
                r'\*\*Mejor spread:\*\*\s*Vende (.+?)(?:\n|$)', options_md
            ):
                pos   = m.start()
                chunk = options_md[:pos]
                tm    = re.findall(r'^## ([A-Z]+) —', chunk, re.MULTILINE)
                if tm:
                    best_spreads[tm[-1]] = f"PUT: {m.group(0).replace('**Mejor spread:** ', '').strip()}"

        for ticker, criteria in passed_criteria.items():
            price = criteria.get("price", 0)
            vol   = criteria.get("volatility", {})
            tech  = criteria.get("technical", {})
            earn  = criteria.get("earnings", {})

            iv    = vol.get("iv")
            ivp   = vol.get("iv_percentile")
            rsi   = tech.get("rsi")
            trend = tech.get("trend_25d", {})
            beta  = vol.get("beta")
            pcr   = vol.get("put_call_ratio")

            from criteria import select_strategy
            strategy = select_strategy(criteria)

            trend_str = f"{trend.get('pct_change', 0):+.1f}%"
            iv_str    = f"{iv:.1f}% P{ivp:.0f}" if iv and ivp else "N/A"
            rsi_str   = f"{rsi:.1f}" if rsi else "N/A"
            beta_str  = f"{beta:.2f}" if beta else "N/A"
            pcr_str   = f"{pcr:.2f}" if pcr else "N/A"
            earn_str  = f"{earn.get('days_to_earnings')}d" if earn.get('days_to_earnings') else "N/A"

            lines.append(
                f"\n{ticker} | {strategy} | ${price:.2f} | "
                f"IV {iv_str} | RSI {rsi_str} | Trend {trend_str} | "
                f"Beta {beta_str} | P/C {pcr_str} | Earn {earn_str}"
            )

            if ticker in best_spreads:
                lines.append(f"  → {best_spreads[ticker]}")
            else:
                lines.append(f"  → Sin estructura viable")
    else:
        lines.append("CANDIDATOS: ninguno pasó los filtros hoy")

    lines.append("")

    # ── Open positions summary ────────────────────────────────────────────────
    if open_positions:
        lines.append(f"POSICIONES ABIERTAS ({len(open_positions)}):")
        for p in open_positions:
            exp    = str(p.get("expiration", ""))[:10]
            cost   = float(p.get("total_cost") or 0)
            pnl    = float(p.get("gross_pnl") or 0) if p.get("gross_pnl") else None
            pmax   = float(p.get("profit_pct_of_max") or 0) * 100 if p.get("profit_pct_of_max") else None
            strat  = p.get("strategy", "")
            strat_short = "BCS" if "Call" in strat else "BPS"

            pnl_str  = f"${pnl:+.0f}" if pnl is not None else "?"
            pmax_str = f"{pmax:.0f}% max" if pmax is not None else ""
            dte      = (datetime.strptime(exp, "%Y-%m-%d").date() -
                        datetime.now().date()).days if exp else "?"

            lines.append(
                f"  {p['ticker']} {strat_short} "
                f"${p.get('strike_low')}/{p.get('strike_high')} "
                f"exp {exp} ({dte}d) | "
                f"P&L {pnl_str} {pmax_str}"
            )
    else:
        lines.append("POSICIONES ABIERTAS: ninguna")

    lines.append("")
    lines.append(f"---")
    lines.append(f"Generated {timestamp}")

    content = "\n".join(lines)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(REPORT_AI_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  AI summary → {REPORT_AI_PATH}")
    return REPORT_AI_PATH


def generate_markdown(market_ctx, open_positions, passed_criteria,
                      options_md, eliminated, tier_used):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Scanner Report — {timestamp}",
        f"**Tier usado:** {tier_used}",
        "",
        format_market_context_md(market_ctx),
        "",
        format_open_positions_md(open_positions),
        "",
    ]

    if passed_criteria:
        lines.append(f"## Tickers que pasaron filtros ({len(passed_criteria)})")
        lines.append("")
        for ticker, criteria in passed_criteria.items():
            lines.append(format_criteria_md(ticker, criteria))
            lines.append("")
    else:
        lines += ["## Tickers que pasaron filtros", "",
                  "_Ningún ticker pasó los 5 filtros duros hoy._", ""]

    if options_md:
        lines += ["## Option Selector — Strikes Recomendados", "", options_md, ""]

    if eliminated:
        lines.append(f"## Eliminados por Filtros Duros ({len(eliminated)})")
        lines.append("")
        for ticker, reasons in eliminated.items():
            lines.append(f"- **{ticker}:** {' | '.join(reasons)}")
        lines.append("")

    lines += ["---", f"_Generado {timestamp} · Options Trading System_"]

    content = "\n".join(lines)
    with open(REPORT_MD_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Markdown → {REPORT_MD_PATH}")
    return REPORT_MD_PATH


def generate_html(market_ctx, open_positions, passed_criteria,
                  eliminated, options_md, all_criteria, tier_used):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    def ticker_card(ticker, criteria, passed, reasons=None):
        price  = criteria.get("price", 0)
        tech   = criteria.get("technical", {})
        vol    = criteria.get("volatility", {})
        trend  = tech.get("trend_25d", {})
        ma     = tech.get("moving_averages", {})
        rsi    = tech.get("rsi")
        iv     = vol.get("iv")
        ivp    = vol.get("iv_percentile")
        border = "#22c55e" if passed else "#ef4444"
        tag    = "PASSED" if passed else "FILTERED"
        tag_bg = "#052e16" if passed else "#450a0a"
        rhtml  = (f'<div style="color:#ef4444;font-size:11px;margin-top:6px;">'
                  + " | ".join(reasons) + "</div>") if reasons else ""
        return f"""<div style="background:#111;border:1px solid {border}33;
            border-radius:10px;padding:16px;">
            <div style="display:flex;justify-content:space-between;
                        align-items:center;margin-bottom:8px;">
                <span style="font-size:20px;font-weight:900;color:#f9fafb;">{ticker}</span>
                <span style="font-size:12px;color:#9ca3af;">${price:.2f}</span>
                <span style="background:{tag_bg};border:1px solid {border};
                             border-radius:4px;padding:2px 8px;
                             color:{border};font-size:11px;font-weight:700;">{tag}</span>
            </div>
            <div style="font-size:12px;color:#6b7280;">
                {'BULLISH' if trend.get('is_bullish') else 'BEARISH'}
                {trend.get('pct_change', 0):+.1f}% |
                RSI {f"{rsi:.1f}" if rsi else 'N/A'} |
                IV {f"{iv:.1f}" if iv else 'N/A'}% (P{f"{ivp:.0f}" if ivp else 'N/A'}) |
                {'Above SMA50' if ma.get('above_sma50') else 'Below SMA50'}
            </div>{rhtml}</div>"""

    passed_cards     = "".join(ticker_card(t, c, True)
                                for t, c in passed_criteria.items())
    eliminated_cards = "".join(ticker_card(t, all_criteria.get(t, {}), False, r)
                                for t, r in eliminated.items())

    vix_val       = market_ctx["vix"]["current"]  if market_ctx else "N/A"
    spy_val       = market_ctx["spy"]["price"]    if market_ctx else "N/A"
    spy_trend     = market_ctx["spy"]["trend"]    if market_ctx else "N/A"
    verdict       = market_ctx["verdict"]         if market_ctx else "N/A"
    verdict_color = {"FAVORABLE": "#22c55e", "CAUTION": "#eab308",
                     "DO_NOT_TRADE": "#ef4444"}.get(verdict, "#6b7280")
    options_html  = (f"<pre style='color:#9ca3af;font-size:12px;'>{options_md}</pre>"
                     if options_md else
                     "<div style='color:#6b7280;'>No options data.</div>")
    pos_html = "".join(
        f'<div style="background:#1a1a1a;border-radius:8px;padding:10px 12px;'
        f'margin-bottom:8px;font-size:13px;color:#9ca3af;">'
        f'<strong style="color:#f9fafb;">{p["ticker"]}</strong> '
        f'{p["strategy"]} | ${p["strike_low"]}/{p["strike_high"]} | '
        f'Exp {p["expiration"]} | Costo ${p["total_cost"]}</div>'
        for p in open_positions
    ) if open_positions else '<div style="color:#6b7280;">Ninguna.</div>'

    html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Scanner — {timestamp}</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#f9fafb;
     font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     max-width:900px;margin:0 auto;padding:24px}}
.st{{font-size:11px;color:#4b5563;letter-spacing:3px;text-transform:uppercase;
     margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid #1e1e1e}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}}
.card{{background:#111;border:1px solid #1e1e1e;border-radius:10px;padding:20px}}
.sec{{margin-bottom:40px}}</style></head><body>
<div style="margin-bottom:32px;padding-bottom:24px;border-bottom:1px solid #1e1e1e;">
<h1 style="font-size:28px;font-weight:900;letter-spacing:-1px;">Options Scanner</h1>
<p style="color:#6b7280;font-size:13px;font-family:monospace;margin-top:4px;">
{timestamp} · {tier_used}</p></div>
<div class="sec"><div class="st">Contexto Macro</div>
<div class="card" style="border-color:{verdict_color}33;">
<div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
<div style="width:10px;height:10px;border-radius:50%;background:{verdict_color};"></div>
<span style="font-size:18px;font-weight:900;color:{verdict_color};">{verdict}</span></div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
<div><div style="font-size:10px;color:#6b7280;letter-spacing:2px;">VIX</div>
<div style="font-size:24px;font-weight:900;">{vix_val}</div></div>
<div><div style="font-size:10px;color:#6b7280;letter-spacing:2px;">SPY</div>
<div style="font-size:24px;font-weight:900;">${spy_val}</div>
<div style="font-size:12px;color:#22c55e;">{spy_trend}</div></div>
</div></div></div>
<div class="sec"><div class="st">Posiciones Abiertas</div>{pos_html}</div>
<div class="sec"><div class="st">Pasaron Filtros ({len(passed_criteria)})</div>
<div class="grid">{passed_cards or '<div style="color:#6b7280;">Ninguno.</div>'}</div></div>
<div class="sec"><div class="st">Option Selector — Strikes</div>
<div class="card">{options_html}</div></div>
<div class="sec"><div class="st">Eliminados ({len(eliminated)})</div>
<div class="grid">{eliminated_cards or '<div style="color:#6b7280;">Ninguno.</div>'}</div></div>
<div style="text-align:center;color:#374151;font-size:11px;
            padding:24px 0;border-top:1px solid #1e1e1e;font-family:monospace;">
{timestamp} · Options Trading System · No AI inside scanner</div>
</body></html>"""

    with open(REPORT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML    → {REPORT_HTML_PATH}")
    return REPORT_HTML_PATH


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCANNER
# ══════════════════════════════════════════════════════════════════════════════

def scan_tickers(tickers, tt_session, market_ctx):
    all_criteria    = {}
    passed_criteria = {}
    eliminated      = {}
    failed          = []

    for ticker in tickers:
        print(f"  {ticker}...", end=" ", flush=True)
        try:
            criteria = get_all_criteria(ticker)
            if criteria is None:
                print("no data")
                failed.append(ticker)
                continue

            all_criteria[ticker] = criteria
            passed, reasons = passes_hard_filters(criteria)
            if passed:
                passed_criteria[ticker] = criteria
                print(f"OK ${criteria['price']:.2f} ✓ PASSED")
            else:
                eliminated[ticker] = reasons
                print(f"OK ${criteria['price']:.2f} ✗ {reasons[0]}")

        except Exception as e:
            print(f"ERROR {e}")
            failed.append(ticker)

    return all_criteria, passed_criteria, eliminated, failed


def run_scan(tickers, expand_to_universe=False):
    timestamp  = datetime.now().strftime("%Y-%m-%d %H:%M")
    market_ctx = load_market_context()

    print(f"\n{'=' * 65}")
    print(f"  SCANNER — {timestamp}")
    if market_ctx:
        print(f"  Context: {market_ctx.get('verdict', 'N/A')} | "
              f"VIX {market_ctx['vix']['current']:.1f} | "
              f"SPY {market_ctx['spy']['trend']}")
    print(f"{'=' * 65}\n")

    tt_session = None
    try:
        tt_session = Session(os.getenv("TASTYTRADE_CLIENT_SECRET"),
                             os.getenv("TASTYTRADE_REFRESH_TOKEN"))
        print("  Tastytrade session: OK\n")
    except Exception as e:
        print(f"  Tastytrade session: FAILED ({e})\n")

    # Tier 1
    print(f"  TIER 1 — Stars ({len(tickers)} tickers)")
    print(f"  {', '.join(tickers[:10])}{'...' if len(tickers) > 10 else ''}\n")
    all_criteria, passed_criteria, eliminated, failed = scan_tickers(
        tickers, tt_session, market_ctx)
    tier_used = f"Tier 1 — Stars ({len(tickers)} tickers)"

    # Tier 2 — only if requested and Tier 1 insufficient
    if expand_to_universe and len(passed_criteria) < TIER1_MIN_CANDIDATES:
        print(f"\n  Tier 1: {len(passed_criteria)} candidates "
              f"(min {TIER1_MIN_CANDIDATES}) → expanding to S&P 500\n")
        try:
            from universe import get_scanner_candidates
            universe_tickers = [t for t in get_scanner_candidates()
                                 if t not in all_criteria]
            print(f"  TIER 2 — Universe ({len(universe_tickers)} new tickers)\n")
            ac2, pc2, el2, fa2 = scan_tickers(universe_tickers, tt_session, market_ctx)
            all_criteria.update(ac2)
            passed_criteria.update(pc2)
            eliminated.update(el2)
            failed.extend(fa2)
            tier_used = (f"Tier 1 ({len(tickers)}) + "
                         f"Tier 2 Universe ({len(universe_tickers)})")
        except Exception as e:
            print(f"  Universe expansion failed: {e}")

    print(f"\n  {'=' * 50}")
    print(f"  Analyzed: {len(all_criteria)} | Passed: {len(passed_criteria)} | "
          f"Eliminated: {len(eliminated)} | Failed: {len(failed)}")

    # Option selector
    options_md = ""
    if passed_criteria and tt_session:
        print(f"\n  Running option_selector for {len(passed_criteria)} tickers...")
        options_md = get_options_for_tickers(tt_session, passed_criteria)
    elif not passed_criteria:
        print("\n  No tickers passed filters — skipping option_selector")

    # Open positions + concentration check
    open_positions  = get_open_positions()
    sector_warnings = check_sector_concentration(open_positions, passed_criteria)
    if sector_warnings:
        print("\n  SECTOR CONCENTRATION WARNINGS:")
        for w in sector_warnings:
            print(f"    {w}")
        warn_block = "\n## ⚠️ Advertencias de Concentración Sectorial\n\n"
        warn_block += "\n".join(sector_warnings) + "\n"
        options_md = warn_block + options_md

    # Reports
    print("\n  Generating reports...")
    md_path   = generate_markdown(market_ctx, open_positions, passed_criteria,
                                   options_md, eliminated, tier_used)
    html_path = generate_html(market_ctx, open_positions, passed_criteria,
                               eliminated, options_md, all_criteria, tier_used)
    ai_path   = generate_ai_summary(market_ctx, open_positions, passed_criteria,
                                     options_md, tier_used)

    print(f"\n{'=' * 65}")
    print(f"  SCAN COMPLETE — {tier_used}")
    print(f"  Passed:    {len(passed_criteria)} tickers")
    print(f"  Markdown:  {md_path}")
    print(f"  HTML:      {html_path}")
    print(f"  AI:        {ai_path}")
    if failed:
        print(f"  Failed:    {', '.join(failed)}")
    print(f"{'=' * 65}\n")

    _open_browser(html_path)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Options opportunity scanner")
    parser.add_argument("--tickers",  nargs="+", default=None)
    parser.add_argument("--universe", action="store_true",
                        help="Expand to full S&P 500 if Tier 1 yields < 2 candidates")
    parser.add_argument("--context",  action="store_true",
                        help="Use tickers from market_context.json (legacy)")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    if args.tickers:
        run_scan(args.tickers, expand_to_universe=False)
    elif args.universe:
        # Scan full S&P 500 directly — no Tier 1 gate
        try:
            from universe import get_scanner_candidates
            universe_tickers = get_scanner_candidates(use_cache=not args.no_cache)
            if not universe_tickers:
                print("  Could not build universe — falling back to Tier 1 stars")
                universe_tickers = TIER1_STARS
        except Exception as e:
            print(f"  Universe import failed ({e}) — falling back to Tier 1 stars")
            universe_tickers = TIER1_STARS
        print(f"\n  UNIVERSE MODE — {len(universe_tickers)} tickers from S&P 500\n")
        run_scan(universe_tickers, expand_to_universe=False)
    elif args.context:
        ctx = load_market_context()
        tickers = (ctx.get("recommended_tickers") if ctx else None) or TIER1_STARS
        if ctx and ctx.get("recommended_tickers"):
            print(f"  Context mode: {len(tickers)} tickers from priority sectors")
        else:
            print("  market_context.json not found — using Tier 1 stars")
        run_scan(tickers, expand_to_universe=False)
    else:
        # Default: Tier 1 stars only
        run_scan(TIER1_STARS, expand_to_universe=False)