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

CIERRE SIEMPRE PERMITIDO
    El interruptor de live (LIVE_TRADING_ENABLED + kill-flag) bloquea APERTURAS,
    NUNCA cierres: salir de una posicion es la accion segura. Por eso este script
    instancia LiveExecutor directo y NO consulta el interruptor. En `def` ya no
    existe TRADING_MODE — cerrar live es live por definicion (el nombre lo dice).

REUTILIZABLE POR EL BOT (FASE 5)
    La logica vive en close_live(ticker, reason, commit) -> (ok, mensaje). El CLI
    de abajo solo parsea args y la llama. El bot de Telegram llama la MISMA
    funcion, sin replicar las guardas (una guarda replicada es una guarda que
    diverge).

GUARDAS (aborta si alguna falla)
    - El ticker debe existir OPEN en `positions`, exactamente una vez.
    - Sin commit=True no manda nada (dry-run informativo).

USO (CLI)
    python close_live_manual.py --ticker JNJ
    python close_live_manual.py --ticker JNJ --commit
"""
import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

DEFAULT_REASON = ("Cierre manual: take profit alcanzado, verificado contra el "
                  "broker antes de ejecutar.")


def _buscar_fila_unica(ticker):
    """(row, error). row=None + error si 0 o >1 filas OPEN. No elige a ciegas."""
    import psycopg2
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT id, strategy, strike_low, strike_high, expiration, contracts,
               premium_paid, total_cost, current_spread_value, gross_pnl
        FROM positions
        WHERE UPPER(ticker) = %s AND UPPER(status) = 'OPEN'
        ORDER BY id DESC
    """, (ticker.upper(),))
    rows = cur.fetchall()
    cur.close(); conn.close()

    if not rows:
        return None, f"{ticker} no esta OPEN en `positions`."
    if len(rows) > 1:
        return None, (f"{ticker} tiene {len(rows)} filas OPEN "
                      f"(ids {[r[0] for r in rows]}). No se elige a ciegas.")
    return rows[0], None


def close_live(ticker, reason=DEFAULT_REASON, commit=False):
    """
    Cierra una posicion LIVE a mano. Reutilizable por CLI y por el bot (FASE 5).

    Devuelve (ok: bool, mensaje: str).
        commit=False -> dry-run: precia e informa, NO manda orden. ok=True si la
                        fila es valida y se pudo evaluar; el mensaje trae el detalle.
        commit=True  -> manda la ORDEN REAL de cierre y registra el fill.

    NO pasa por el interruptor de live: cerrar siempre esta permitido.
    """
    from executor import LiveExecutor
    ticker = ticker.upper()

    # ── GUARDA · la fila existe y es unica ───────────────────────────────────
    row, err = _buscar_fila_unica(ticker)
    if row is None:
        return False, f"⛔ ABORTA: {err}"

    (pos_id, strategy, sl, sh, exp, contracts,
     premium, total_cost, last_val, _) = row

    detalle = [
        f"positions.id = {pos_id}",
        f"{ticker} {strategy} ${sl}/${sh} exp {exp} x{contracts}",
        f"premium al abrir: {premium} · total_cost: {total_cost}",
        f"ultimo valor conocido del spread: {last_val}",
    ]

    # ── quote fresco, informativo ────────────────────────────────────────────
    opt_type = "put" if float(premium) < 0 else "call"
    try:
        import pricing
        q = pricing.get_spread_quote(ticker, float(sl), float(sh), exp,
                                     option_type=opt_type)
        detalle.append(f"quote ahora: {q}")
        if q:
            ancho = abs(float(sh) - float(sl))
            horq = q["ask"] - q["bid"]
            detalle.append(f"horquilla ${horq:.2f} sobre ancho ${ancho:.2f} "
                           f"({horq/ancho*100:.0f}%)")
            if horq / ancho > 0.30:
                detalle.append("⚠️  libro ANCHO — el mid no es un precio confiable.")
    except Exception as e:
        detalle.append(f"(no se pudo pricear: {e})")

    if not commit:
        detalle.append("DRY RUN — no se mando nada. Con commit=True se ejecuta la ORDEN REAL.")
        return True, "\n  ".join(detalle)

    # ── ejecucion real ───────────────────────────────────────────────────────
    ex = LiveExecutor()
    ok = ex.close_position(ticker, reason)
    if not ok:
        return False, ("\n  ".join(detalle) +
                       f"\n\n  ⚠️  {ticker} NO confirmado. Revisa Tastytrade a mano: "
                       f"puede haber quedado una pata suelta o una orden viva.")
    return True, "\n  ".join(detalle) + f"\n\n  ✅ {ticker} cerrada y registrada."


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ticker", required=True)
    p.add_argument("--reason", default=DEFAULT_REASON)
    p.add_argument("--commit", action="store_true")
    a = p.parse_args()

    if a.commit:
        print(f"\n  >>> MANDANDO ORDEN REAL DE CIERRE de {a.ticker.upper()} <<<\n")

    ok, mensaje = close_live(a.ticker, a.reason, a.commit)
    print(f"\n  {mensaje}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())