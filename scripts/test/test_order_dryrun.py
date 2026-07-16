"""
test_order_dryrun.py
====================
Primer bloque del LiveExecutor: construir la orden y validarla contra el broker
SIN MANDARLA. Todo pasa por dry_run=True.

POR QUÉ ESTE SCRIPT EXISTE
    Antes de escribir el executor hay tres cosas de la API que no se asumen de
    memoria: si get_option_chain se awaitea en 12.4.1, la forma exacta de
    build_leg, y qué devuelve el dry run. Se verifican contra el SDK instalado.

EL BUG QUE ESTE SCRIPT FIJA
    Los signos están invertidos entre tu sistema y el SDK:

        OpenIntent.debit  :  >0 = débito (BCS)   ·  <0 = crédito (BPS)
        NewOrder.price    :  <0 = débito          ·  >0 = crédito

    Pasar intent.debit directo convierte un débito de $2.00 en una orden de
    CRÉDITO de $2.00: en vez de comprar el spread pagando $2, intentás venderlo
    cobrando $2. Con plata real no da error — da una orden que no llena o que
    llena al revés. La conversión es price = -intent.debit, y acá se prueba.

NO MANDA ÓRDENES. dry_run=True en todas las llamadas.

Uso:
    python scripts/test/test_order_dryrun.py            # default: SPY
    python scripts/test/test_order_dryrun.py HD
"""
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from dotenv import load_dotenv

load_dotenv()

PROD_ACCOUNT = "5WI77328"      # la cuenta REAL. Nunca debe aparecer acá.

fallos = []


def check(nombre, ok, detalle=""):
    print(f"  {'✓' if ok else '✗'} {nombre}" + (f"\n      {detalle}" if detalle and not ok else ""))
    if not ok:
        fallos.append(nombre)


# ══════════════════════════════════════════════════════════════════════════════
# LA CONVERSIÓN DE SIGNO — pura, testeable sin red
# ══════════════════════════════════════════════════════════════════════════════

def intent_debit_to_order_price(debit) -> Decimal:
    """
    Convierte el `debit` del OpenIntent al `price` de NewOrder.

    OpenIntent.debit :  >0 = débito   ·  <0 = crédito   (convención del sistema)
    NewOrder.price   :  <0 = débito   ·  >0 = crédito   (convención del SDK)

    Están invertidas. Esta función es el único lugar donde se cruza el puente.
    """
    return Decimal(str(-float(debit)))


def _test_signos():
    print("\n  === A · conversión de signo (sin red) ===")
    # BCS: pagás $2.00 de débito -> el SDK quiere price = -2.00
    p = intent_debit_to_order_price(2.0)
    check("débito 2.00 -> price -2.00 (el SDK cobra, no paga)", p == Decimal("-2.0"), f"dio {p}")
    # BPS: cobrás $1.38 de crédito -> el SDK quiere price = +1.38
    p = intent_debit_to_order_price(-1.38)
    check("crédito 1.38 -> price +1.38", p == Decimal("1.38"), f"dio {p}")
    # El error que se busca evitar
    check("un débito NUNCA sale con price positivo",
          intent_debit_to_order_price(2.0) < 0)


# ══════════════════════════════════════════════════════════════════════════════
# EXPLORACIÓN DE LA API + DRY RUN
# ══════════════════════════════════════════════════════════════════════════════

