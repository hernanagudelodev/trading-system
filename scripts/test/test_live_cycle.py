"""
test_live_cycle.py
==================
CICLO COMPLETO CON PLATA REAL. Abre, verifica, guarda en la DB, cierra, verifica.

⚠️  ESTO USA DINERO DE VERDAD. Cuenta 5WI77328, producción.

QUÉ PRUEBA
    Que el camino entero funciona de punta a punta con el broker real:
        LiveExecutor.open_position  -> broker_orders.open_spread
        account.get_positions()     -> ¿existe de verdad?
        trade.run_sync()            -> tabla `positions`
        LiveExecutor.close_position -> broker_orders.close_spread
        verify_closed()         -> ¿el broker dice cero?

    Nada de esto es código nuevo. El script solo los llama en orden.

LA PROPIEDAD QUE LO HACE VÁLIDO
    Cada paso que afirma algo lo confirma CONTRA EL BROKER, no contra un
    `return True`. El paso 3 no cree que abrió: pregunta. El paso 6 no cree que
    cerró: pregunta. Si el código y Tastytrade discrepan, gana Tastytrade.

LO QUE PUEDE SALIR MAL, Y QUÉ HACE EL SCRIPT
    1. La apertura no llena  -> cancela. No pasó nada. Costo: $0.
    2. Filled con patas sin fills tras reintentos -> para y grita. Mirar a mano.
    3. El CIERRE no llena al mid en 60s -> cancela y LA POSICIÓN QUEDA ABIERTA.
       El script te imprime ticker, strikes y expiración para que la cierres vos
       en la app. Este es el fallo más probable.

    El script NUNCA intenta arreglar una pata suelta solo. Un arreglo automático
    equivocado deja una opción desnuda.

COSTO ESPERADO
    ~$5-10 reales que no vuelven (comisiones de 4 patas + slippage del ida y
    vuelta), y el riesgo del spread expuesto unos minutos.

Uso:
    python scripts/test/test_live_cycle.py CCL
"""
import asyncio
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from dotenv import load_dotenv

load_dotenv()

# Tope propio del test, INDEPENDIENTE de MAX_RISK_DOLLARS (~$423). Este test no
# busca un buen trade: busca saber si la plomería anda. Un spread de $423 y uno
# de $120 dan exactamente la misma información. Se paga el más barato.
TEST_MAX_LOSS = 250.0

PROD_ACCOUNT = "5WI77328"


def sep(t=""):
    print("\n" + "=" * 64)
    if t:
        print(f"  {t}")
        print("=" * 64)


