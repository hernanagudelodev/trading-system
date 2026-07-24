"""
close_live_manual.py
====================
Cierra A MANO una posicion de `positions` (dinero real) pasando por el
LiveExecutor, para que el precio de fill quede registrado en la DB.

POR QUE NO CERRARLA EN LA PLATAFORMA
    El precio de salida solo existe en el instante del fill. Si cerras en
    Tastytrade a mano, run_sync detecta despues que las patas ya no estan, no
    tiene que precio consultar, y escribe CLOSED_PRICE_UNKNOWN con P&L NULL.
    Paso con DLTR el 23-jul: hubo que reconstruirlo desde los logs de Railway.

QUE HACE
    LiveExecutor.close_position() -> broker_orders.close_spread() (orden REAL,
    con escalera de reprice) -> verify_closed() contra el broker ->
    close_position_in_db() con el fill real.

GUARDAS (aborta si alguna falla)
    - TRADING_MODE debe ser 'live'. Si falta, current_mode() devuelve 'paper' y
      cerrarias la posicion PAPER creyendo que cerras la real.
    - El executor instanciado debe ser LiveExecutor.
    - El ticker debe existir OPEN en `positions`, exactamente una vez.
    - Sin --commit no manda nada.

USO
    python close_live_manual.py --ticker JNJ
    python close_live_manual.py --ticker JNJ --commit
"""
import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ticker", required=True)
    p.add_argument("--reason", default="Cierre manual: take profit alcanzado, "
                                       "verificado contra el broker antes de ejecutar.")
    p.add_argument("--commit", action="store_true")
    a = p.parse_args()
    ticker = a.ticker.upper()

    # ── GUARDA 1 · modo ──────────────────────────────────────────────────────
    from executor import current_mode, get_executor, LiveExecutor
    mode = current_mode()
    print(f"\n  TRADING_MODE = {mode!r}")
    if mode != "live":
        print(f"  ⛔ ABORTA: TRADING_MODE debe ser 'live'. Con {mode!r} cerrarias")
        print(f"     la posicion de PAPER y la real seguiria abierta.")
        print(f"     Corre asi:   set TRADING_MODE=live   (o exportala)")
        return 1

    ex = get_executor()
    if not isinstance(ex, LiveExecutor):
        print(f"  ⛔ ABORTA: el executor es {type(ex).__name__}, no LiveExecutor.")
        return 1

    # ── GUARDA 2 · la fila existe y es unica ─────────────────────────────────
    import psycopg2
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT id, strategy, strike_low, strike_high, expiration, contracts,
               premium_paid, total_cost, current_spread_value, gross_pnl
        FROM positions
        WHERE UPPER(ticker) = %s AND UPPER(status) = 'OPEN'
        ORDER BY id DESC
    """, (ticker,))
    rows = cur.fetchall()
    cur.close(); conn.close()

    if not rows:
        print(f"  ⛔ ABORTA: {ticker} no esta OPEN en `positions`.")
        return 1
    if len(rows) > 1:
        print(f"  ⛔ ABORTA: {ticker} tiene {len(rows)} filas OPEN "
              f"(ids {[r[0] for r in rows]}). No se elige a ciegas.")
        return 1

    (pos_id, strategy, sl, sh, exp, contracts,
     premium, total_cost, last_val, _) = rows[0]

    print(f"  positions.id = {pos_id}")
    print(f"  {ticker} {strategy} ${sl}/${sh} exp {exp} x{contracts}")
    print(f"  premium al abrir: {premium}   total_cost: {total_cost}")
    print(f"  ultimo valor conocido del spread: {last_val}")

    # ── quote fresco, informativo ────────────────────────────────────────────
    opt_type = "put" if float(premium) < 0 else "call"
    try:
        import pricing
        q = pricing.get_spread_quote(ticker, float(sl), float(sh), exp,
                                     option_type=opt_type)
        print(f"\n  quote ahora: {q}")
        if q:
            ancho = abs(float(sh) - float(sl))
            horq = q["ask"] - q["bid"]
            print(f"  horquilla ${horq:.2f} sobre ancho ${ancho:.2f} "
                  f"({horq/ancho*100:.0f}%)")
            if horq / ancho > 0.30:
                print(f"  ⚠️  libro ANCHO — el mid no es un precio confiable.")
    except Exception as e:
        print(f"  (no se pudo pricear: {e})")

    if not a.commit:
        print(f"\n  DRY RUN — no se mando nada. Con --commit se ejecuta la ORDEN REAL.\n")
        return 0

    # ── ejecucion real ───────────────────────────────────────────────────────
    print(f"\n  >>> MANDANDO ORDEN REAL DE CIERRE de {ticker} <<<\n")
    ok = ex.close_position(ticker, a.reason)
    print(f"\n  close_position -> {ok}")
    if not ok:
        print(f"  ⚠️  NO confirmado. Revisa Tastytrade a mano antes de reintentar:")
        print(f"     puede haber quedado una pata suelta o una orden viva.")
        return 1
    print(f"  ✅ {ticker} cerrada y registrada.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())