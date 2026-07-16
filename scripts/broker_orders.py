"""
broker_orders.py
================
Ciclo de vida de una orden real en Tastytrade. Lo usa LiveExecutor.

Vive aparte de executor.py por una razón: executor.py es la interfaz que auto_run
conoce y no debe saber nada del SDK. Acá está todo lo que huele a Tastytrade.

QUÉ HACE Y QUÉ NO
    Hace : resuelve la cadena, arma las patas, valida con dry run, manda con
           idempotencia, y sigue la orden hasta un estado terminal.
    NO hace: escribir la DB. Ver LiveExecutor.
    NO hace: arreglar una pata suelta. La detecta y grita. Un arreglo automático
           equivocado es peor que ninguno.

LA CONVENCIÓN DE SIGNO — el bug que este módulo aísla
    OpenIntent.debit :  >0 = débito   ·  <0 = crédito   (el sistema)
    NewOrder.price   :  <0 = débito   ·  >0 = crédito   (el SDK)
    Están INVERTIDAS. `_order_price()` es el único puente. Pasar intent.debit
    crudo convierte un débito de $2.00 en una orden de crédito de $2.00: en vez
    de comprar el spread pagando $2, lo vendés cobrando $2. No da error.

EL ENTORNO
    EXECUTOR_ENV=sandbox   -> is_test=True,  credenciales TASTYTRADE_SANDBOX_*
    EXECUTOR_ENV=production-> is_test=False, credenciales TASTYTRADE_*
    Obligatoria. Sin default: adivinar el entorno es cómo se manda una orden real
    por accidente.

LO QUE EL SANDBOX NO PUEDE PROBAR (verificado el 16-jul)
    - Fills parciales: el fill es determinista por precio, todo o nada.
    - Rechazo por margen: reporta $1,000,000 de buying power con $14,000 de NLV.
      Sus números de margen son ficción.
    - Precios: quotes con 15 minutos de atraso.
    Valida PLOMERÍA (envío, estados, rechazos, cancelaciones), no economía.
    Regla gratis del sandbox: un límite > $3 queda Live y nunca llena — ese es
    el test del camino de orden colgada.
"""
import asyncio
import datetime
import os
import uuid
from decimal import Decimal

PROD_ACCOUNT = "5WI77328"      # la cuenta REAL

# Estados terminales de una orden. Todo lo demás es "seguí esperando".
LLENA      = {"Filled"}
MUERTA     = {"Rejected", "Cancelled", "Expired", "Removed"}
PARCIAL    = {"Partially Filled"}

POLL_SEGUNDOS = 2
POLL_INTENTOS = 30             # ~60s: un límite al mid llena o no llena


class OrderResult:
    """
    Resultado de un intento de orden. Explícito a propósito: un bool no alcanza
    para distinguir 'no llenó' de 'llenó a medias', y esa diferencia es una pata
    desnuda.
    """
    def __init__(self, estado, order_id=None, fill_price=None, detalle="",
                 raw=None):
        self.estado     = estado        # 'filled'|'rejected'|'timeout'|'partial'|'error'
        self.order_id   = order_id
        self.fill_price = fill_price
        self.detalle    = detalle
        self.raw        = raw

    @property
    def ok(self):
        return self.estado == "filled"

    def __repr__(self):
        return (f"OrderResult({self.estado}, id={self.order_id}, "
                f"fill={self.fill_price}, {self.detalle!r})")


# ══════════════════════════════════════════════════════════════════════════════
# ENTORNO Y SESIÓN
# ══════════════════════════════════════════════════════════════════════════════

def executor_env() -> str:
    """
    'sandbox' | 'production'. OBLIGATORIA cuando TRADING_MODE=live.
    Sin default: un default silencioso acá manda órdenes reales.
    """
    env = os.getenv("EXECUTOR_ENV", "").strip().lower()
    if env not in ("sandbox", "production"):
        raise RuntimeError(
            f"EXECUTOR_ENV='{env}' inválido. Debe ser 'sandbox' o 'production'. "
            "No se adivina el entorno del broker."
        )
    return env


