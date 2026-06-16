import os
from dotenv import load_dotenv
import psycopg2
load_dotenv()

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()

fixes = [
    {
        "ticker": "HOOD",
        "strike_low": 85.00,
        "strike_high": 91.00,
        "gross_pnl": 336.31,
        "pnl_pct": round(336.31 / 171.00 * 100, 2),   # vs total_cost=171
        "profit_pct_of_max": round(336.31 / 429.00, 4),
        "note": "Corregido: log dice +78.4% del máximo ($429) = +336.31"
    },
    {
        "ticker": "CVS",
        "strike_low": 94.00,
        "strike_high": 101.00,
        "gross_pnl": 212.43,
        "pnl_pct": round(212.43 / 262.00 * 100, 2),
        "profit_pct_of_max": round(212.43 / 438.00, 4),
        "note": "Corregido: log dice +48.5% del máximo ($438) = +212.43"
    },
    {
        "ticker": "CVS",
        "strike_low": 97.00,
        "strike_high": 104.00,
        "gross_pnl": 0.00,
        "pnl_pct": 0.00,
        "profit_pct_of_max": 0.00,
        "note": "Corregido: log dice P&L neutro al momento del cierre"
    },
    {
        "ticker": "WDAY",
        "strike_low": 128.00,
        "strike_high": 132.00,
        "gross_pnl": -135.04,
        "pnl_pct": round(-135.04 / 160.00 * 100, 2),
        "profit_pct_of_max": round(-135.04 / 160.00, 4),
        "note": "Corregido: log dice -84.4% del máximo ($160) = -135.04"
    },
]

for fix in fixes:
    cur.execute("""
        UPDATE paper_positions SET
            gross_pnl = %s,
            pnl_pct = %s,
            profit_pct_of_max = %s,
            notes = COALESCE(notes, '') || ' | ' || %s
        WHERE ticker = %s
          AND strike_low = %s
          AND strike_high = %s
          AND UPPER(status) = 'CLOSED'
    """, (
        fix["gross_pnl"], fix["pnl_pct"], fix["profit_pct_of_max"],
        fix["note"],
        fix["ticker"], fix["strike_low"], fix["strike_high"]
    ))
    print(f"  {fix['ticker']} ${fix['strike_low']}/{fix['strike_high']} → "
          f"gross_pnl=${fix['gross_pnl']:+.2f} ({fix['pnl_pct']:+.1f}%) "
          f"[{cur.rowcount} row(s)]")

conn.commit()
cur.close()
conn.close()
print("\nOK — 4 posiciones corregidas")