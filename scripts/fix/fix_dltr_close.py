"""
fix_dltr_close.py
=================
Backfill del cierre de DLTR (positions.id = 12), perdido el 2026-07-23 por el
bug de `close_reason` contra varchar(50).

QUÉ PASÓ
    LiveExecutor.close_position() cerró DLTR contra el broker (fill real
    -1.87 = crédito 1.87, order id 486730902) y al escribir en la DB pasó
    f"CLOSED_LIVE: {reason}" con la prosa del LLM (~450 chars) a una columna
    varchar(50). La excepción quedó atrapada en un `except` que solo imprime,
    así que el cierre se reportó como éxito y el P&L nunca se escribió.
    run_sync() la marcó después como CLOSED_PRICE_UNKNOWN con P&L NULL.

DE DÓNDE SALEN LOS NÚMEROS
    fill = -1.87  (log de Railway, servicio live, 2026-07-23 ~14:05 UTC)
    Convención del sistema: >0 débito, <0 crédito. Cerrar un BCS a -1.87
    significa que se RECIBIERON 1.87 por acción.
    El resto es aritmética sobre total_cost, leído de la propia fila.

    NO se toca `closed_at` (ya quedó en 14:05:15, correcto) ni
    `price_at_close` (semántica no verificada — ver nota al final del script).

USO
    python fix_dltr_close.py            # dry-run: muestra antes/después
    python fix_dltr_close.py --commit   # escribe
"""
import os
import sys

import psycopg2
from dotenv import load_dotenv

# Busca .env hacia arriba desde este archivo: sirve corriendo desde
# scripts/fix/ o desde la raíz del repo.
load_dotenv()

POSITION_ID    = 12
FILL_PRICE     = -1.87          # convención del sistema (<0 = crédito)
CLOSE_REASON   = "CLOSED_LIVE"  # código, NO prosa — check_closed.py agrupa por acá
EXPECTED_TICKER = "DLTR"


def main():
    commit = "--commit" in sys.argv

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: falta DATABASE_URL")
        return 1

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    cur.execute("""
        SELECT ticker, contracts, total_cost, status, close_reason,
               premium_received, total_received, gross_pnl, net_pnl, pnl_pct
        FROM positions WHERE id = %s
    """, (POSITION_ID,))
    row = cur.fetchone()

    if not row:
        print(f"ERROR: no existe positions.id = {POSITION_ID}")
        cur.close(); conn.close()
        return 1

    (ticker, contracts, total_cost, status, close_reason,
     premium_received, total_received, gross_pnl, net_pnl, pnl_pct) = row

    # ── Guardas: no escribir a ciegas ────────────────────────────────────────
    if ticker.upper() != EXPECTED_TICKER:
        print(f"ERROR: id={POSITION_ID} es {ticker}, no {EXPECTED_TICKER}. Aborta.")
        cur.close(); conn.close()
        return 1

    if gross_pnl is not None:
        print(f"ERROR: id={POSITION_ID} YA tiene gross_pnl = {gross_pnl}. "
              f"Este script solo rellena filas sin P&L. Aborta.")
        cur.close(); conn.close()
        return 1

    contracts  = int(contracts or 1)
    total_cost = float(total_cost)

    recibido       = -FILL_PRICE                                    # 1.87
    new_received   = round(recibido * contracts * 100, 2)           # 187.00
    new_gross_pnl  = round(new_received - total_cost, 2)            # -232.00
    new_pnl_pct    = round(new_gross_pnl / total_cost * 100, 2)     # -55.37

    print()
    print(f"  positions.id = {POSITION_ID}  ({ticker}, {contracts} contrato/s)")
    print(f"  total_cost   = ${total_cost:,.2f}")
    print()
    print(f"  {'campo':<18} {'ANTES':>22}   {'DESPUÉS':>12}")
    print(f"  {'-'*18} {'-'*22}   {'-'*12}")
    print(f"  {'status':<18} {str(status):>22}   {'CLOSED':>12}")
    print(f"  {'close_reason':<18} {str(close_reason):>22}   {CLOSE_REASON:>12}")
    print(f"  {'premium_received':<18} {str(premium_received):>22}   {recibido:>12.2f}")
    print(f"  {'total_received':<18} {str(total_received):>22}   {new_received:>12.2f}")
    print(f"  {'gross_pnl':<18} {str(gross_pnl):>22}   {new_gross_pnl:>12.2f}")
    print(f"  {'net_pnl':<18} {str(net_pnl):>22}   {new_gross_pnl:>12.2f}")
    print(f"  {'pnl_pct':<18} {str(pnl_pct):>22}   {new_pnl_pct:>12.2f}")
    print()

    if not commit:
        print("  DRY RUN — no se escribió nada. Correr con --commit para aplicar.")
        cur.close(); conn.close()
        return 0

    cur.execute("""
        UPDATE positions SET
            premium_received = %s,
            total_received   = %s,
            gross_pnl        = %s,
            net_pnl          = %s,
            pnl_pct          = %s,
            close_reason     = %s
        WHERE id = %s AND gross_pnl IS NULL
    """, (recibido, new_received, new_gross_pnl, new_gross_pnl,
          new_pnl_pct, CLOSE_REASON, POSITION_ID))

    afectadas = cur.rowcount
    conn.commit()
    cur.close(); conn.close()

    print(f"  COMMIT — filas afectadas: {afectadas}")
    return 0 if afectadas == 1 else 1


if __name__ == "__main__":
    sys.exit(main())

# ─────────────────────────────────────────────────────────────────────────────
# NO TOCADO A PROPÓSITO
#
# price_at_close : quedó NULL. close_position_in_db() nunca escribe esta
#     columna, así que su semántica no está verificada (¿precio del subyacente?
#     ¿del spread?). price_at_open de esta misma fila vale 0.00, que es
#     justamente el "cero que parece dato" que el proyecto persigue. Resolver
#     aparte, no acá.
#
# commission_close / total_commission : quedan en 0.00 / NULL. close_position_in_db
#     escribe gross_pnl en net_pnl sin restar comisiones, así que este backfill
#     replica ese comportamiento para no crear una fila con criterio distinto
#     al del resto. Que net_pnl == gross_pnl en todo el sistema es un tema
#     aparte.
# ─────────────────────────────────────────────────────────────────────────────