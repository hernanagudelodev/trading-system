"""
check_open.py
=============
Exposición viva: posiciones abiertas, riesgo agregado, concentración,
y detección de trades que nunca se pricearon (posibles fantasma tipo DAL).
Solo lectura.

Uso:
    python check_open.py
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

CAPITAL = float(os.getenv("ACCOUNT_NLV", "14100"))

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

cur.execute("""
    SELECT id, ticker, strategy, strike_low, strike_high, expiration,
           premium_paid, gross_pnl, pnl_pct, current_spread_value,
           last_synced_at, opened_at
    FROM paper_positions
    WHERE UPPER(status) = 'OPEN'
    ORDER BY opened_at
""")
rows = cur.fetchall()

if not rows:
    print("\n  No hay posiciones abiertas.\n")
    cur.close(); conn.close(); raise SystemExit

print(f"\n  POSICIONES ABIERTAS: {len(rows)}\n")
print(f"  {'id':<4} {'ticker':<6} {'tipo':<4} {'spread':<16} {'exp':<11} "
      f"{'P&L':>8} {'priceado':<10}")
print(f"  {'-'*4} {'-'*6} {'-'*4} {'-'*16} {'-'*11} {'-'*8} {'-'*10}")

riesgo_total = 0.0
sin_pricear  = []
por_ticker   = {}

for (pid, ticker, strat, sl, sh, exp, prem, pnl, pnlp, csv,
     synced, opened) in rows:
    ancho   = abs(float(sh) - float(sl))
    es_put  = "Put" in (strat or "")
    tipo    = "BPS" if es_put else "BCS"
    credito = abs(float(prem)) if prem else 0.0
    # Pérdida máxima: crédito-> ancho*100-credito ; débito-> costo (premium_paid*100)
    if es_put:
        max_loss = ancho * 100 - credito * 100
    else:
        max_loss = abs(float(prem)) * 100
    riesgo_total += max_loss

    nunca = (synced is None) or (csv is None)
    if nunca:
        sin_pricear.append((pid, ticker))
    estado = "NUNCA ⚠️" if nunca else "ok"

    pnl_str = f"${float(pnl):+.0f}" if pnl is not None else "  s/d"
    spread  = f"${float(sl):.0f}/{float(sh):.0f}"
    exps    = str(exp)
    print(f"  {pid:<4} {ticker:<6} {tipo:<4} {spread:<16} {exps:<11} "
          f"{pnl_str:>8} {estado:<10}")

    por_ticker[ticker] = por_ticker.get(ticker, 0) + 1

print(f"\n  EXPOSICIÓN AGREGADA:")
print(f"    Posiciones abiertas   : {len(rows)}")
print(f"    Pérdida máx combinada : ${riesgo_total:,.0f}")
print(f"    % del capital (${CAPITAL:,.0f}) : {riesgo_total/CAPITAL*100:.1f}%")

dups = {t: n for t, n in por_ticker.items() if n > 1}
print(f"\n  CONCENTRACIÓN:")
if dups:
    for t, n in sorted(dups.items(), key=lambda x: -x[1]):
        print(f"    {t}: {n} posiciones  ⚠️ duplicado")
else:
    print(f"    Sin tickers duplicados.")

print(f"\n  SIN PRICEAR (posibles fantasma tipo DAL):")
if sin_pricear:
    for pid, t in sin_pricear:
        print(f"    id {pid} {t}  ⚠️")
else:
    print(f"    Ninguna — todas se han priceado al menos una vez.")

print()
cur.close()
conn.close()