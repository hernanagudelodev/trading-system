"""
report_generator.py
===================
Generates scanner_report.html from scan results.

Called by scanner.py after each scan. Overwrites the file each time.
Open scanner_report.html in any browser to view results with full emoji support.

Usage (from scanner.py):
    from report_generator import generate_report
    generate_report(all_scored, ai_text, market_context=None)
"""

import os
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
    }.get(verdict, "#6b7280")

def verdict_bg(verdict):
    return {
        "VIABLE":       "#052e16",
        "CAUTION":      "#451a03",
        "DO_NOT_TRADE": "#450a0a",
    }.get(verdict, "#1a1a1a")

def verdict_emoji(verdict):
    return {
        "VIABLE":       "✅",
        "CAUTION":      "⚠️",
        "DO_NOT_TRADE": "❌",
    }.get(verdict, "—")

def score_emoji(score):
    if score > 0:  return "✅"
    if score == 0: return "—"
    return "❌"

def score_bar(score, score_max):
    pct = score / score_max * 100 if score_max > 0 else 0
    if pct >= 68:   color = "#22c55e"
    elif pct >= 35: color = "#f59e0b"
    else:           color = "#ef4444"
    return f"""
        <div style="background:#1e1e1e;border-radius:4px;height:6px;width:100%;margin-top:4px;">
            <div style="background:{color};width:{pct:.1f}%;height:6px;border-radius:4px;transition:width 0.5s;"></div>
        </div>"""


# ══════════════════════════════════════════════════════════════════════════════
# TICKER CARD
# ══════════════════════════════════════════════════════════════════════════════

def render_ticker_card(scored):
    verdict   = scored["verdict"]
    color     = verdict_color(verdict)
    bg        = verdict_bg(verdict)
    emoji     = verdict_emoji(verdict)
    score     = scored["score"]
    score_max = scored["score_max"]
    score_pct = scored["score_pct"]

    categories = {
        "TECHNICAL":   ["trend_25d", "moving_averages", "sma50_direction", "rsi",
                        "week_52", "support_resistance", "candlestick"],
        "VOLATILITY":  ["hv", "iv_vs_hv", "iv_percentile", "beta",
                        "put_call_ratio", "open_interest"],
        "OPERATIONAL": ["earnings", "volume"],
        "FUNDAMENTAL": ["pe", "eps_growth", "debt_equity", "profit_margin"],
    }

    criteria_html = ""
    for cat, keys in categories.items():
        rows = ""
        for key in keys:
            if key not in scored["criteria_scores"]:
                continue
            c    = scored["criteria_scores"][key]
            icon = score_emoji(c["score"])
            pts  = f"{c['score']:+d}"
            pts_color = "#22c55e" if c["score"] > 0 else "#ef4444" if c["score"] < 0 else "#6b7280"
            rows += f"""
                <tr style="border-bottom:1px solid #2a2a2a;">
                    <td style="padding:5px 8px;color:#9ca3af;font-size:12px;">{icon}</td>
                    <td style="padding:5px 8px;color:#d1d5db;font-size:12px;font-family:monospace;">{key}</td>
                    <td style="padding:5px 8px;color:#9ca3af;font-size:12px;">{c['label']}</td>
                    <td style="padding:5px 8px;color:{pts_color};font-size:12px;font-weight:bold;text-align:right;">{pts}</td>
                </tr>"""
        criteria_html += f"""
            <div style="margin-bottom:12px;">
                <div style="font-size:10px;font-weight:700;letter-spacing:2px;color:#6b7280;margin-bottom:6px;">{cat}</div>
                <table style="width:100%;border-collapse:collapse;">{rows}</table>
            </div>"""

    return f"""
        <div style="background:#111;border:1px solid {color}33;border-radius:12px;padding:20px;
                    box-shadow:0 0 20px {color}11;">
            <!-- Header -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
                <div>
                    <span style="font-size:24px;font-weight:900;color:#f9fafb;letter-spacing:-1px;">{scored['ticker']}</span>
                    <span style="font-size:14px;color:#6b7280;margin-left:8px;">${scored['price']:.2f}</span>
                </div>
                <div style="text-align:right;">
                    <div style="background:{bg};border:1px solid {color};border-radius:6px;
                                padding:4px 12px;display:inline-block;">
                        <span style="color:{color};font-weight:700;font-size:13px;">{emoji} {verdict}</span>
                    </div>
                </div>
            </div>
            <!-- Score bar -->
            <div style="margin-bottom:16px;">
                <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
                    <span style="font-size:12px;color:#9ca3af;">Score</span>
                    <span style="font-size:12px;color:{color};font-weight:700;">{score}/{score_max} ({score_pct}%)</span>
                </div>
                {score_bar(score, score_max)}
            </div>
            <!-- Criteria -->
            {criteria_html}
        </div>"""


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY TABLE
# ══════════════════════════════════════════════════════════════════════════════