def _credenciales(env):
    if env == "sandbox":
        return (os.getenv("TASTYTRADE_SANDBOX_CLIENT_SECRET"),
                os.getenv("TASTYTRADE_SANDBOX_REFRESH_TOKEN"),
                os.getenv("TASTYTRADE_SANDBOX_ACCOUNT"))
    return (os.getenv("TASTYTRADE_CLIENT_SECRET"),
            os.getenv("TASTYTRADE_REFRESH_TOKEN"),
            os.getenv("TASTYTRADE_ACCOUNT"))


async def _sesion_y_cuenta():
    """
    Sesión FRESCA por operación — patrón fijado en pricing.py/criteria.py: un
    singleton atado a un event loop cerrado devuelve None intermitentes.
    """
    from tastytrade import Session
    from tastytrade.account import Account

    env = executor_env()
    secret, refresh, num_esperado = _credenciales(env)
    if not secret or not refresh:
        raise RuntimeError(
            f"Faltan credenciales para EXECUTOR_ENV={env}. Sin credenciales no "
            "se manda nada."
        )

    session  = Session(secret, refresh, is_test=(env == "sandbox"))
    accounts = await Account.get(session)
    if not accounts:
        raise RuntimeError(f"La sesión {env} no devolvió ninguna cuenta.")

    account = accounts[0]
    if num_esperado and account.account_number != num_esperado:
        raise RuntimeError(
            f"La sesión devolvió {account.account_number} pero el entorno "
            f"espera {num_esperado}. Abortado antes de mandar nada."
        )

    # La red de seguridad: 5WI77328 y 5WV27378 difieren en dos caracteres.
    if env == "sandbox" and account.account_number == PROD_ACCOUNT:
        raise RuntimeError(
            f"EXECUTOR_ENV=sandbox pero la sesión devolvió {PROD_ACCOUNT}, que "
            "es la cuenta REAL. is_test no está haciendo efecto. ABORTADO."
        )

    return session, account, env


# ══════════════════════════════════════════════════════════════════════════════
# CONSTRUCCIÓN DE LA ORDEN
# ══════════════════════════════════════════════════════════════════════════════

def _order_price(debit) -> Decimal:
    """
    ÚNICO puente entre la convención del sistema y la del SDK. Ver cabecera.
        debit +2.00 (débito)  -> price -2.00
        debit -1.38 (crédito) -> price +1.38
    """
    return Decimal(str(-float(debit)))


def client_order_id(intent) -> str:
    """
    Idempotencia (§16). Va en NewOrder.external_identifier.

    Determinista por (ticker, strikes, expiración, día): si el proceso se cae
    después de mandar y antes de leer la respuesta, el reintento del mismo run
    lleva el mismo id y el broker no duplica. Cambia al día siguiente, para que
    una posición legítimamente nueva del mismo spread no quede bloqueada.
    """
    hoy = datetime.date.today().isoformat()
    base = (f"{intent.ticker}-{intent.strike_low}-{intent.strike_high}-"
            f"{intent.expiration}-{hoy}")
    # El campo tiene largo limitado: un hash corto y estable del identificador.
    corto = uuid.uuid5(uuid.NAMESPACE_OID, base).hex[:16]
    return f"hha-{corto}"


