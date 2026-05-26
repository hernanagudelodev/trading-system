"""
report_generator.py
===================
Generates scanner reports from scan results.

Outputs TWO files:
    scanner_report.md   — markdown (token-efficient, for sharing with AI)
    scanner_report.html — HTML (for browser viewing)

Key changes vs previous version:
    - Markdown report only includes tickers with actionable strategy (not "No trade")
    - HTML report still shows all tickers for completeness
    - Markdown is the primary output — compact, readable, token-efficient

Usage (from scanner.py):
    from report_generator import generate_report
    generate_report(all_scored, ai_text, market_context=None)
"""

import os
import re
from datetime import datetime


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT PATHS
# ══════════════════════════════════════════════════════════════════════════════

REPORT_HTML_PATH = os.path.join(os.path.dirname(__file__), "scanner_report.html")
REPORT_MD_PATH   = os.path.join(os.path.dirname(__file__), "scanner_report.md")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def verdict_color(verdict):
    return {
        "VIABLE":       "#22c55e",
        "CAUTION":      "#f59e0b",
        "DO_NOT_TRADE": "#ef4444",
        "ANALYZED":     "#6b7280",
    }.get(verdict, "#6b7280")

def verdict_bg(verdict):
    return {
        "VIABLE":       "#052e16",
        "CAUTION":      "#451a03",
        "DO_NOT_TRADE": "#450a0a",
        "ANALYZED":     "#1a1a1a",
    }.get(verdict, "#1a1a1a")

def verdict_emoji(verdict):
    return {
        "VIABLE":       "VIABLE",
        "CAUTION":      "CAUTION",
        "DO_NOT_TRADE": "NO TRADE",
        "ANALYZED":     "ANALYZED",
    }.get(verdict, "ANALYZED")


def is_no_trade(ai_text, ticker):
    """
    Detect if AI recommended 'No trade' for a specific ticker.
    Looks for patterns like 'TICKER - NO TRADE' or 'STRATEGY: No trade'
    in the AI interpretation text.
    """
    if not ai_text:
        return False

    text_upper = ai_text.upper()
    ticker_upper = ticker.upper()

    # Common patterns the AI uses
    patterns = [
        f"{ticker_upper} - NO TRADE",
        f"{ticker_upper} — NO TRADE",
        f"{ticker_upper}: NO TRADE",
        f"{ticker_upper} - NO",
        f"NO TRADE\n",
    ]

    # Check around ticker mention
    idx = text_upper.find(ticker_upper)
    if idx >= 0:
        # Look at the 200 chars after the ticker mention
        snippet = text_upper[idx:idx+200]
        if "NO TRADE" in snippet or "NO_TRADE" in snippet:
            return True

    return False


def markdown_to_html(text):
    """Convert markdown to HTML for the AI box."""
    if not text:
        return "No AI interpretation available."

    lines = text.split("\n")
    html_lines = []

    for line in lines:
        if line.startswith("## "):
            content = line[3:].strip()
            html_lines.append(
                f'<h2 style="color:#f9fafb;font-size:16px;font-weight:900;'
                f'margin:20px 0 8px;letter-spacing:-0.5px;">{content}</h2>'
            )
        elif line.startswith("### "):
            content = line[4:].strip()
            html_lines.append(
                f'<h3 style="color:#94a3b8;font-size:14px;font-weight:700;'
                f'margin:16px 0 6px;letter-spacing:0.5px;text-transform:uppercase;">'
                f'{content}</h3>'
            )
        elif line.strip() == "---":
            html_lines.append(
                '<hr style="border:none;border-top:1px solid #1e3a5f;margin:16px 0;">'
            )
        elif line.startswith("- "):
            content = line[2:].strip()
            content = re.sub(r'\*\*(.*?)\*\*',
                             r'<strong style="color:#f9fafb;">\1</strong>', content)
            html_lines.append(
                f'<div style="display:flex;gap:8px;margin:4px 0 4px 8px;">'
                f'<span style="color:#3b82f6;flex-shrink:0;">></span>'
                f'<span>{content}</span></div>'
            )
        elif re.match(r'^\d+\.\s', line):
            content = re.sub(r'^\d+\.\s', '', line)
            num     = re.match(r'^(\d+)\.', line).group(1)
            content = re.sub(r'\*\*(.*?)\*\*',
                             r'<strong style="color:#f9fafb;">\1</strong>', content)
            html_lines.append(
                f'<div style="display:flex;gap:10px;margin:6px 0 6px 8px;">'
                f'<span style="color:#3b82f6;font-weight:700;flex-shrink:0;">{num}.</span>'
                f'<span>{content}</span></div>'
            )
        elif line.strip() == "":
            html_lines.append('<div style="height:8px;"></div>')
        else:
            content = re.sub(r'\*\*(.*?)\*\*',
                             r'<strong style="color:#f9fafb;">\1</strong>', line)
            html_lines.append(f'<p style="margin:4px 0;">{content}</p>')

    return "\n".join(html_lines)