def render_summary_table(all_scored):
    sorted_results = sorted(all_scored, key=lambda x: x["score"], reverse=True)

    rows = ""
    for s in sorted_results:
        color = verdict_color(s["verdict"])
        emoji = verdict_emoji(s["verdict"])
        rows += f"""
            <tr style="border-bottom:1px solid #1e1e1e;transition:background 0.15s;"
                onmouseover="this.style.background='#1a1a1a'"
                onmouseout="this.style.background='transparent'">
                <td style="padding:10px 16px;color:#f9fafb;font-weight:700;">{s['ticker']}</td>
                <td style="padding:10px 16px;color:#9ca3af;">${s['price']:.2f}</td>
                <td style="padding:10px 16px;">
                    <span style="color:{color};font-weight:700;">{s['score']}/{s['score_max']}</span>
                    <span style="color:#6b7280;font-size:12px;margin-left:4px;">({s['score_pct']}%)</span>
                </td>
                <td style="padding:10px 16px;">
                    <span style="color:{color};">{emoji} {s['verdict']}</span>
                </td>
            </tr>"""

    return f"""
        <table style="width:100%;border-collapse:collapse;">
            <thead>
                <tr style="border-bottom:1px solid #2a2a2a;">
                    <th style="padding:10px 16px;text-align:left;color:#6b7280;font-size:11px;letter-spacing:2px;">TICKER</th>
                    <th style="padding:10px 16px;text-align:left;color:#6b7280;font-size:11px;letter-spacing:2px;">PRICE</th>
                    <th style="padding:10px 16px;text-align:left;color:#6b7280;font-size:11px;letter-spacing:2px;">SCORE</th>
                    <th style="padding:10px 16px;text-align:left;color:#6b7280;font-size:11px;letter-spacing:2px;">VERDICT</th>
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
        ai_text         (str)  — AI interpretation text
        market_context  (dict) — optional dict with vix/spy/verdict info
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    viable   = sum(1 for s in all_scored if s["verdict"] == "VIABLE")
    caution  = sum(1 for s in all_scored if s["verdict"] == "CAUTION")
    no_trade = sum(1 for s in all_scored if s["verdict"] == "DO_NOT_TRADE")

    # ── Ticker cards (VIABLE first, then CAUTION, then DNT) ──────────────────
    order = {"VIABLE": 0, "CAUTION": 1, "DO_NOT_TRADE": 2}
    sorted_scored = sorted(all_scored, key=lambda x: (order.get(x["verdict"], 3), -x["score"]))

    ticker_cards = "\n".join(render_ticker_card(s) for s in sorted_scored)

    # ── AI interpretation (convert markdown-ish to HTML) ─────────────────────
    ai_html = ai_text.replace("\n", "<br>") if ai_text else "No AI interpretation available."

    # ── Market context banner ─────────────────────────────────────────────────
    if market_context:
        verdict     = market_context.get("verdict", "")
        v_color     = "#22c55e" if "FAVORABLE" in verdict else "#f59e0b" if "CAUTION" in verdict else "#ef4444"
        ctx_banner  = f"""
            <div style="background:#111;border:1px solid {v_color}44;border-radius:12px;
                        padding:16px 24px;margin-bottom:24px;display:flex;
                        align-items:center;gap:16px;">
                <span style="font-size:28px;">{verdict.split()[0]}</span>
                <div>
                    <div style="color:{v_color};font-weight:700;font-size:16px;">{verdict}</div>
                    <div style="color:#9ca3af;font-size:13px;margin-top:2px;">
                        VIX {market_context.get('vix', '—')} &nbsp;|&nbsp;
                        SPY {market_context.get('spy_trend', '—')} &nbsp;|&nbsp;
                        {market_context.get('detail', '')}
                    </div>
                </div>
            </div>"""
    else:
        ctx_banner = ""

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
            grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
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
            padding: 24px;
            line-height: 1.7;
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
        <div style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:12px;">
            <div>
                <h1 style="font-size:32px;font-weight:900;letter-spacing:-1.5px;color:#f9fafb;">
                    📊 Bull Call Spread Scanner
                </h1>
                <p style="color:#6b7280;margin-top:4px;font-size:14px;">{timestamp}</p>
            </div>
            <div style="display:flex;gap:12px;flex-wrap:wrap;">
                <div class="stat-card">
                    <div style="font-size:28px;font-weight:900;color:#22c55e;">{viable}</div>
                    <div style="font-size:11px;color:#6b7280;letter-spacing:1px;margin-top:2px;">VIABLE</div>
                </div>
                <div class="stat-card">
                    <div style="font-size:28px;font-weight:900;color:#f59e0b;">{caution}</div>
                    <div style="font-size:11px;color:#6b7280;letter-spacing:1px;margin-top:2px;">CAUTION</div>
                </div>
                <div class="stat-card">
                    <div style="font-size:28px;font-weight:900;color:#ef4444;">{no_trade}</div>
                    <div style="font-size:11px;color:#6b7280;letter-spacing:1px;margin-top:2px;">DO NOT TRADE</div>
                </div>
                <div class="stat-card">
                    <div style="font-size:28px;font-weight:900;color:#9ca3af;">{len(all_scored)}</div>
                    <div style="font-size:11px;color:#6b7280;letter-spacing:1px;margin-top:2px;">TOTAL</div>
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

    <div style="text-align:center;color:#374151;font-size:12px;padding:24px 0;border-top:1px solid #1e1e1e;">
        Generated {timestamp} · Bull Call Spread System · Paper Trading
    </div>

</div>
</body>
</html>"""

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report saved → {REPORT_PATH}")
    return REPORT_PATH