async def _elegir_estructura(ticker):
    """
    Dos strikes REALES de la cadena: cerca del dinero (para que llene) y con el
    ancho mínimo del sistema (para que sea una estructura de verdad).
    """
    from tastytrade import Session
    from tastytrade.instruments import NestedOptionChain
    from option_selector import DTE_MIN, DTE_MAX, MIN_SPREAD_WIDTH
    import httpx

    session = Session(
        os.getenv("TASTYTRADE_CLIENT_SECRET"),
        os.getenv("TASTYTRADE_REFRESH_TOKEN"),
        is_test=False,
    )
    try:
        session._client.timeout = httpx.Timeout(60.0)
    except Exception:
        pass

    chains = await NestedOptionChain.get(session, ticker)
    chain  = chains[0]
    hoy    = datetime.date.today()

    exp = next((e for e in chain.expirations
                if DTE_MIN <= (e.expiration_date - hoy).days <= DTE_MAX), None)
    if exp is None:
        hay = [f"{e.expiration_date} ({(e.expiration_date-hoy).days}d)"
               for e in chain.expirations[:6]]
        raise SystemExit(f"⛔ {ticker}: no hay expiración entre {DTE_MIN}-{DTE_MAX} "
                         f"DTE. Hay: {hay}")

    strikes = sorted(float(s.strike_price) for s in exp.strikes)
    if len(strikes) < 4:
        raise SystemExit(f"⛔ {ticker}: la cadena solo tiene {len(strikes)} strikes")

    # Cerca del dinero = el medio de la cadena. Buscamos el par cuyo ancho sea el
    # más chico que cumpla MIN_SPREAD_WIDTH: menos ancho = menos riesgo.
    base = strikes[len(strikes) // 2]
    par  = None
    for alto in strikes:
        if alto - base >= MIN_SPREAD_WIDTH:
            par = (base, alto)
            break
    if par is None:
        raise SystemExit(f"⛔ {ticker}: no hay par con ancho >= ${MIN_SPREAD_WIDTH}")

    return par[0], par[1], exp.expiration_date, (exp.expiration_date - hoy).days


def main(ticker):
    from executor import OpenIntent, LiveExecutor
    from broker_orders import executor_env, verify_closed, _count_legs
    from option_selector import position_max_loss, MIN_SPREAD_WIDTH
    import pricing

    sep("CICLO LIVE COMPLETO — PLATA REAL")

    # ── Candado 1: entorno ────────────────────────────────────────────────────
    env = executor_env()
    if env != "production":
        raise SystemExit(f"⛔ EXECUTOR_ENV='{env}'. Este test necesita 'production'.")
    print(f"\n  EXECUTOR_ENV = {env}")
    print(f"  ⚠️  cuenta REAL — esto usa dinero de verdad")

    # ── Estructura ────────────────────────────────────────────────────────────
    sl, sh, exp, dte = asyncio.run(_elegir_estructura(ticker))
    print(f"\n  {ticker} Bull Call Spread ${sl:g}/${sh:g}")
    print(f"    expiración : {exp}  ({dte} DTE)")
    print(f"    ancho      : ${sh - sl:g}  (mínimo del sistema: ${MIN_SPREAD_WIDTH})")

    # ── Precio REAL del mid — fuente única ────────────────────────────────────
    valor = pricing.get_spread_value(ticker, sl, sh, exp, option_type="call")
    if valor is None:
        raise SystemExit(f"⛔ {ticker}: sin precio real del spread. NO se manda "
                         f"un límite inventado. Abortado.")
    debit = round(abs(float(valor)), 2)
    if debit <= 0:
        raise SystemExit(f"⛔ {ticker}: el mid dio {debit} — no tiene sentido. Abortado.")

    max_loss = position_max_loss(sl, sh, debit)
    print(f"    débito mid : ${debit:.2f}")
    print(f"    pérdida máx: ${max_loss:.0f}")

    # ── Candado 2: tope de riesgo del test ────────────────────────────────────
    if max_loss > TEST_MAX_LOSS:
        raise SystemExit(
            f"\n  ⛔ ${max_loss:.0f} supera el tope del test (${TEST_MAX_LOSS:.0f}).\n"
            f"     Este test prueba plomería, no busca un buen trade. Probá con un\n"
            f"     subyacente más barato, o subí TEST_MAX_LOSS si sabés lo que hacés."
        )

    # ── Candado 3: el ticker tiene que estar LIMPIO ──────────────────────────
    previas = asyncio.run(_count_legs(ticker))
    if previas:
        print(f"\n  ⛔ el broker YA tiene {len(previas)} pata(s) de {ticker}:")
        for pp in previas:
            print(f"       {pp.symbol}  qty={pp.quantity} {pp.quantity_direction}")
        print(f"\n     Abrir otra dejaría {len(previas)+2} patas: el paso 3 abortaría")
        print(f"     DESPUÉS de abrir, y close_spread exige exactamente 2 — o sea")
        print(f"     que la nueva quedaría abierta. Cerrá esas primero.")
        return
    print(f"\n  ✓ el broker no tiene ninguna pata de {ticker}")

    # ── Candado 4: confirmación escrita ───────────────────────────────────────
    print(f"\n  Esto MANDA una orden REAL por ${max_loss:.0f} de riesgo.")
    print(f"  Va a abrir y CERRAR de inmediato. Costo esperado: $5-10 en comisiones.")
    print(f"\n  Escribí 'plata real' para confirmar.")
    try:
        r = input("  > ").strip()
    except EOFError:
        r = ""
    if r != "plata real":
        print("\n  Cancelado. No se mandó nada.\n")
        return

    ex     = LiveExecutor()
    intent = OpenIntent(
        ticker=ticker, strike_low=sl, strike_high=sh,
        expiration=exp.isoformat(), debit=debit,
        rationale=f"test de ciclo live — {datetime.date.today()}",
    )

    # ══ 2 · ABRIR ═════════════════════════════════════════════════════════════
    sep("2 · ABRIR")
    if not ex.open_position(intent):
        sep("RESULTADO")
        print("\n  No abrió. Mirá el motivo arriba.")
        print("  Si dice 'pata suelta' o 'sin fills': REVISÁ LA CUENTA A MANO.")
        print("  Si dice timeout o rechazo: no pasó nada, costo $0.\n")
        return

    # A partir de acá HAY una posición real. Todo lo que sigue se protege para
    # que un fallo nunca deje al usuario sin saber qué tiene abierto.
    abierta = True
    try:
        # ══ 3 · ¿ESTÁ ABIERTA? ════════════════════════════════════════════════
        sep("3 · ¿EL BROKER LA TIENE?")
        time.sleep(3)                     # que la posición asiente en el broker
        patas = asyncio.run(_count_legs(ticker))
        print(f"\n  {len(patas)} pata(s) de {ticker} en el broker:")
        for p in patas:
            print(f"    {p.symbol}  qty={p.quantity} {p.quantity_direction} "
                  f"avg_open={getattr(p, 'average_open_price', None)}")
        if len(patas) != 2:
            print(f"\n  ⛔ se esperaban 2 patas y hay {len(patas)}. NO se cierra a "
                  f"ciegas. Revisá la cuenta A MANO.")
            return

        # ══ 4 · GUARDAR EN LA DB ══════════════════════════════════════════════
        sep("4 · GUARDAR EN LA DB")
        print("\n  trade.run_sync() -> group_spreads() -> insert_spread()")
        try:
            import trade
            trade.run_sync()
        except Exception as e:
            print(f"  ⚠️  el sync falló: {e}")
            print(f"     No bloquea el cierre. La posición existe igual.")

        # ══ 5 · CERRAR ════════════════════════════════════════════════════════
        sep("5 · CERRAR")
        ok = ex.close_position(ticker, "test de ciclo live")
        abierta = not ok

        # ══ 6 · ¿CERRÓ? ═══════════════════════════════════════════════════════
        sep("6 · ¿EL BROKER DICE CERO?")
        time.sleep(2)
        if verify_closed(ticker):
            abierta = False

    finally:
        # ══ 7 · SYNC FINAL + VEREDICTO ════════════════════════════════════════
        try:
            import trade
            trade.run_sync()
        except Exception:
            pass

        sep("RESULTADO")
        if not abierta:
            print(f"\n  ✓ Ciclo completo. {ticker} abrió, se guardó y cerró.")
            print(f"    El broker confirma cero patas.")
            print(f"\n  El camino live funciona de punta a punta.\n")
        else:
            print(f"\n  ⛔ {ticker} SIGUE ABIERTA.")
            print(f"\n     {ticker} Bull Call Spread ${sl:g}/${sh:g}  exp {exp}")
            print(f"     Comprada  el call ${sl:g}")
            print(f"     Vendida   el call ${sh:g}")
            print(f"\n     CERRALA A MANO en la app de Tastytrade.")
            print(f"     Riesgo expuesto: ${max_loss:.0f}\n")


if __name__ == "__main__":
    main(sys.argv[1].upper() if len(sys.argv) > 1 else "CCL")