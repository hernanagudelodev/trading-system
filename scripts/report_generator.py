"""
report_generator.py
===================
Generates scanner_report.html from scan results.

Called by scanner.py after each scan. Overwrites the file each time.
Open scanner_report.html in any browser to view results with full emoji support.

Fixes vs previous version:
    - Markdown rendered in AI box (###, **, ---)
    - Market context banner uses structured JSON fields (no raw dict)
    - Score column hidden when score_max = 0 (no scoring mode)
    - +0 column removed from criteria rows

Usage (from scanner.py):
    from report_generator import generate_report
    generate_report(all_scored, ai_text, market_context=None)
"""

import os
import re
from datetime import datetime


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT PATH
# ══════════════════════════════════════════════════════════════════════════════

REPORT_PATH = os.path.join(os.path.dirname(__file__), "scanner_report.html")


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
        "VIABLE":       "✅",
        "CAUTION":      "⚠️",
        "DO_NOT_TRADE": "❌",
        "ANALYZED":     "—",
    }.get(verdict, "—")

def markdown_to_html(text):
    """
    Convert markdown-style text to HTML for the AI box.
    Handles: ### headers, ## headers, **bold**, --- dividers, bullet lists.
    """
    if not text:
        return "No AI interpretation available."

    lines = text.split("\n")
    html_lines = []

    for line in lines:
        # H2
        if line.startswith("## "):
            content = line[3:].strip()
            html_lines.append(
                f'<h2 style="color:#f9fafb;font-size:16px;font-weight:900;'
                f'margin:20px 0 8px;letter-spacing:-0.5px;">{content}</h2>'
            )
        # H3
        elif line.startswith("### "):
            content = line[4:].strip()
            html_lines.append(
                f'<h3 style="color:#e2e8f0;font-size:14px;font-weight:700;'
                f'margin:16px 0 6px;letter-spacing:0.5px;text-transform:uppercase;'
                f'color:#94a3b8;">{content}</h3>'
            )
        # Horizontal rule
        elif line.strip() == "---":
            html_lines.append(
                '<hr style="border:none;border-top:1px solid #1e3a5f;margin:16px 0;">'
            )
        # Bullet list item
        elif line.startswith("- "):
            content = line[2:].strip()
            # Apply bold inside bullet
            content = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color:#f9fafb;">\1</strong>', content)
            html_lines.append(
                f'<div style="display:flex;gap:8px;margin:4px 0 4px 8px;">'
                f'<span style="color:#3b82f6;flex-shrink:0;">›</span>'
                f'<span>{content}</span></div>'
            )
        # Numbered list item
        elif re.match(r'^\d+\.\s', line):
            content = re.sub(r'^\d+\.\s', '', line)
            num = re.match(r'^(\d+)\.', line).group(1)
            content = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color:#f9fafb;">\1</strong>', content)
            html_lines.append(
                f'<div style="display:flex;gap:10px;margin:6px 0 6px 8px;">'
                f'<span style="color:#3b82f6;font-weight:700;flex-shrink:0;">{num}.</span>'
                f'<span>{content}</span></div>'
            )
        # Empty line
        elif line.strip() == "":
            html_lines.append('<div style="height:8px;"></div>')
        # Normal paragraph
        else:
            # Apply bold
            content = re.sub(r'\*\*(.*?)\*\*', r'<strong style="color:#f9fafb;">\1</strong>', line)
            html_lines.append(f'<p style="margin:4px 0;">{content}</p>')

    return "\n".join(html_lines)


# ══════════════════════════════════════════════════════════════════════════════
# MARKET CONTEXT BANNER
# ══════════════════════════════════════════════════════════════════════════════

