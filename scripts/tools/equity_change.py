"""
equity_change.py — cambio de patrimonio (NLV) en un periodo, separando P&L real
de movimientos de caja (depositos/retiros). Solo lectura.

Mide el cambio del Net Liquidating Value entre dos snapshots (el mas cercano al
inicio del periodo y el mas reciente), y le RESTA los depositos/retiros reales
del periodo — que consulta de las transacciones del broker (tipo Money Movement,
excluyendo los "Balance Adjustment" de centavos). Asi el numero final es P&L de
trading puro, no patrimonio movido por caja externa.

    P&L real = (NLV_fin - NLV_ini) - (depositos - retiros)

Uso:
    python equity_change.py                 # ultimo mes (30 dias)
    python equity_change.py 2026-07-01      # desde esa fecha hasta hoy
    python equity_change.py 90              # ultimos 90 dias
"""
import os
import sys
import asyncio
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
load_dotenv()
import psycopg2

# ── Resolver el periodo (start_date) ──────────────────────────────────────────
arg = sys.argv[1] if len(sys.argv) > 1 else None
if arg is None:
    start_date = datetime.now() - timedelta(days=30)
    label = "last month (30 days)"
elif arg.isdigit():
    days = int(arg)
    start_date = datetime.now() - timedelta(days=days)
    label = f"last {days} days"
else:
    start_date = datetime.strptime(arg, "%Y-%m-%d")
    label = f"since {arg}"


def _get_nlv_snapshots(start_dt):
    """Snapshot mas cercano al inicio (>= start_dt) y el mas reciente."""
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT snapshot_at, net_liquidating_value
        FROM account_snapshots
        WHERE snapshot_at >= %s
        ORDER BY snapshot_at ASC LIMIT 1
    """, (start_dt,))
    start_row = cur.fetchone()
    cur.execute("""
        SELECT snapshot_at, net_liquidating_value
        FROM account_snapshots
        ORDER BY snapshot_at DESC LIMIT 1
    """)
    end_row = cur.fetchone()
    cur.close(); conn.close()
    return start_row, end_row


async def _get_cash_movements(start_dt):
    """
    Suma neta de depositos/retiros REALES en el periodo, desde las transacciones
    del broker. Filtra transaction_type == 'Money Movement' y EXCLUYE los
    'Balance Adjustment' (ajustes regulatorios de centavos, que son costo de
    operar, no caja externa). Devuelve (neto, lista_de_movimientos).

    net_value: negativo = salio plata (retiro), positivo = entro (deposito).
    """
    from tastytrade import Session
    from tastytrade.account import Account
    session = Session(os.getenv("TASTYTRADE_CLIENT_SECRET"),
                      os.getenv("TASTYTRADE_REFRESH_TOKEN"))
    account = (await Account.get(session))[0]
    txns = await account.get_history(session, start_date=start_dt.date()
                                     if hasattr(start_dt, "date") else start_dt)

    movimientos = []
    neto = 0.0
    for t in txns:
        if getattr(t, "transaction_type", "") != "Money Movement":
            continue
        sub = getattr(t, "transaction_sub_type", "") or ""
        if sub == "Balance Adjustment":        # ruido de centavos — no es caja externa
            continue
        nv = float(getattr(t, "net_value", 0) or 0)
        neto += nv
        movimientos.append((getattr(t, "transaction_date", "?"), sub, nv,
                            getattr(t, "description", "")))
    return neto, movimientos


def main():
    start_row, end_row = _get_nlv_snapshots(start_date)
    if not start_row or not end_row:
        print("\n  No hay snapshots suficientes para el periodo.\n")
        return

    start_at, start_nlv = start_row
    end_at,   end_nlv   = end_row
    start_nlv, end_nlv  = float(start_nlv), float(end_nlv)

    nlv_change = end_nlv - start_nlv
    real_days  = (end_at - start_at).days

    # Movimientos de caja reales (deposito/retiro) del periodo
    cash_net, movimientos = asyncio.run(_get_cash_movements(start_date))

    # P&L real = cambio de NLV menos lo que entro/salio de caja.
    # Si metiste $1000 (cash_net +1000), el NLV subio por eso, no por trading:
    # se resta. Si retiraste $1000 (cash_net -1000), el NLV bajo por eso: al
    # restar un negativo, se suma de vuelta. En ambos casos aisla el P&L.
    pnl_real = nlv_change - cash_net
    pnl_pct  = (pnl_real / start_nlv * 100) if start_nlv else 0

    print(f"\n  CAMBIO DE PATRIMONIO — {label}")
    print(f"  {'-'*52}")
    print(f"  Inicio : {start_at.strftime('%Y-%m-%d %H:%M')}  NLV ${start_nlv:>10,.2f}")
    print(f"  Fin    : {end_at.strftime('%Y-%m-%d %H:%M')}  NLV ${end_nlv:>10,.2f}")
    print(f"  {'-'*52}")
    print(f"  Cambio de NLV      : ${nlv_change:>+10,.2f}")

    if movimientos:
        print(f"\n  Movimientos de caja (depositos/retiros) en el periodo:")
        for fecha, sub, nv, desc in movimientos:
            print(f"    {fecha} | {sub:<18} | ${nv:>+10,.2f}")
        print(f"    {'':<31} neto: ${cash_net:>+10,.2f}")
        print(f"\n  P&L REAL (sin caja) : ${pnl_real:>+10,.2f}  ({pnl_pct:+.1f}%)  en {real_days} dias")
    else:
        print(f"  Movimientos de caja: ninguno (sin depositos ni retiros)")
        print(f"  {'-'*52}")
        print(f"  P&L REAL           : ${pnl_real:>+10,.2f}  ({pnl_pct:+.1f}%)  en {real_days} dias")

    print()


if __name__ == "__main__":
    main()