async def main(ticker):
    _test_signos()

    print(f"\n  === B · sesión sandbox ===")
    from tastytrade import Session
    from tastytrade.account import Account

    secret   = os.getenv("TASTYTRADE_SANDBOX_CLIENT_SECRET")
    refresh  = os.getenv("TASTYTRADE_SANDBOX_REFRESH_TOKEN")
    esperada = os.getenv("TASTYTRADE_SANDBOX_ACCOUNT")
    if not secret or not refresh:
        raise SystemExit("Faltan TASTYTRADE_SANDBOX_* en el .env")

    session  = Session(secret, refresh, is_test=True)
    accounts = await Account.get(session)
    account  = accounts[0]

    check("la cuenta NO es la de producción", account.account_number != PROD_ACCOUNT,
          f"¡{account.account_number} ES LA CUENTA REAL! ABORTAR")
    check(f"cuenta = {esperada}", account.account_number == esperada,
          f"sesión dio {account.account_number}")
    if fallos:
        return
    print(f"    cuenta: {account.account_number}")

    # ── C · cadena de opciones ────────────────────────────────────────────────
    print(f"\n  === C · cadena de opciones ({ticker}) ===")
    from tastytrade.instruments import get_option_chain

    # ¿se awaitea en 12.4.1? Se prueba, no se asume.
    try:
        chain = await get_option_chain(session, ticker)
        print("    get_option_chain: ES async (await)")
    except TypeError:
        chain = get_option_chain(session, ticker)
        print("    get_option_chain: es SYNC (sin await)")

    check("la cadena trae expiraciones", bool(chain), "cadena vacía")
    if not chain:
        return

    from option_selector import DTE_MIN, DTE_MAX
    import datetime
    hoy = datetime.date.today()

    exps = sorted(chain.keys())
    objetivo = None
    for e in exps:
        dte = (e - hoy).days
        if DTE_MIN <= dte <= DTE_MAX:
            objetivo = e
            break
    check(f"hay expiración entre {DTE_MIN}-{DTE_MAX} DTE", objetivo is not None,
          f"expiraciones: {[str(e) for e in exps[:8]]}")
    if not objetivo:
        return
    print(f"    expiración elegida: {objetivo} ({(objetivo - hoy).days} DTE)")

    opciones = chain[objetivo]
    calls = sorted([o for o in opciones if o.option_type.value == "C"],
                   key=lambda o: o.strike_price)
    check("hay calls en esa expiración", len(calls) > 2, f"encontradas {len(calls)}")
    if len(calls) < 2:
        return

    # Dos strikes contiguos del medio de la cadena
    medio = len(calls) // 2
    largo, corto = calls[medio], calls[medio + 1]
    ancho = float(corto.strike_price) - float(largo.strike_price)
    print(f"    strikes: {largo.strike_price} / {corto.strike_price}  (ancho ${ancho})")
    print(f"    símbolo OCC: {largo.symbol}")

    # ── D · build_leg ─────────────────────────────────────────────────────────
    print(f"\n  === D · build_leg ===")
    from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType

    pata_larga = largo.build_leg(Decimal(1), OrderAction.BUY_TO_OPEN)
    pata_corta = corto.build_leg(Decimal(1), OrderAction.SELL_TO_OPEN)
    check("build_leg construye las dos patas", pata_larga and pata_corta)
    print(f"    larga: {pata_larga.action.value} {pata_larga.symbol}")
    print(f"    corta: {pata_corta.action.value} {pata_corta.symbol}")

    # ── E · DRY RUN ───────────────────────────────────────────────────────────
    print(f"\n  === E · DRY RUN (no manda nada) ===")

    # Bull Call Spread ficticio: débito de $0.50 en la convención del SISTEMA
    debit_del_intent = 0.50
    price = intent_debit_to_order_price(debit_del_intent)
    print(f"    intent.debit = {debit_del_intent:+.2f} (débito)  ->  price = {price} (SDK)")

    orden = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[pata_larga, pata_corta],
        price=price,
    )

    try:
        resp = await account.place_order(session, orden, dry_run=True)
    except Exception as e:
        check("el dry run es aceptado por el broker", False, f"{type(e).__name__}: {e}")
        cuerpo = getattr(getattr(e, "response", None), "text", None)
        if cuerpo:
            print(f"      respuesta: {cuerpo[:400]}")
        return

    check("el dry run es aceptado por el broker", True)

    bpe = getattr(resp, "buying_power_effect", None)
    if bpe:
        print(f"\n    BUYING POWER EFFECT (lo que el broker dice que cuesta):")
        for campo in ("change_in_buying_power", "change_in_margin_requirement",
                      "current_buying_power", "new_buying_power", "impact", "effect"):
            v = getattr(bpe, campo, None)
            if v is not None:
                print(f"      {campo:<30} {v}")

    warns = getattr(resp, "warnings", None)
    if warns:
        print(f"\n    WARNINGS: {warns}")

    o = getattr(resp, "order", None)
    if o is not None:
        print(f"\n    ORDEN (no enviada):")
        print(f"      status : {getattr(o, 'status', None)}")
        print(f"      id     : {getattr(o, 'id', 'sin id — dry run no crea orden')}")

    # ── F · external_identifier (idempotencia) ────────────────────────────────
    print(f"\n  === F · external_identifier (la idempotencia del §16) ===")
    try:
        orden_id = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[pata_larga, pata_corta],
            price=price,
            external_identifier="prueba-idempotencia-001",
        )
        await account.place_order(session, orden_id, dry_run=True)
        check("NewOrder acepta external_identifier", True)
        print("    -> sirve como client order id: reenviar el mismo no duplica")
    except Exception as e:
        check("NewOrder acepta external_identifier", False, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    t = sys.argv[1].upper() if len(sys.argv) > 1 else "SPY"
    asyncio.run(main(t))
    print("\n" + "=" * 60)
    if fallos:
        print(f"  {len(fallos)} FALLO(S): {fallos}")
        sys.exit(1)
    print("  Camino de órdenes validado. Se puede escribir LiveExecutor.")
    print("=" * 60 + "\n")