async def _resolver_patas(session, intent):
    """
    Busca los dos contratos reales en la cadena. NUNCA se construye un símbolo
    OCC a mano: los strikes varían por ticker (DAL de $1, otros de $2.5 o $5) y
    las expiraciones son días hábiles. Verdad del broker, siempre (§15).
    """
    from tastytrade.instruments import get_option_chain
    from tastytrade.order import OrderAction

    chain = await get_option_chain(session, intent.ticker)
    if not chain:
        return None, f"{intent.ticker}: la cadena vino vacía"

    exp = datetime.date.fromisoformat(str(intent.expiration))
    if exp not in chain:
        disponibles = sorted(str(e) for e in chain)[:6]
        return None, (f"{intent.ticker}: la expiración {exp} no existe en la "
                      f"cadena. Hay: {disponibles}")

    # debit > 0 = Bull Call Spread (calls) · debit < 0 = Bull Put Spread (puts)
    es_call = float(intent.debit) > 0
    tipo    = "C" if es_call else "P"
    opciones = [o for o in chain[exp] if o.option_type.value == tipo]

    def _buscar(strike):
        for o in opciones:
            if abs(float(o.strike_price) - float(strike)) < 0.001:
                return o
        return None

    bajo = _buscar(intent.strike_low)
    alto = _buscar(intent.strike_high)
    if bajo is None or alto is None:
        cerca = sorted(float(o.strike_price) for o in opciones)
        cerca = [s for s in cerca
                 if abs(s - float(intent.strike_low)) < 15][:10]
        falta = intent.strike_low if bajo is None else intent.strike_high
        return None, (f"{intent.ticker}: el strike {falta} no existe en la "
                      f"cadena ({tipo}, {exp}). Cerca hay: {cerca}")

    if es_call:
        # Bull Call Spread: compra el strike BAJO, vende el ALTO
        patas = [bajo.build_leg(Decimal(1), OrderAction.BUY_TO_OPEN),
                 alto.build_leg(Decimal(1), OrderAction.SELL_TO_OPEN)]
    else:
        # Bull Put Spread: vende el strike ALTO, compra el BAJO
        patas = [alto.build_leg(Decimal(1), OrderAction.SELL_TO_OPEN),
                 bajo.build_leg(Decimal(1), OrderAction.BUY_TO_OPEN)]

    return patas, None


def _build_order(patas, precio, ext_id):
    from tastytrade.order import NewOrder, OrderTimeInForce, OrderType
    return NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=patas,
        price=precio,
        external_identifier=ext_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# SEGUIMIENTO
# ══════════════════════════════════════════════════════════════════════════════

