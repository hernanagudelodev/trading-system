"""
check_closed.py
===============
Análisis de trades cerrados: expectativa real (no win rate), motivos de
cierre y desglose por estrategia. Excluye lotes contaminados.
Solo lectura.

Solo cuenta trades del SISTEMA ACTUAL (post-reglas). Los anteriores al
2026-06-20 son del sistema viejo sin gates y contaminan la expectativa.

Uso:
    python check_closed.py                      # libro del modo activo, desde 2026-06-20
    python check_closed.py live                 # libro LIVE (positions)
    python check_closed.py paper 2026-06-23     # paper, desde otra fecha
    python check_closed.py live all             # live, todos
"""
import os
import sys
import psycopg2
from dotenv import load_dotenv

load_dotenv()

EXCLUIR = ("PRE_RULES", "INVALID_STRIKES", "MANUAL_PRICE_FIX")

# El libro entra por ARGUMENTO, no por el modo del proceso. Este script lo corre
# el bot de Telegram, que es su propio servicio en `main` con TRADING_MODE=paper
# — si mirara current_mode() SIEMPRE vería paper, sin importar qué le pidas.
# Por eso el libro es explícito: 'live' o 'paper' como argumento.
# Sin libro -> paper, el inofensivo: nunca mostrás plata real por accidente.
args = [a.lower() for a in sys.argv[1:]]
mode  = "live" if "live" in args else "paper"
TABLE = "positions" if mode == "live" else "paper_positions"

# Lo que no sea libro es el filtro de fecha (o 'all').
resto = [a for a in args if a not in ("live", "paper")]
arg   = resto[0] if resto else "2026-06-20"
desde = None if arg == "all" else arg

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

if desde:
    cur.execute("""
        SELECT ticker, strategy, close_reason, gross_pnl, pnl_pct, closed_at
        FROM {table}
        WHERE UPPER(status) = 'CLOSED'
          AND (close_reason IS NULL OR close_reason NOT IN %s)
          AND closed_at >= %s
        ORDER BY closed_at
    """.format(table=TABLE), (EXCLUIR, desde))
    print(f"\n  [{mode.upper()} · cerrados desde {desde} — sistema post-reglas]")
else:
    cur.execute("""
        SELECT ticker, strategy, close_reason, gross_pnl, pnl_pct, closed_at
        FROM {table}
        WHERE UPPER(status) = 'CLOSED'
          AND (close_reason IS NULL OR close_reason NOT IN %s)
        ORDER BY closed_at
    """.format(table=TABLE), (EXCLUIR,))
    print(f"\n  [{mode.upper()} · TODOS los cerrados — incluye era vieja contaminada]")

rows = cur.fetchall()

if not rows:
    print("\n  No hay trades cerrados válidos todavía.\n")
    cur.close(); conn.close(); raise SystemExit

print(f"\n  TRADES CERRADOS VÁLIDOS: {len(rows)}\n")
print(f"  {'ticker':<6} {'tipo':<4} {'motivo':<16} {'P&L':>8} {'%':>7} {'fecha':<11}")
print(f"  {'-'*6} {'-'*4} {'-'*16} {'-'*8} {'-'*7} {'-'*11}")

ganadores = []
perdedores = []
por_motivo = {}
por_estrategia = {}

for ticker, strat, motivo, pnl, pnlp, closed in rows:
    pnl = float(pnl or 0)
    tipo = "BPS" if "Put" in (strat or "") else "BCS"
    mot  = motivo or "—"
    fecha = closed.strftime("%Y-%m-%d") if hasattr(closed, "strftime") else str(closed)
    print(f"  {ticker:<6} {tipo:<4} {mot:<16} ${pnl:>+6.0f} "
          f"{(float(pnlp) if pnlp is not None else 0):>+6.1f}% {fecha:<11}")

    (ganadores if pnl > 0 else perdedores).append(pnl)
    por_motivo[mot] = por_motivo.get(mot, 0) + 1
    por_estrategia.setdefault(tipo, []).append(pnl)

n      = len(rows)
n_gan  = len(ganadores)
n_perd = len(perdedores)
pnl_total = sum(ganadores) + sum(perdedores)
win_rate  = n_gan / n * 100
gan_prom  = (sum(ganadores)/n_gan) if n_gan else 0
perd_prom = (sum(perdedores)/n_perd) if n_perd else 0
# Expectativa por trade = (win% * gan_prom) + (loss% * perd_prom)
expectativa = (n_gan/n)*gan_prom + (n_perd/n)*perd_prom

print(f"\n  ── RESULTADO ──")
print(f"    Cerrados            : {n}")
print(f"    Ganadores/Perdedores: {n_gan} / {n_perd}")
print(f"    Win rate            : {win_rate:.0f}%")
print(f"    P&L total           : ${pnl_total:+.0f}")
print(f"    Ganancia promedio   : ${gan_prom:+.0f}")
print(f"    Pérdida promedio    : ${perd_prom:+.0f}")
print(f"    EXPECTATIVA / trade  : ${expectativa:+.2f}  <-- la métrica que importa")

print(f"\n  ── CÓMO CERRARON ──")
for mot, c in sorted(por_motivo.items(), key=lambda x: -x[1]):
    print(f"    {mot:<18}: {c}")

print(f"\n  ── POR ESTRATEGIA ──")
for tipo, pnls in por_estrategia.items():
    g = sum(1 for p in pnls if p > 0)
    print(f"    {tipo}: {len(pnls)} trades | {g} ganadores | P&L ${sum(pnls):+.0f}")

# Advertencias automáticas
print(f"\n  ── LECTURA ──")
if n < 20:
    print(f"    ⚠️ Solo {n} cerrados — muestra insuficiente para concluir edge.")
if n_perd == 0:
    print(f"    ⚠️ CERO perdedores — aún no viste la cara de riesgo de la estrategia.")
    print(f"       Los Bull Put Spreads se ven perfectos hasta el primer gran perdedor.")
if n_perd > 0 and perd_prom != 0 and abs(perd_prom) > gan_prom * 2:
    print(f"    ⚠️ La pérdida promedio (${perd_prom:.0f}) es >2x la ganancia (${gan_prom:.0f}):")
    print(f"       necesitás win rate alto solo para empatar. Vigilá la expectativa.")
print()

cur.close()
conn.close()