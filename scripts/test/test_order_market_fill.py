"""
test_order_market_fill.py
=========================
MANDA UNA ORDEN MARKET DE VERDAD. En SANDBOX, cuenta 5WV27378, plata falsa.

POR QUÉ MARKET Y NO LIMIT
    El 16-jul mandamos un límite de $0.50. La doc del sandbox dice que un límite
    < $3 llena al instante. No llenó: quedó Live hasta el timeout. Esa regla no
    rige para spreads de dos patas.
    Queda una sola vía garantizada, de su misma doc:
        "Market orders will always fill at a price of $1."
    El precio de $1 es un disparate. No importa: venimos por la FORMA de
    FillInfo, no por el número.

POR QUÉ EL MARKET NO VIVE EN broker_orders.py
    Una orden market sobre un spread de opciones se llena contra el bid/ask
    entero — en producción eso es slippage garantizado. El sistema manda límites
    al mid y así debe quedar. Una capacidad que existe termina usándose, así que
    el market vive acá, en un test, y no en el camino de producción.

QUÉ PRUEBA DEL CÓDIGO REAL
    Usa _sesion_y_cuenta(), _resolver_patas(), _seguir() y _fill_price() del
    executor. Lo único propio del test es el tipo de orden.

EFECTO SECUNDARIO ÚTIL
    Si llena, queda una posición REAL en la cuenta sandbox — que es lo que hace
    falta después para probar close_position. El sandbox la borra en 24h.

Uso:
    python scripts/test/test_order_market_fill.py            # HD
    python scripts/test/test_order_market_fill.py AAPL
"""
import asyncio
import datetime
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from dotenv import load_dotenv

load_dotenv()


def _volcar(obj, titulo, sangria="    "):
    RUIDO = ("model_computed_fields", "model_config", "model_extra",
             "model_fields", "model_fields_set")
    print(f"\n{sangria}{titulo}")
    print(f"{sangria}{'-' * len(titulo)}")
    if obj is None:
        print(f"{sangria}  (None)")
        return
    for a in sorted(dir(obj)):
        if a.startswith("_") or a in RUIDO:
            continue
        try:
            v = getattr(obj, a)
        except Exception:
            continue
        if callable(v):
            continue
        print(f"{sangria}  {a:<26} = {v!r}")


async def _run(ticker):
    from tastytrade.instruments import NestedOptionChain
    from tastytrade.order import NewOrder, OrderTimeInForce, OrderType
    from option_selector import DTE_MIN, DTE_MAX
    from broker_orders import (_sesion_y_cuenta, _resolver_patas, _seguir,
                               _fill_price)
    from executor import OpenIntent

    print("\n" + "=" * 62)
    print("  ORDEN MARKET EN SANDBOX — va a llenar")
    print("=" * 62)

    # _sesion_y_cuenta ya hace TODOS los candados: EXECUTOR_ENV obligatoria,
    # credenciales, cuenta esperada, y aborta si devuelve la cuenta real.
    session, account, env = await _sesion_y_cuenta()
    if env != "sandbox":
        raise SystemExit(f"⛔ EXECUTOR_ENV='{env}'. Este script solo corre en sandbox.")
    print(f"\n  EXECUTOR_ENV = {env}  ✓")
    print(f"  cuenta       = {account.account_number}  ✓")

    # ── Elegir dos strikes REALES ─────────────────────────────────────────────
    chains = await NestedOptionChain.get(session, ticker)
    chain  = chains[0]
    hoy    = datetime.date.today()

    exp = next((e for e in chain.expirations
                if DTE_MIN <= (e.expiration_date - hoy).days <= DTE_MAX), None)
    if exp is None:
        raise SystemExit(f"No hay expiración entre {DTE_MIN}-{DTE_MAX} DTE para {ticker}")

    strikes = sorted(exp.strikes, key=lambda x: float(x.strike_price))
    medio   = len(strikes) // 2
    sl = float(strikes[medio].strike_price)
    sh = float(strikes[medio + 1].strike_price)

    # debit > 0 -> Bull Call Spread. El valor solo define el TIPO de estructura:
    # la orden es market, no lleva precio.
    intent = OpenIntent(
        ticker=ticker,
        strike_low=sl,
        strike_high=sh,
        expiration=exp.expiration_date.isoformat(),
        debit=0.50,
        rationale="prueba de forma de fill — sandbox",
    )

    print(f"\n  LA ORDEN (MARKET):")
    print(f"    {ticker} Bull Call Spread ${sl:g}/${sh:g}")
    print(f"    expiración : {exp.expiration_date}  ({(exp.expiration_date - hoy).days} DTE)")
    print(f"    sin límite — el sandbox la llena a $1 (número sin sentido, da igual)")

    # ── Las patas salen del EXECUTOR, no del test ────────────────────────────
    patas, err = await _resolver_patas(session, intent)
    if err:
        raise SystemExit(f"⛔ _resolver_patas falló: {err}")
    for p in patas:
        print(f"    {p.action.value:<14} {p.symbol}")

    print(f"\n  Esto MANDA la orden y va a LLENAR. Escribí 'sandbox' para confirmar.")
    try:
        r = input("  > ").strip()
    except EOFError:
        r = ""
    if r != "sandbox":
        print("\n  Cancelado. No se mandó nada.\n")
        return

    orden = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.MARKET,
        legs=patas,
        external_identifier="prueba-market-fill",
    )

    print(f"\n  --- mandando ---")
    try:
        resp = await account.place_order(session, orden, dry_run=False)
    except Exception as e:
        cuerpo = getattr(getattr(e, "response", None), "text", "")
        print(f"  ✗ rechazada: {e}")
        if cuerpo:
            print(f"    respuesta del broker: {cuerpo[:500]}")
        return

    placed   = getattr(resp, "order", None)
    order_id = getattr(placed, "id", None)
    print(f"  orden enviada · id={order_id}")

    estado, final = await _seguir(session, account, order_id)
    print(f"\n  estado final : {estado}")
    print(f"  _fill_price(): {_fill_price(final)}   <- la función del executor")

    print("\n" + "=" * 62)
    print("  FORMA DEL FILL — esto alimenta el escritor de DB")
    print("=" * 62)

    _volcar(final, "PlacedOrder")

    for i, p in enumerate(getattr(final, "legs", None) or []):
        _volcar(p, f"Leg[{i}]", sangria="      ")
        fills = getattr(p, "fills", None) or []
        if not fills:
            print(f"        Leg[{i}].fills = {fills!r}   <- vacío: no llenó")
        for j, f in enumerate(fills):
            _volcar(f, f"Leg[{i}].fills[{j}]  <<< LO QUE BUSCAMOS", sangria="        ")

    print("\n" + "=" * 62)
    print("  POSICIONES EN LA CUENTA DESPUÉS")
    print("=" * 62)
    pos = await account.get_positions(session)
    print(f"\n    {len(pos)} posición(es)")
    for p in pos:
        print(f"      {p.symbol}  qty={p.quantity} {p.quantity_direction} "
              f"avg_open={getattr(p, 'average_open_price', None)}")
    if pos:
        print(f"\n    Sirve para probar close_position. El sandbox la borra en 24h.")


if __name__ == "__main__":
    asyncio.run(_run(sys.argv[1].upper() if len(sys.argv) > 1 else "HD"))
    print()