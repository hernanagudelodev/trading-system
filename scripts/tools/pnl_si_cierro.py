"""
pnl_si_cierro.py — READ ONLY. Estima el P&L si cerrás posiciones live abiertas.
No cierra nada.
  --ticker X : solo esa posición. Sin argumento: todas.

Prioriza el MID (DXLink). Si el libro está roto (una pata sin bid/ask → mid None),
cae al MARK (REST, la valuación del broker, la misma que muestra la plataforma).
El mid/mark es optimista; el bid/ask es el peor caso realista. NO inventa precios:
si ni mid ni mark hay, dice "sin precio" (no es error, es libro ilíquido).
"""
import os, sys, argparse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import psycopg2
from dotenv import load_dotenv
load_dotenv()
from pricing import get_spread_quote, get_spread_mark_by_symbols

ap = argparse.ArgumentParser()
ap.add_argument("--ticker", default=None, help="solo esta posición")
args = ap.parse_args()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
base = """
    SELECT id, ticker, strategy, strike_low, strike_high, expiration,
           premium_paid, total_cost, contracts,
           tastytrade_symbol, tastytrade_symbol_short
    FROM positions
    WHERE UPPER(status) = 'OPEN'
"""
if args.ticker:
    cur.execute(base + " AND ticker = %s ORDER BY opened_at", (args.ticker.upper(),))
else:
    cur.execute(base + " ORDER BY opened_at")
rows = cur.fetchall()
cur.close(); conn.close()

if not rows:
    print("No hay posiciones live abiertas (o el ticker no está abierto).")
    sys.exit()

print(f"\n  P&L SI CIERRO AHORA (live) — {len(rows)} posición(es)\n")
print(f"  {'ticker':<6} {'tipo':<4} {'valor':>6} {'peor':>6} {'horq':>5}  "
      f"{'P&L val':>9} {'P&L peor':>9}  fuente")
print(f"  {'-'*6} {'-'*4} {'-'*6} {'-'*6} {'-'*5}  {'-'*9} {'-'*9}  ------")

tot_val = tot_peor = 0.0
sin_precio = []

for (pid, ticker, strat, sl, sh, exp, prem, cost, ctr,
     sym_long, sym_short) in rows:
    prem_f = float(prem or 0)
    is_put = prem_f < 0
    opt = "put" if is_put else "call"
    tipo = "BPS" if is_put else "BCS"
    n = int(ctr or 1)
    cost_f = float(cost or 0)

    # 1. Intentar mid (DXLink)
    q = get_spread_quote(ticker, float(sl), float(sh), str(exp), opt)
    fuente = "mid"
    valor = peor_precio = None
    if q is not None:
        valor = q["mid"]
        peor_precio = q["ask"] if is_put else q["bid"]
    else:
        # 2. Respaldo: mark (REST) — existe aunque el libro esté roto
        m = get_spread_mark_by_symbols(sym_long, sym_short, is_put)
        if m is not None:
            valor = m["mark"]
            peor_precio = m.get("ask") if is_put else m.get("bid")
            fuente = "MARK*"

    if valor is None:
        print(f"  {ticker:<6} {tipo:<4} {'--':>6} {'--':>6} {'--':>5}  "
              f"{'sin precio':>9} {'':>9}  ilíquido")
        sin_precio.append(ticker)
        continue

    # Si el peor precio no vino (libro roto), usar el valor como fallback
    if peor_precio is None:
        peor_precio = valor

    ancho = float(sh) - float(sl)
    # horquilla solo si tenemos bid y ask del mid
    if q is not None:
        horq = (q["ask"] - q["bid"]) / ancho * 100 if ancho else 0
        horq_str = f"{horq:>4.0f}%"
    else:
        horq_str = "  --"

    if is_put:
        credito_ini = abs(cost_f)
        pnl_val  = round((credito_ini - valor * 100 * n), 2)
        pnl_peor = round((credito_ini - peor_precio * 100 * n), 2)
    else:
        pnl_val  = round((valor * 100 * n - cost_f), 2)
        pnl_peor = round((peor_precio * 100 * n - cost_f), 2)

    tot_val += pnl_val
    tot_peor += pnl_peor
    print(f"  {ticker:<6} {tipo:<4} {valor:>6.2f} {peor_precio:>6.2f} {horq_str}  "
          f"${pnl_val:>+8.0f} ${pnl_peor:>+8.0f}  {fuente}")

print(f"  {'-'*6} {'-'*4} {'-'*6} {'-'*6} {'-'*5}  {'-'*9} {'-'*9}")
print(f"  {'TOTAL':<28} ${tot_val:>+8.0f} ${tot_peor:>+8.0f}")
print()
print(f"  Al valor (optimista):  ${tot_val:+,.0f}")
print(f"  Peor caso realista:    ${tot_peor:+,.0f}")
print(f"  * MARK = valuación del broker (libro ilíquido, el mid no existe).")
print(f"    No es precio de ejecución garantizado — como en la plataforma.")
if sin_precio:
    print(f"\n  ⚠️ Sin precio ni mark: {', '.join(sin_precio)}")
print()