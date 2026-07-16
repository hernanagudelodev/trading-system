"""
check_open.py
=============
Exposición viva: posiciones abiertas, riesgo agregado, concentración,
y detección de trades que nunca se pricearon (posibles fantasma tipo DAL).
Solo lectura.

Este script debe mostrar EXACTAMENTE los mismos números que el gate de cartera
de auto_run. Por eso:
  - usa option_selector.position_max_loss() — la misma función, no una copia
  - lee la tabla del libro activo (paper_positions | positions), no una fija
  - compara contra MAX_PORTFOLIO_RISK_PCT, el mismo tope que rechaza aperturas

Si este script y el log del run dicen cosas distintas, uno de los dos miente.

Uso (desde cualquier carpeta):
    python scripts/tools/check_open.py
"""
import os
import sys

# check_open vive en scripts/tools/ y los módulos en scripts/ — subir un nivel.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import psycopg2
from dotenv import load_dotenv

load_dotenv()

from executor import current_mode
from option_selector import CAPITAL, position_max_loss, portfolio_risk_pct

MODE  = current_mode()
TABLE = "positions" if MODE == "live" else "paper_positions"

PCT      = portfolio_risk_pct()
MAX_RISK = CAPITAL * PCT / 100.0

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

cur.execute(f"""
    SELECT id, ticker, strategy, strike_low, strike_high, expiration,
           premium_paid, contracts, gross_pnl, pnl_pct, current_spread_value,
           last_synced_at, opened_at
    FROM {TABLE}
    WHERE UPPER(status) = 'OPEN'
    ORDER BY opened_at
""")
rows = cur.fetchall()

print(f"\n  LIBRO: {MODE}  ·  tabla: {TABLE}  ·  capital: ${CAPITAL:,.0f}")

if not rows:
    print(f"\n  No hay posiciones abiertas.")
    print(f"  Riesgo agregado: $0 / ${MAX_RISK:,.0f} ({PCT:.0f}% del capital)\n")
    cur.close(); conn.close(); raise SystemExit

print(f"\n  POSICIONES ABIERTAS: {len(rows)}\n")
print(f"  {'id':<4} {'ticker':<6} {'tipo':<4} {'spread':<16} {'exp':<11} "
      f"{'ctr':>3} {'max loss':>9} {'P&L':>8} {'priceado':<10}")
print(f"  {'-'*4} {'-'*6} {'-'*4} {'-'*16} {'-'*11} "
      f"{'-'*3} {'-'*9} {'-'*8} {'-'*10}")

riesgo_total = 0.0
sin_pricear  = []
por_ticker   = {}

for (pid, ticker, strat, sl, sh, exp, prem, ctr, pnl, pnlp, csv,
     synced, opened) in rows:

    # El SIGNO de premium_paid decide, no el string de strategy.
    # cmd_paper_buy inserta `debit` tal cual: <0 = crédito (BPS), >0 = débito (BCS).
    prem_f   = float(prem or 0)
    tipo     = "BPS" if prem_f < 0 else "BCS"
    max_loss = position_max_loss(sl, sh, prem_f, ctr)
    riesgo_total += max_loss

    nunca  = (synced is None) or (csv is None)
    if nunca:
        sin_pricear.append((pid, ticker))
    estado = "NUNCA ⚠️" if nunca else "ok"

    pnl_str = f"${float(pnl):+.0f}" if pnl is not None else "  s/d"
    spread  = f"${float(sl):.0f}/{float(sh):.0f}"

    print(f"  {pid:<4} {ticker:<6} {tipo:<4} {spread:<16} {str(exp):<11} "
          f"{int(ctr or 1):>3} {max_loss:>9,.0f} {pnl_str:>8} {estado:<10}")

    por_ticker[ticker] = por_ticker.get(ticker, 0) + 1

# ── Exposición agregada — los mismos números que ve el gate ───────────────────
pct_usado = riesgo_total / CAPITAL * 100
pct_tope  = riesgo_total / MAX_RISK * 100 if MAX_RISK else 0

print(f"\n  EXPOSICIÓN AGREGADA:")
print(f"    Posiciones abiertas   : {len(rows)}")
print(f"    Pérdida máx combinada : ${riesgo_total:,.0f}")
print(f"    % del capital         : {pct_usado:.1f}%")
print(f"    Tope ({PCT:.0f}%)            : ${MAX_RISK:,.0f}  "
      f"[{pct_tope:.0f}% consumido]")

margen = MAX_RISK - riesgo_total
if margen <= 0:
    print(f"    ⛔ TOPE ALCANZADO — el gate no va a abrir nada nuevo")
else:
    print(f"    Margen para abrir     : ${margen:,.0f}")

print(f"\n  CONCENTRACIÓN:")
dups = {t: n for t, n in por_ticker.items() if n > 1}
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