def _fill_price(placed):
    """
    Precio de fill según el BROKER. Devuelve None si no se pudo leer.
    Nunca se infiere del límite: el límite es lo que pediste, no lo que pagaste.
    Sin dato real -> None (§10).
    """
    for attr in ("price", "filled_price", "average_fill_price"):
        v = getattr(placed, attr, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


async def _seguir(session, account, order_id):
    """
    Poll hasta estado terminal. Devuelve (estado_str, PlacedOrder|None).
    """
    from tastytrade.order import PlacedOrder  # noqa: F401  (documenta el tipo)

    ultimo = None
    for intento in range(POLL_INTENTOS):
        try:
            ordenes = await account.get_live_orders(session)
        except Exception as e:
            print(f"    [orden {order_id}] no se pudo leer el estado: {e}")
            await asyncio.sleep(POLL_SEGUNDOS)
            continue

        actual = next((o for o in ordenes if getattr(o, "id", None) == order_id), None)

        if actual is None:
            # Salió de las vivas: o llenó o murió. get_order da el estado final.
            try:
                actual = await account.get_order(session, order_id)
            except Exception as e:
                return "error", None if ultimo is None else ultimo

        ultimo = actual
        estado = str(getattr(actual, "status", "") or "")
        if intento == 0 or estado != str(getattr(ultimo, "status", "")):
            print(f"    [orden {order_id}] {estado}")

        if estado in LLENA:
            return "filled", actual
        if estado in MUERTA:
            return "rejected", actual
        if estado in PARCIAL:
            return "partial", actual

        await asyncio.sleep(POLL_SEGUNDOS)

    return "timeout", ultimo


async def _cancelar(session, account, order_id):
    try:
        await account.delete_order(session, order_id)
        print(f"    [orden {order_id}] cancelada")
        return True
    except Exception as e:
        print(f"    [orden {order_id}] NO se pudo cancelar: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# API PÚBLICA
# ══════════════════════════════════════════════════════════════════════════════

async def _abrir_async(intent, dry_run=False):
    session, account, env = await _sesion_y_cuenta()

    patas, err = await _resolver_patas(session, intent)
    if err:
        return OrderResult("error", detalle=err)

    precio = _order_price(intent.debit)
    ext_id = client_order_id(intent)
    orden  = _build_order(patas, precio, ext_id)

    lado = "débito" if float(intent.debit) > 0 else "crédito"
    print(f"    [{env}] {intent.ticker} ${intent.strike_low}/{intent.strike_high} "
          f"{intent.expiration} · {lado} {abs(float(intent.debit)):.2f} "
          f"-> price={precio} · id={ext_id}")

    # ── 1. DRY RUN — el broker valida ANTES de que exista nada ────────────────
    try:
        prev = await account.place_order(session, orden, dry_run=True)
    except Exception as e:
        cuerpo = getattr(getattr(e, "response", None), "text", "")
        return OrderResult("rejected",
                           detalle=f"dry run rechazado: {e} {cuerpo[:200]}")

    bpe = getattr(prev, "buying_power_effect", None)
    if bpe is not None:
        impacto = getattr(bpe, "impact", None)
        efecto  = getattr(bpe, "effect", None)
        print(f"    dry run OK · impacto {impacto} {efecto}")
        if env == "sandbox":
            print(f"    (sandbox: los números de margen son ficción — no los leas)")

    if dry_run:
        return OrderResult("filled", detalle="dry run — no se envió", raw=prev)

    # ── 2. ENVÍO ─────────────────────────────────────────────────────────────
    try:
        resp = await account.place_order(session, orden, dry_run=False)
    except Exception as e:
        cuerpo = getattr(getattr(e, "response", None), "text", "")
        return OrderResult("rejected", detalle=f"envío rechazado: {e} {cuerpo[:200]}")

    placed   = getattr(resp, "order", None)
    order_id = getattr(placed, "id", None)
    if not order_id or order_id == -1:
        return OrderResult("error",
                           detalle="el broker no devolvió id de orden — estado "
                                   "desconocido, NO reintentar a ciegas",
                           raw=resp)

    print(f"    orden enviada · id={order_id}")

    # ── 3. SEGUIMIENTO ───────────────────────────────────────────────────────
    estado, final = await _seguir(session, account, order_id)

    if estado == "filled":
        fp = _fill_price(final)
        return OrderResult("filled", order_id=order_id, fill_price=fp,
                           detalle="llena", raw=final)

    if estado == "partial":
        # NO se arregla sola. Una pata suelta es una opción desnuda; un arreglo
        # automático equivocado es peor que ninguno. Se grita y se para.
        return OrderResult("partial", order_id=order_id, raw=final,
                           detalle=("FILL PARCIAL — posible PATA SUELTA. "
                                    "Revisar la cuenta A MANO ya."))

    if estado == "timeout":
        cancelada = await _cancelar(session, account, order_id)
        detalle = ("no llenó en "
                   f"{POLL_SEGUNDOS * POLL_INTENTOS}s — "
                   + ("cancelada" if cancelada
                      else "NO SE PUDO CANCELAR, sigue viva en el broker"))
        return OrderResult("timeout", order_id=order_id, detalle=detalle, raw=final)

    motivo = getattr(final, "reject_reason", None) or getattr(final, "status", "?")
    return OrderResult("rejected", order_id=order_id, raw=final,
                       detalle=f"el broker la rechazó: {motivo}")


def abrir_spread(intent, dry_run=False) -> OrderResult:
    """
    Punto de entrada sincrónico. asyncio.run() en el borde — mismo patrón que
    pricing.py y option_selector.py.
    """
    try:
        return asyncio.run(_abrir_async(intent, dry_run=dry_run))
    except Exception as e:
        return OrderResult("error", detalle=f"{type(e).__name__}: {e}")