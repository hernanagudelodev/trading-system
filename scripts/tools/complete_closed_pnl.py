"""
complete_closed_pnl.py
======================
Completa el P&L de una posición marcada CLOSED_PRICE_UNKNOWN, trayendo el precio
de cierre REAL desde Tastytrade (get_history).

CUÁNDO SE USA
    Cuando cerrás una posición FUERA del sistema (a mano en la plataforma, p.ej.
    porque el cierre del sistema dio 429). run_sync la marca CLOSED pero con
    close_reason=CLOSED_PRICE_UNKNOWN y P&L en NULL — no inventa el número.
    Este script lo completa con el dato real del broker.

    Es un PARCHE puntual, no la solución general. La solución general (que
    run_sync lea get_history siempre) va en `def` — ver NOTA_CAPITAL_DINAMICO.md.

QUÉ HACE
    1. Busca en la DB la fila CLOSED_PRICE_UNKNOWN del ticker dado.
    2. Trae de get_history las transacciones de CIERRE de ese ticker (las patas
       'Sell to Close' / 'Buy to Close' más recientes).
    3. Calcula el resultado real: total_received (suma de net_value de las patas
       de cierre) y las comisiones.
    4. DRY-RUN por defecto: muestra qué escribiría. Con --commit, actualiza la fila.

SEGURIDAD
    - Read del broker, y write de UNA fila puntual en la DB. Dry-run por defecto.
    - Solo toca filas con close_reason=CLOSED_PRICE_UNKNOWN (no pisa cierres
      normales que ya tienen su P&L).
    - Verifica que haya exactamente 2 patas de cierre; si no, aborta y avisa.

USAGE
    python complete_closed_pnl.py --ticker BAX            # dry-run
    python complete_closed_pnl.py --ticker BAX --commit   # aplica
"""
import argparse
import asyncio
import os
import sys
from datetime import date, timedelta

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def _f(v):
    return float(v) if v is not None else 0.0