# ══════════════════════════════════════════════════════════════════════════════
# MARKDOWN REPORT — token-efficient, only actionable tickers
# ══════════════════════════════════════════════════════════════════════════════

def generate_markdown_report(actionable, ai_text, market_context=None):
    """
    Generate scanner_report.md — compact markdown for sharing with AI.

    Only includes tickers where AI recommended an actionable strategy.
    No mention of 'No trade' tickers — they are completely excluded.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines     = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append(f"# Scanner Report — {timestamp}")
    lines.append("")

    # ── Market Context ────────────────────────────────────────────────────────
    if market_context:
        vix     = market_context.get("vix", {})
        spy     = market_context.get("spy", {})
        verdict = market_context.get("verdict", "N/A")

        lines.append("## Contexto Macro")
        lines.append(f"**Verdict:** {verdict}")
        lines.append(
            f"**VIX:** {vix.get('current', 'N/A')} "
            f"({vix.get('level', 'N/A')}, {vix.get('trend', 'N/A')})"
        )
        lines.append(
            f"**SPY:** ${spy.get('price', 'N/A')} | "
            f"{spy.get('trend', 'N/A')} ({spy.get('pct_25d', 0):+.1f}% 25d)"
        )
        priority = ", ".join(market_context.get("priority_sectors", []))
        if priority:
            lines.append(f"**Sectores prioritarios:** {priority}")
        lines.append("")

    # ── AI Interpretation ─────────────────────────────────────────────────────
    lines.append("## Interpretacion AI")
    lines.append("")
    if ai_text:
        lines.append(ai_text)
    else:
        lines.append("_No hay interpretacion AI disponible._")
    lines.append("")

    # ── Ticker detail — ONLY actionable tickers ───────────────────────────────
    if actionable:
        lines.append(f"## Tickers con Oportunidad ({len(actionable)})")
        lines.append("")

        for s in actionable:
            ticker = s.get("ticker", "")
            price  = s.get("price", 0)
            scores = s.get("criteria_scores", {})

            lines.append(f"### {ticker} — ${price:.2f}")
            lines.append("")

            technical_keys   = ["trend_25d", "moving_averages", "rsi",
                                 "week_52", "support", "resistance", "candlestick"]
            volatility_keys  = ["hv_30d", "iv", "beta",
                                 "put_call_ratio", "open_interest"]
            operational_keys = ["earnings", "volume"]
            fundamental_keys = ["pe", "eps_growth", "debt_equity", "profit_margin"]

            def render_group(title, keys):
                rows = []
                for k in keys:
                    if k in scores:
                        label = scores[k].get("label", "N/A")
                        rows.append(f"- **{k}:** {label}")
                if rows:
                    return [f"**{title}**"] + rows + [""]
                return []

            lines += render_group("Technical",   technical_keys)
            lines += render_group("Volatility",  volatility_keys)
            lines += render_group("Operational", operational_keys)
            lines += render_group("Fundamental", fundamental_keys)

    else:
        lines.append("## Tickers con Oportunidad")
        lines.append("")
        lines.append("_No hay tickers con estrategia recomendada hoy._")
        lines.append("")

    # ── Footer ────────────────────────────────────────────────────────────────
    lines.append("---")
    lines.append(f"_Generado {timestamp} · Options Trading System_")

    content = "\n".join(lines)

    with open(REPORT_MD_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Markdown report saved -> {REPORT_MD_PATH}")
    return REPORT_MD_PATH


# ══════════════════════════════════════════════════════════════════════════════
# HTML HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def render_context_banner(market_context):
    if not market_context:
        return ""

    vix     = market_context.get("vix", {})
    spy     = market_context.get("spy", {})
    verdict = market_context.get("verdict", "N/A")
    detail  = market_context.get("verdict_detail", "")

    verdict_colors = {
        "FAVORABLE":    "#22c55e",
        "NEUTRAL":      "#f59e0b",
        "DO_NOT_TRADE": "#ef4444",
    }
    color = verdict_colors.get(verdict, "#6b7280")

    sectors = market_context.get("sectors", [])
    sector_rows = ""
    for s in sectors:
        pri = s.get("priority", "")
        pri_color = "#22c55e" if pri == "PRIORITY" else \
                    "#f59e0b" if pri == "ACCEPTABLE" else "#ef4444"
        sector_rows += (
            f'<tr><td style="padding:4px 12px;color:#9ca3af;">{s["sector"]}</td>'
            f'<td style="padding:4px 12px;color:#f9fafb;">{s["win_rate"]:.1f}%</td>'
            f'<td style="padding:4px 12px;color:{pri_color};font-size:11px;">'
            f'{pri}</td></tr>'
        )

    chips = " ".join(
        f'<span style="background:#1a1a1a;border:1px solid #2a2a2a;'
        f'border-radius:20px;padding:4px 12px;font-size:12px;color:#9ca3af;">'
        f'{t}</span>'
        for t in market_context.get("recommended_tickers", [])[:20]
    )

    return f"""
    <div style="background:#111;border:1px solid {color}33;border-radius:16px;
                padding:24px;margin-bottom:32px;">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;">
            <div style="width:12px;height:12px;border-radius:50%;
                        background:{color};box-shadow:0 0 8px {color};"></div>
            <span style="font-size:20px;font-weight:900;color:{color};">{verdict}</span>
            <span style="color:#6b7280;font-size:13px;">{detail}</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 2fr;gap:16px;
                    margin-bottom:16px;">
            <div style="background:#1a1a1a;border-radius:10px;padding:12px 16px;">
                <div style="font-size:10px;color:#6b7280;letter-spacing:2px;
                            text-transform:uppercase;margin-bottom:6px;">VIX</div>
                <div style="font-size:22px;font-weight:900;color:#f9fafb;">
                    {vix.get('current', 'N/A')}
                </div>
                <div style="font-size:11px;color:#6b7280;margin-top:2px;">
                    {vix.get('level','')}, {vix.get('trend','')}
                </div>
            </div>
            <div style="background:#1a1a1a;border-radius:10px;padding:12px 16px;">
                <div style="font-size:10px;color:#6b7280;letter-spacing:2px;
                            text-transform:uppercase;margin-bottom:6px;">SPY</div>
                <div style="font-size:22px;font-weight:900;color:#f9fafb;">
                    ${spy.get('price', 'N/A')}
                </div>
                <div style="font-size:11px;color:#22c55e;margin-top:2px;">
                    {spy.get('trend','')} ({spy.get('pct_25d',0):+.1f}% 25d)
                </div>
            </div>
            <div style="background:#1a1a1a;border-radius:10px;padding:12px 16px;
                        overflow-y:auto;max-height:120px;">
                <div style="font-size:10px;color:#6b7280;letter-spacing:2px;
                            text-transform:uppercase;margin-bottom:6px;">
                    SECTORS — WIN RATE HISTORY
                </div>
                <table style="width:100%;border-collapse:collapse;">
                    {sector_rows}
                </table>
            </div>
        </div>
        <div style="font-size:10px;color:#6b7280;letter-spacing:2px;
                    text-transform:uppercase;margin-bottom:8px;">
            RECOMMENDED TICKERS — {len(market_context.get('recommended_tickers',[]))} FROM PRIORITY SECTORS
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px;">
            {chips}
        </div>
    </div>"""


def render_ticker_card(scored, ai_text=""):
    ticker  = scored.get("ticker", "")
    price   = scored.get("price", 0)
    verdict = scored.get("verdict", "ANALYZED")
    scores  = scored.get("criteria_scores", {})

    color  = verdict_color(verdict)
    bg     = verdict_bg(verdict)
    label  = verdict_emoji(verdict)

    # Detect no trade from AI
    if is_no_trade(ai_text, ticker):
        color = "#6b7280"
        label = "NO TRADE"

    # Criteria rows
    categories = {
        "TECHNICAL":   ["trend_25d", "moving_averages", "rsi",
                        "week_52", "support", "resistance", "candlestick"],
        "VOLATILITY":  ["hv_30d", "iv", "beta",
                        "put_call_ratio", "open_interest"],
        "OPERATIONAL": ["earnings", "volume"],
        "FUNDAMENTAL": ["pe", "eps_growth", "debt_equity", "profit_margin"],
    }

    criteria_html = ""
    for cat, keys in categories.items():
        rows = ""
        for k in keys:
            if k not in scores:
                continue
            label_val = scores[k].get("label", "N/A")
            rows += (
                f'<tr style="border-bottom:1px solid #1a1a1a;">'
                f'<td style="padding:5px 8px;color:#6b7280;font-size:11px;'
                f'white-space:nowrap;">{k}</td>'
                f'<td style="padding:5px 8px;color:#e2e8f0;font-size:12px;">'
                f'{label_val}</td></tr>'
            )
        if rows:
            criteria_html += (
                f'<div style="margin-bottom:10px;">'
                f'<div style="font-size:9px;color:#4b5563;letter-spacing:2px;'
                f'text-transform:uppercase;padding:4px 8px;">{cat}</div>'
                f'<table style="width:100%;border-collapse:collapse;">{rows}</table>'
                f'</div>'
            )

    return f"""
    <div style="background:#111;border:1px solid {color}33;border-radius:12px;
                padding:20px;">
        <div style="display:flex;justify-content:space-between;
                    align-items:center;margin-bottom:14px;">
            <div>
                <span style="font-size:26px;font-weight:900;color:#f9fafb;
                             letter-spacing:-1px;">{ticker}</span>
                <span style="font-size:14px;color:#6b7280;margin-left:8px;">
                    ${price:.2f}
                </span>
            </div>
            <div style="background:{bg};border:1px solid {color};
                        border-radius:6px;padding:4px 12px;">
                <span style="color:{color};font-weight:700;font-size:12px;">
                    {label}
                </span>
            </div>
        </div>
        {criteria_html}
    </div>"""


def render_summary_table(all_scored, ai_text=""):
    rows = ""
    for s in all_scored:
        ticker  = s.get("ticker", "")
        price   = s.get("price", 0)
        verdict = s.get("verdict", "ANALYZED")
        color   = verdict_color(verdict)

        tag = "NO TRADE" if is_no_trade(ai_text, ticker) else verdict_emoji(verdict)
        if is_no_trade(ai_text, ticker):
            color = "#6b7280"

        rows += f"""
            <tr style="border-bottom:1px solid #1e1e1e;"
                onmouseover="this.style.background='#1a1a1a'"
                onmouseout="this.style.background='transparent'">
                <td style="padding:10px 16px;color:#f9fafb;font-weight:700;">
                    {ticker}
                </td>
                <td style="padding:10px 16px;color:#9ca3af;">
                    ${price:.2f}
                </td>
                <td style="padding:10px 16px;">
                    <span style="color:{color};">{tag}</span>
                </td>
            </tr>"""

    return f"""
        <table style="width:100%;border-collapse:collapse;">
            <thead>
                <tr style="border-bottom:1px solid #2a2a2a;">
                    <th style="padding:10px 16px;text-align:left;color:#6b7280;
                               font-size:11px;letter-spacing:2px;">TICKER</th>
                    <th style="padding:10px 16px;text-align:left;color:#6b7280;
                               font-size:11px;letter-spacing:2px;">PRICE</th>
                    <th style="padding:10px 16px;text-align:left;color:#6b7280;
                               font-size:11px;letter-spacing:2px;">VERDICT</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>"""