def render_context_banner(market_context):
    """
    Render macro context banner using structured JSON fields.
    Never shows raw Python dicts.
    """
    if not market_context:
        return ""

    verdict = market_context.get("verdict", "")
    detail  = market_context.get("verdict_detail", "")

    vix = market_context.get("vix", {})
    spy = market_context.get("spy", {})

    vix_current = vix.get("current", "N/A")
    vix_level   = vix.get("level", "")
    vix_trend   = vix.get("trend", "")

    spy_price   = spy.get("price", "N/A")
    spy_trend   = spy.get("trend", "")
    spy_pct     = spy.get("pct_25d")
    spy_sma     = spy.get("sma_status", "")

    v_color = "#22c55e" if "FAVORABLE" in verdict else \
              "#f59e0b" if "CAUTION"   in verdict else "#ef4444"
    v_emoji = "🟢" if "FAVORABLE" in verdict else \
              "🟡" if "CAUTION"   in verdict else "🔴"

    spy_str = f"${spy_price} | {spy_trend}"
    if spy_pct is not None:
        spy_str += f" ({spy_pct:+.1f}% 25d)"
    spy_str += f" | {spy_sma}"

    vix_str = f"{vix_current} ({vix_level}"
    if vix_trend:
        vix_str += f", {vix_trend}"
    vix_str += ")"

    priority = market_context.get("priority_sectors", [])
    priority_str = " · ".join(priority[:4]) if priority else ""

    return f"""
        <div style="background:#111;border:1px solid {v_color}44;border-radius:12px;
                    padding:16px 24px;margin-bottom:24px;">
            <div style="display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap;">
                <div style="font-size:28px;line-height:1;">{v_emoji}</div>
                <div style="flex:1;min-width:200px;">
                    <div style="color:{v_color};font-weight:900;font-size:16px;
                                letter-spacing:1px;">{verdict}</div>
                    <div style="color:#9ca3af;font-size:13px;margin-top:4px;">{detail}</div>
                </div>
                <div style="display:flex;gap:24px;flex-wrap:wrap;">
                    <div>
                        <div style="font-size:10px;color:#6b7280;letter-spacing:2px;
                                    text-transform:uppercase;margin-bottom:2px;">VIX</div>
                        <div style="font-size:13px;color:#f9fafb;font-family:monospace;">
                            {vix_str}
                        </div>
                    </div>
                    <div>
                        <div style="font-size:10px;color:#6b7280;letter-spacing:2px;
                                    text-transform:uppercase;margin-bottom:2px;">SPY</div>
                        <div style="font-size:13px;color:#f9fafb;font-family:monospace;">
                            {spy_str}
                        </div>
                    </div>
                    {f'''<div>
                        <div style="font-size:10px;color:#6b7280;letter-spacing:2px;
                                    text-transform:uppercase;margin-bottom:2px;">PRIORITY SECTORS</div>
                        <div style="font-size:13px;color:#22c55e;font-family:monospace;">
                            {priority_str}
                        </div>
                    </div>''' if priority_str else ""}
                </div>
            </div>
        </div>"""


# ══════════════════════════════════════════════════════════════════════════════
# TICKER CARD
# ══════════════════════════════════════════════════════════════════════════════