async def fetch_close_legs(ticker, opened_at):
    """
    Trae las transacciones de CIERRE del ticker desde get_history.
    Cierre = acciones 'Sell to Close' / 'Buy to Close'. Se filtran desde la
    fecha de apertura para no traer operaciones viejas del mismo ticker.
    """
    from tastytrade import Session, Account
    cs = os.getenv("TASTYTRADE_CLIENT_SECRET")
    rt = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not cs or not rt:
        print("  ERROR: faltan credenciales Tastytrade")
        return None
    session = Session(cs, rt)
    accounts = await Account.get(session)
    acct = accounts[0]

    start = (opened_at.date() if opened_at else date.today() - timedelta(days=60))
    txns = await acct.get_history(session, start_date=start,
                                  underlying_symbol=ticker)

    # Quedarse SOLO con las patas de cierre (Sell to Close / Buy to Close)
    close_legs = []
    for t in txns:
        action = (getattr(t, "action", "") or "")
        if "to Close" in action:
            close_legs.append(t)
    return close_legs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--commit", action="store_true",
                    help="aplica el UPDATE (sin esto, solo dry-run)")
    a = ap.parse_args()
    ticker = a.ticker.upper()

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    # ── 1. La fila CLOSED_PRICE_UNKNOWN de ese ticker ─────────────────────────
    cur.execute("""
        SELECT id, ticker, strategy, strike_low, strike_high,
               premium_paid, total_cost, contracts, opened_at, closed_at
        FROM positions
        WHERE ticker = %s AND close_reason = 'CLOSED_PRICE_UNKNOWN'
        ORDER BY id DESC
    """, (ticker,))
    rows = cur.fetchall()

    if not rows:
        print(f"  No hay fila CLOSED_PRICE_UNKNOWN para {ticker}. Nada que hacer.")
        cur.close(); conn.close(); return 0
    if len(rows) > 1:
        print(f"  ⚠️  Hay {len(rows)} filas CLOSED_PRICE_UNKNOWN para {ticker}:")
        for r in rows:
            print(f"      id={r[0]} opened={r[8]} closed={r[9]}")
        print("  Aborto: resolvé manualmente cuál completar (una a la vez).")
        cur.close(); conn.close(); return 1

    (pid, tkr, strat, sl, sh, prem_paid, total_cost, contracts,
     opened_at, closed_at) = rows[0]
    total_cost = _f(total_cost)
    prem_paid = _f(prem_paid)

    print(f"\n  Fila a completar: id={pid} {tkr} {strat} "
          f"${sl}/{sh} · total_cost ${total_cost:.2f} · cerrada {closed_at}")

    # ── 2. Traer las patas de cierre del broker ───────────────────────────────
    try:
        legs = asyncio.run(fetch_close_legs(ticker, opened_at))
    except Exception as e:
        print(f"  ERROR trayendo get_history: {type(e).__name__}: {e}")
        cur.close(); conn.close(); return 1
    if legs is None:
        cur.close(); conn.close(); return 1

    if len(legs) != 2:
        print(f"  ⚠️  Esperaba 2 patas de cierre, encontré {len(legs)}.")
        for t in legs:
            print(f"      {getattr(t,'action','?')} {getattr(t,'symbol','?')} "
                  f"net={_f(getattr(t,'net_value',None)):.2f} "
                  f"@ {getattr(t,'executed_at','?')}")
        print("  Aborto: no puedo emparejar con seguridad. Revisá a mano.")
        cur.close(); conn.close(); return 1

    # ── 3. Calcular el resultado real ─────────────────────────────────────────
    # total_received = suma de net_value de las patas de cierre (ya con signo:
    # lo que entró/salió de caja al cerrar). Para un BCS que ganó, es positivo.
    total_received = sum(_f(getattr(t, "net_value", None)) for t in legs)
    commission = sum(_f(getattr(t, "commission", None))
                     + _f(getattr(t, "regulatory_fees", None))
                     + _f(getattr(t, "clearing_fees", None))
                     + _f(getattr(t, "proprietary_index_option_fees", None))
                     + _f(getattr(t, "other_charge", None))
                     for t in legs)
    commission = abs(commission)   # guardar como magnitud positiva

    # precio de cierre por spread (por contrato, por acción): total_received /
    # contratos / 100. Para mostrar; el P&L sale de los dólares.
    n = int(contracts or 1)
    price_at_close = round(total_received / n / 100, 2) if n else 0

    # gross = lo que recibiste al cerrar menos lo que pagaste al abrir
    gross_pnl = round(total_received - total_cost, 2)
    net_pnl = round(gross_pnl - commission, 2)
    # pnl_pct sobre el costo (débito). BCS: costo = total_cost.
    pnl_pct = round(gross_pnl / total_cost * 100, 2) if total_cost else 0

    print(f"\n  ── Datos del broker (get_history) ──")
    for t in legs:
        print(f"    {getattr(t,'action','?'):<15} "
              f"net_value {_f(getattr(t,'net_value',None)):>8.2f} "
              f"@ {getattr(t,'executed_at','?')}")
    print(f"\n  ── Cálculo ──")
    print(f"    total_cost (abrir)     : ${total_cost:>8.2f}")
    print(f"    total_received (cerrar): ${total_received:>8.2f}")
    print(f"    comisiones             : ${commission:>8.2f}")
    print(f"    gross_pnl              : ${gross_pnl:>8.2f}")
    print(f"    net_pnl (con fees)     : ${net_pnl:>8.2f}")
    print(f"    pnl_pct                : {pnl_pct:>8.2f}%")
    print(f"    price_at_close         : {price_at_close:>8.2f}")

    # ── 4. Escribir (o dry-run) ───────────────────────────────────────────────
    if not a.commit:
        print(f"\n  DRY-RUN — no se escribió nada. Con --commit se aplica el UPDATE.")
        cur.close(); conn.close(); return 0

    cur.execute("""
        UPDATE positions
        SET total_received   = %s,
            price_at_close   = %s,
            gross_pnl        = %s,
            total_commission = %s,
            net_pnl          = %s,
            pnl_pct          = %s,
            close_reason     = %s
        WHERE id = %s AND close_reason = 'CLOSED_PRICE_UNKNOWN'
    """, (round(total_received, 2), price_at_close, gross_pnl,
          round(commission, 2), net_pnl, pnl_pct,
          "Cierre manual (get_history)", pid))
    conn.commit()
    print(f"\n  ✅ Fila id={pid} actualizada. gross ${gross_pnl:.2f} · "
          f"net ${net_pnl:.2f}")
    cur.close(); conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())