# ══════════════════════════════════════════════════════════════════════════════
# HTML REPORT — full detail, all tickers
# ══════════════════════════════════════════════════════════════════════════════

def generate_html_report(all_scored, ai_text, market_context=None):
    """Generate scanner_report.html with full detail for all tickers."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    viable   = sum(1 for s in all_scored if s.get("verdict") == "VIABLE")
    caution  = sum(1 for s in all_scored if s.get("verdict") == "CAUTION")
    no_trade = sum(1 for s in all_scored if s.get("verdict") == "DO_NOT_TRADE")

    ticker_cards = "\n".join(render_ticker_card(s, ai_text) for s in all_scored)
    ai_html      = markdown_to_html(ai_text)
    ctx_banner   = render_context_banner(market_context)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Scanner Report -- {timestamp}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: #0a0a0a;
            color: #f9fafb;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            min-height: 100vh;
            padding: 32px 24px;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        .grid-3 {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
            gap: 20px;
        }}
        .section-title {{
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 3px;
            color: #6b7280;
            text-transform: uppercase;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 1px solid #1e1e1e;
        }}
        .stat-card {{
            background: #111;
            border: 1px solid #1e1e1e;
            border-radius: 10px;
            padding: 16px 20px;
            text-align: center;
        }}
        .ai-box {{
            background: #0f1629;
            border: 1px solid #1e3a5f;
            border-radius: 12px;
            padding: 28px 32px;
            line-height: 1.75;
            color: #cbd5e1;
            font-size: 14px;
        }}
        .summary-box {{
            background: #111;
            border: 1px solid #1e1e1e;
            border-radius: 12px;
            overflow: hidden;
        }}
    </style>
</head>
<body>
<div class="container">
    <div style="margin-bottom:32px;border-bottom:1px solid #1e1e1e;padding-bottom:24px;">
        <div style="display:flex;justify-content:space-between;
                    align-items:flex-end;flex-wrap:wrap;gap:12px;">
            <div>
                <h1 style="font-size:32px;font-weight:900;
                           letter-spacing:-1.5px;color:#f9fafb;">
                    Options Scanner
                </h1>
                <p style="color:#6b7280;margin-top:4px;font-size:14px;
                          font-family:monospace;">{timestamp}</p>
            </div>
            <div style="display:flex;gap:12px;flex-wrap:wrap;">
                <div class="stat-card">
                    <div style="font-size:28px;font-weight:900;color:#22c55e;">{viable}</div>
                    <div style="font-size:11px;color:#6b7280;letter-spacing:1px;
                                margin-top:2px;">VIABLE</div>
                </div>
                <div class="stat-card">
                    <div style="font-size:28px;font-weight:900;color:#f59e0b;">{caution}</div>
                    <div style="font-size:11px;color:#6b7280;letter-spacing:1px;
                                margin-top:2px;">CAUTION</div>
                </div>
                <div class="stat-card">
                    <div style="font-size:28px;font-weight:900;color:#ef4444;">{no_trade}</div>
                    <div style="font-size:11px;color:#6b7280;letter-spacing:1px;
                                margin-top:2px;">DO NOT TRADE</div>
                </div>
                <div class="stat-card">
                    <div style="font-size:28px;font-weight:900;color:#9ca3af;">
                        {len(all_scored)}
                    </div>
                    <div style="font-size:11px;color:#6b7280;letter-spacing:1px;
                                margin-top:2px;">TOTAL</div>
                </div>
            </div>
        </div>
    </div>

    {ctx_banner}

    <div style="margin-bottom:40px;">
        <div class="section-title">Summary</div>
        <div class="summary-box">
            {render_summary_table(all_scored, ai_text)}
        </div>
    </div>

    <div style="margin-bottom:40px;">
        <div class="section-title">AI Interpretation</div>
        <div class="ai-box">{ai_html}</div>
    </div>

    <div style="margin-bottom:40px;">
        <div class="section-title">Ticker Detail</div>
        <div class="grid-3">
            {ticker_cards}
        </div>
    </div>

    <div style="text-align:center;color:#374151;font-size:12px;
                padding:24px 0;border-top:1px solid #1e1e1e;
                font-family:monospace;">
        Generated {timestamp} · Options Trading System
    </div>

</div>
</body>
</html>"""

    with open(REPORT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"HTML report saved  -> {REPORT_HTML_PATH}")
    return REPORT_HTML_PATH


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT — called by scanner.py
# ══════════════════════════════════════════════════════════════════════════════

def generate_report(all_scored, ai_text="", market_context=None, actionable=None):
    """
    Generate both markdown and HTML reports.

    Args:
        all_scored      (list) — ALL scored ticker dicts (for HTML)
        ai_text         (str)  — AI interpretation text (markdown)
        market_context  (dict) — structured JSON from market_context.json
        actionable      (list) — only tickers with actionable strategy (for markdown)
                                 if None, uses all_scored filtered by is_no_trade

    Returns:
        tuple (md_path, html_path)
    """
    # If no actionable list provided, filter automatically
    if actionable is None:
        actionable = [s for s in all_scored if not is_no_trade(ai_text, s.get("ticker", ""))]

    md_path   = generate_markdown_report(actionable, ai_text, market_context)
    html_path = generate_html_report(all_scored, ai_text, market_context)
    return md_path, html_path