def render_ticker_card(scored):
    verdict   = scored.get("verdict", "ANALYZED")
    color     = verdict_color(verdict)
    bg        = verdict_bg(verdict)
    emoji     = verdict_emoji(verdict)
    score     = scored.get("score", 0)
    score_max = scored.get("score_max", 0)
    score_pct = scored.get("score_pct", 0)

    # Hide score bar when not using scoring (score_max == 0)
    show_score = score_max > 0

    score_html = ""
    if show_score:
        pct = score / score_max * 100 if score_max > 0 else 0
        bar_color = "#22c55e" if pct >= 68 else "#f59e0b" if pct >= 35 else "#ef4444"
        score_html = f"""
            <div style="margin-bottom:16px;">
                <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
                    <span style="font-size:12px;color:#9ca3af;">Score</span>
                    <span style="font-size:12px;color:{color};font-weight:700;">
                        {score}/{score_max} ({score_pct}%)
                    </span>
                </div>
                <div style="background:#1e1e1e;border-radius:4px;height:6px;width:100%;">
                    <div style="background:{bar_color};width:{pct:.1f}%;
                                height:6px;border-radius:4px;"></div>
                </div>
            </div>"""

    categories = {
        "TECHNICAL":   ["trend_25d", "moving_averages", "rsi", "week_52",
                        "support", "resistance", "candlestick"],
        "VOLATILITY":  ["hv_30d", "iv", "beta", "put_call_ratio", "open_interest"],
        "OPERATIONAL": ["earnings", "volume"],
        "FUNDAMENTAL": ["pe", "eps_growth", "debt_equity", "profit_margin"],
    }

    criteria_html = ""
    criteria_scores = scored.get("criteria_scores", {})

    for cat, keys in categories.items():
        rows = ""
        for key in keys:
            if key not in criteria_scores:
                continue
            c     = criteria_scores[key]
            label = c.get("label", "N/A")
            s     = c.get("score", 0)

            # Color label based on content
            if "BULLISH" in str(label).upper() or "RISING" in str(label).upper() or "ABOVE" in str(label).upper():
                label_color = "#22c55e"
            elif "BEARISH" in str(label).upper() or "FALLING" in str(label).upper() or "BELOW" in str(label).upper():
                label_color = "#ef4444"
            else:
                label_color = "#9ca3af"

            rows += f"""
                <tr style="border-bottom:1px solid #1e1e1e;">
                    <td style="padding:5px 8px;color:#6b7280;font-size:11px;
                               font-family:monospace;white-space:nowrap;">{key}</td>
                    <td style="padding:5px 8px;color:{label_color};font-size:12px;">
                        {label}
                    </td>
                </tr>"""

        if rows:
            criteria_html += f"""
                <div style="margin-bottom:14px;">
                    <div style="font-size:10px;font-weight:700;letter-spacing:2px;
                                color:#4b5563;margin-bottom:6px;
                                text-transform:uppercase;">{cat}</div>
                    <table style="width:100%;border-collapse:collapse;">{rows}</table>
                </div>"""

    return f"""
        <div style="background:#111;border:1px solid {color}22;border-radius:12px;
                    padding:20px;">
            <div style="display:flex;justify-content:space-between;
                        align-items:center;margin-bottom:14px;">
                <div>
                    <span style="font-size:26px;font-weight:900;color:#f9fafb;
                                 letter-spacing:-1px;">{scored['ticker']}</span>
                    <span style="font-size:14px;color:#6b7280;margin-left:8px;">
                        ${scored.get('price', 0):.2f}
                    </span>
                </div>
                <div style="background:{bg};border:1px solid {color};
                            border-radius:6px;padding:4px 12px;">
                    <span style="color:{color};font-weight:700;font-size:12px;">
                        {emoji} {verdict}
                    </span>
                </div>
            </div>
            {score_html}
            {criteria_html}
        </div>"""


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY TABLE
# ══════════════════════════════════════════════════════════════════════════════

def render_summary_table(all_scored):
    # Sort: VIABLE first, then by score descending
    order = {"VIABLE": 0, "CAUTION": 1, "DO_NOT_TRADE": 2, "ANALYZED": 3}
    sorted_results = sorted(
        all_scored,
        key=lambda x: (order.get(x.get("verdict", "ANALYZED"), 3), -x.get("score", 0))
    )

    show_score = any(s.get("score_max", 0) > 0 for s in all_scored)

    score_header = '<th style="padding:10px 16px;text-align:left;color:#6b7280;font-size:11px;letter-spacing:2px;">SCORE</th>' if show_score else ""

    rows = ""
    for s in sorted_results:
        verdict = s.get("verdict", "ANALYZED")
        color   = verdict_color(verdict)
        emoji   = verdict_emoji(verdict)

        score_cell = ""
        if show_score:
            score_cell = f"""
                <td style="padding:10px 16px;">
                    <span style="color:{color};font-weight:700;">
                        {s.get('score',0)}/{s.get('score_max',0)}
                    </span>
                    <span style="color:#6b7280;font-size:12px;margin-left:4px;">
                        ({s.get('score_pct',0)}%)
                    </span>
                </td>"""

        rows += f"""
            <tr style="border-bottom:1px solid #1e1e1e;"
                onmouseover="this.style.background='#1a1a1a'"
                onmouseout="this.style.background='transparent'">
                <td style="padding:10px 16px;color:#f9fafb;font-weight:700;">
                    {s.get('ticker','')}
                </td>
                <td style="padding:10px 16px;color:#9ca3af;">
                    ${s.get('price', 0):.2f}
                </td>
                {score_cell}
                <td style="padding:10px 16px;">
                    <span style="color:{color};">{emoji} {verdict}</span>
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
                    {score_header}
                    <th style="padding:10px 16px;text-align:left;color:#6b7280;
                               font-size:11px;letter-spacing:2px;">VERDICT</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>"""


# ══════════════════════════════════════════════════════════════════════════════
# MAIN GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_report(all_scored, ai_text="", market_context=None):
    """
    Generate scanner_report.html from scan results.
    Overwrites the file on every call.

    Args:
        all_scored      (list) — scored ticker dicts from scanner.py
        ai_text         (str)  — AI interpretation text (markdown supported)
        market_context  (dict) — structured JSON from market_context.json
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    viable   = sum(1 for s in all_scored if s.get("verdict") == "VIABLE")
    caution  = sum(1 for s in all_scored if s.get("verdict") == "CAUTION")
    no_trade = sum(1 for s in all_scored if s.get("verdict") == "DO_NOT_TRADE")

    # Sort cards: VIABLE first
    order = {"VIABLE": 0, "CAUTION": 1, "DO_NOT_TRADE": 2, "ANALYZED": 3}
    sorted_scored = sorted(
        all_scored,
        key=lambda x: (order.get(x.get("verdict", "ANALYZED"), 3), -x.get("score", 0))
    )

    ticker_cards = "\n".join(render_ticker_card(s) for s in sorted_scored)
    ai_html      = markdown_to_html(ai_text)
    ctx_banner   = render_context_banner(market_context)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Scanner Report — {timestamp}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;700;900&display=swap" rel="stylesheet">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: #0a0a0a;
            color: #f9fafb;
            font-family: 'DM Sans', sans-serif;
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

    <!-- Header -->
    <div style="margin-bottom:32px;border-bottom:1px solid #1e1e1e;padding-bottom:24px;">
        <div style="display:flex;justify-content:space-between;
                    align-items:flex-end;flex-wrap:wrap;gap:12px;">
            <div>
                <h1 style="font-size:32px;font-weight:900;
                           letter-spacing:-1.5px;color:#f9fafb;">
                    📊 Options Scanner
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

    <!-- Market context banner -->
    {ctx_banner}

    <!-- Summary table -->
    <div style="margin-bottom:40px;">
        <div class="section-title">Summary</div>
        <div class="summary-box">
            {render_summary_table(all_scored)}
        </div>
    </div>

    <!-- AI Interpretation -->
    <div style="margin-bottom:40px;">
        <div class="section-title">🤖 AI Interpretation</div>
        <div class="ai-box">{ai_html}</div>
    </div>

    <!-- Ticker cards -->
    <div style="margin-bottom:40px;">
        <div class="section-title">Ticker Detail</div>
        <div class="grid-3">
            {ticker_cards}
        </div>
    </div>

    <div style="text-align:center;color:#374151;font-size:12px;
                padding:24px 0;border-top:1px solid #1e1e1e;
                font-family:monospace;">
        Generated {timestamp} · Options Trading System · Paper Trading
    </div>

</div>
</body>
</html>"""

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report saved → {REPORT_PATH}")
    return REPORT_PATH