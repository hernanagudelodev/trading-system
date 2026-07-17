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

# Estados, de la doc de Order Flow — NO inventados. La primera versión de este
# archivo tenía "Partially Filled", que NO EXISTE en el sistema de Tastytrade.
#
#   Submission : Received, Routed, Contingent, In Flight
#   Working    : Live, Cancel Requested, Replace Requested
#   Terminal   : Filled, Cancelled, Rejected, Expired
#
# No hay estado de "fill parcial": una orden está Filled o no lo está. El parcial
# real es en CANTIDAD (3 de 5 contratos), no en patas.
LLENA  = {"Filled"}
# Removed y Partially Removed están en el enum del SDK pero la doc no los define.
# Se tratan como terminales: mejor cortar que quedarse en un loop para siempre.
MUERTA = {"Cancelled", "Rejected", "Expired", "Removed", "Partially Removed"}

# Reintentos para leer los fills de una orden ya Filled. Ver _esperar_fills.
FILLS_INTENTOS = 5
FILLS_ESPERA   = 0.4

# ── REPRECIO DEL CIERRE ──────────────────────────────────────────────────────
# Un límite al mid que no llena y se cancela NO es un stop loss. Lo que cierra
# una posición no es insistir con el mismo precio: es CEDER.
#
# Con la convención de signos del sistema, ceder es SIEMPRE `price -= paso`:
#     BCS (cerrar = vender):  price  0.43 -> 0.41   pedís menos crédito
#     BPS (cerrar = comprar): price -1.38 -> -1.40  pagás más débito
# Mismo operador para los dos lados. No hay que ramificar.
#
# El TOPE no es negociable: sin él, un bug regala la posición. 0.10 = $10 por
# contrato en el peor caso.
CIERRE_PASO       = 0.02   # cuánto se cede por intento
CIERRE_CESION_MAX = 0.10   # tope de lo que se regala, por acción
CIERRE_ESPERA     = 8      # ciclos de POLL_SEGUNDOS por nivel de precio (~16s)

# ── REPRECIO DE LA APERTURA ──────────────────────────────────────────────────
# Abrir y cerrar NO son simétricos, a propósito:
#   El cierre TIENE que cerrar. Ceder $0.10 para salir de algo que se mueve en
#   contra es barato; rendirse es caro.
#   La apertura NO tiene que abrir. Si no llena, no pasó nada: hay 500 tickers
#   más y otro run en 4 horas. Pagar de más para entrar se come el R/R que el
#   gate protege.
# Pero $0.44 clavado tampoco: un centavo suele ser la diferencia entre llenar y
# no llenar. Reacia, no rígida.
#
# El TOPE REAL no es este número: es MAX_RISK_DOLLARS. Ceder sube la pérdida
# máxima en los DOS casos (pagás más débito, o cobrás menos crédito sobre el
# mismo ancho), así que se cede sólo mientras el trade siga pasando el gate que
# ya pasó al mid. Ver _cesion_permitida.
APERTURA_PASO       = 0.01
APERTURA_CESION_MAX = 0.02
APERTURA_ESPERA     = 10   # ciclos por nivel (~20s)

POLL_SEGUNDOS   = 2
POLL_INTENTOS   = 30           # ~60s: un límite al mid llena o no llena
SESSION_TIMEOUT = 60.0         # httpx default = 5s: muy justo, ver _sesion_y_cuenta


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

    session = Session(secret, refresh, is_test=(env == "sandbox"))

    # httpx trae 5.0s por default y el sandbox va degradado: un endpoint que
    # devuelve 405 sin hacer nada tarda 2.1s. El refresh del token y la cadena
    # se pasan de 5s y revientan con ReadTimeout — el 16-jul falló así, y
    # 30 minutos antes había funcionado. Es una carrera, no un bug intermitente.
    # _client es privado del SDK: si un upgrade lo renombra, esto se salta sin
    # romper nada, y volvés al default de 5s.
    try:
        import httpx
        session._client.timeout = httpx.Timeout(SESSION_TIMEOUT)
    except Exception as e:
        print(f"    (no se pudo subir el timeout de la sesión: {e})")

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

def _cesion_permitida(intent, precio) -> bool:
    """
    ¿El trade sigue pasando el gate de riesgo a este precio?

    Ceder sube la pérdida máxima siempre:
        BCS: pagás más débito       -> max_loss = debit * 100        sube
        BPS: cobrás menos crédito   -> max_loss = (ancho - cr) * 100 sube

    option_selector aprobó la estructura al mid. Si el precio cedido la saca de
    MAX_RISK_DOLLARS, ya no es el trade que se aprobó — es otro, más caro, que
    nadie miró. No se abre.
    """
    from option_selector import MAX_RISK_DOLLARS, position_max_loss
    debit_real = -float(precio)          # volver a la convención del sistema
    ml = position_max_loss(intent.strike_low, intent.strike_high, debit_real)
    return ml <= MAX_RISK_DOLLARS


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
    Busca los dos contratos reales y arma las patas.

    NUNCA se construye un símbolo OCC a mano: los strikes varían por ticker
    (DAL de $1, otros de $2.5 o $5) y las expiraciones son días hábiles.
    Verdad del broker, siempre (§15).

    POR QUÉ NestedOptionChain Y NO get_option_chain
        get_option_chain pega contra /option-chains/{symbol}, que devuelve TODOS
        los contratos de TODAS las expiraciones. Con el sandbox degradado eso se
        pasa del timeout y revienta (16-jul, SPY y HD por igual). NestedOptionChain
        usa /nested y tarda 0.5s. Además es lo que ya usan option_selector.py y
        pricing.py contra 500 tickers dos veces por día sin colgarse: el camino
        estaba resuelto en tu código y yo traje otro de la doc del SDK.

    Dos requests chicos (Option.get por pata, uno a uno — no acepta lista)
    en vez de uno gigante.
    """
    from tastytrade.instruments import NestedOptionChain, Option
    from tastytrade.order import OrderAction

    chains = await NestedOptionChain.get(session, intent.ticker)
    if not chains:
        return None, f"{intent.ticker}: la cadena vino vacía"
    chain = chains[0]

    objetivo = datetime.date.fromisoformat(str(intent.expiration))
    exp = next((e for e in chain.expirations
                if e.expiration_date == objetivo), None)
    if exp is None:
        hay = sorted(str(e.expiration_date) for e in chain.expirations)[:6]
        return None, (f"{intent.ticker}: la expiración {objetivo} no existe en "
                      f"la cadena. Hay: {hay}")

    def _buscar(strike):
        for st in exp.strikes:
            if abs(float(st.strike_price) - float(strike)) < 0.001:
                return st
        return None

    st_bajo = _buscar(intent.strike_low)
    st_alto = _buscar(intent.strike_high)
    if st_bajo is None or st_alto is None:
        falta = intent.strike_low if st_bajo is None else intent.strike_high
        cerca = sorted(float(x.strike_price) for x in exp.strikes)
        cerca = [x for x in cerca if abs(x - float(falta)) < 15][:10]
        return None, (f"{intent.ticker}: el strike {falta} no existe en la "
                      f"cadena ({objetivo}). Cerca hay: {cerca}")

    # debit > 0 = Bull Call Spread (calls) · debit < 0 = Bull Put Spread (puts)
    es_call = float(intent.debit) > 0
    sym_bajo = st_bajo.call if es_call else st_bajo.put
    sym_alto = st_alto.call if es_call else st_alto.put
    if not sym_bajo or not sym_alto:
        tipo = "call" if es_call else "put"
        return None, (f"{intent.ticker}: falta el símbolo {tipo} para "
                      f"{intent.strike_low}/{intent.strike_high} en {objetivo}")

    # Option.get acepta UN símbolo, no una lista (verificado: una lista falla
    # con \'list\' object has no attribute \'replace\').
    try:
        opt_bajo = await Option.get(session, sym_bajo)
        opt_alto = await Option.get(session, sym_alto)
    except Exception as e:
        return None, f"{intent.ticker}: no se pudo resolver el contrato: {e}"

    if es_call:
        # Bull Call Spread: compra el strike BAJO, vende el ALTO
        patas = [opt_bajo.build_leg(Decimal(1), OrderAction.BUY_TO_OPEN),
                 opt_alto.build_leg(Decimal(1), OrderAction.SELL_TO_OPEN)]
    else:
        # Bull Put Spread: vende el strike ALTO, compra el BAJO
        patas = [opt_alto.build_leg(Decimal(1), OrderAction.SELL_TO_OPEN),
                 opt_bajo.build_leg(Decimal(1), OrderAction.BUY_TO_OPEN)]

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
    Precio neto de fill según el BROKER. None si no hay fills.

    NO se lee PlacedOrder.price: ese es el LÍMITE que mandaste, no lo que
    pagaste. Verificado el 16-jul en sandbox: una orden que quedó Live sin
    llenar traía price=Decimal('-0.5') — el límite, intacto. Leerlo habría
    reportado un fill que nunca ocurrió.

    La única verdad son los fills de cada pata (Leg.fills -> list[FillInfo]).
    El neto se arma sumando cada pata con su signo:
        multiplier=+1 -> comprada (pagás)
        multiplier=-1 -> vendida  (cobrás)
    Devuelve en la convención del SISTEMA: >0 débito, <0 crédito.
    """
    patas = getattr(placed, "legs", None) or []
    neto  = 0.0
    hubo  = False

    for pata in patas:
        fills = getattr(pata, "fills", None) or []
        if not fills:
            # Una pata sin fills = la orden no llenó completa. Sin dato -> None.
            return None
        mult = getattr(pata, "multiplier", None)
        if mult is None:
            accion = str(getattr(getattr(pata, "action", None), "value", ""))
            mult = -1 if "Sell" in accion else 1
        for f in fills:
            precio = getattr(f, "fill_price", None)
            cant   = getattr(f, "quantity", 1)
            if precio is None:
                return None
            neto += float(precio) * float(cant or 1) * int(mult)
            hubo = True

    return round(neto, 4) if hubo else None


async def _seguir(session, account, order_id, intentos=None):
    """
    Poll hasta estado terminal. Devuelve (estado_str, PlacedOrder|None).
    """
    from tastytrade.order import PlacedOrder  # noqa: F401  (documenta el tipo)

    ultimo = None
    previo = None
    for intento in range(intentos or POLL_INTENTOS):
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

        # OrderStatus es un enum: se compara el .value ('Live'), no el miembro.
        st     = getattr(actual, "status", None)
        estado = str(getattr(st, "value", st) or "")

        # El log imprimía solo el primer estado: comparaba `ultimo` contra sí
        # mismo después de reasignarlo. Por eso se veía 'Routed' y nunca 'Live'.
        if estado != previo:
            print(f"    [orden {order_id}] {estado}")
            previo = estado
        ultimo = actual

        if estado in LLENA:
            return "filled", actual
        if estado in MUERTA:
            return "rejected", actual

        await asyncio.sleep(POLL_SEGUNDOS)

    return "timeout", ultimo


async def _esperar_fills(session, account, order_id, placed):
    """
    Vuelve a pedir la orden hasta que TODAS las patas tengan fills.

    De la doc de Order Flow:
        "tastytrade marca las órdenes Filled apenas puede, aun sin haber
         terminado de procesar todos los fills. Si el estado es Filled pero
         faltan fills de una o más patas, pegaste a la API mientras el sistema
         los procesaba. Volvé a pedirlo tras una breve demora."

    Esto importa MUCHO: sin esperar, una orden perfectamente llena se ve idéntica
    a un fill parcial (patas con fills y patas sin). Detectar "pata suelta" así
    habría mandado push urgente en cada fill normal donde el polling llegó unos
    milisegundos antes. Falsa alarma de opción desnuda sobre un spread cerrado.
    """
    for i in range(FILLS_INTENTOS):
        patas = getattr(placed, "legs", None) or []
        if patas and all(getattr(p, "fills", None) for p in patas):
            return placed
        await asyncio.sleep(FILLS_ESPERA)
        try:
            placed = await account.get_order(session, order_id)
        except Exception as e:
            print(f"    [orden {order_id}] releyendo fills: {e}")
            break
    return placed


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

    # ── 2. ENVÍO + REPRECIO ──────────────────────────────────────────────────
    # Mismo operador que el cierre: ceder es SIEMPRE `price -= paso`.
    #   BCS: price -0.44 -> -0.45  (pagás más débito)
    #   BPS: price +1.38 -> +1.36  (cobrás menos crédito)
    inicial  = precio
    paso     = Decimal(str(APERTURA_PASO))
    intento  = 0
    order_id = None

    while True:
        try:
            if order_id is None:
                resp = await account.place_order(session, orden, dry_run=False)
            else:
                # replace_order: una sola orden viva. Cancelar+mandar deja una
                # ventana donde pueden existir dos aperturas del mismo spread.
                resp = await account.replace_order(session, order_id, orden)
        except Exception as e:
            cuerpo = getattr(getattr(e, "response", None), "text", "")
            return OrderResult("rejected", order_id=order_id,
                               detalle=f"envío rechazado: {e} {cuerpo[:200]}")

        nuevo_id, placed = _id_orden(resp)
        if not nuevo_id:
            return OrderResult("error", raw=resp,
                               detalle="el broker no devolvió id de orden — estado "
                                       "desconocido, NO reintentar a ciegas")
        order_id = nuevo_id
        print(f"    intento {intento} · price={precio} · id={order_id}")

        estado, final = await _seguir(session, account, order_id,
                                      intentos=APERTURA_ESPERA)

        if estado == "filled":
            final = await _esperar_fills(session, account, order_id, final)
            fp    = _fill_price(final)

            # Si tras los reintentos SIGUEN faltando fills, ya no es la carrera
            # de la doc: es real y hay que mirarlo a mano.
            pp = getattr(final, "legs", None) or []
            if pp and not all(getattr(x, "fills", None) for x in pp):
                sin = [x.symbol for x in pp if not getattr(x, "fills", None)]
                return OrderResult("partial", order_id=order_id, raw=final,
                                   detalle=(f"Filled pero {len(sin)} pata(s) sin "
                                            f"fills tras {FILLS_INTENTOS} reintentos: "
                                            f"{sin}. Revisar la cuenta A MANO."))
            cedido = abs(float(inicial - precio))
            return OrderResult("filled", order_id=order_id, fill_price=fp, raw=final,
                               detalle=("llena" if cedido == 0 else
                                        f"llena cediendo ${cedido:.2f}"))

        if estado != "timeout":
            motivo = getattr(final, "reject_reason", None) or getattr(final, "status", "?")
            return OrderResult("rejected", order_id=order_id, raw=final,
                               detalle=f"el broker la rechazó: {motivo}")

        # No llenó. ¿Se puede ceder un paso más?
        siguiente = precio - paso
        cedido    = abs(float(inicial - siguiente))

        if cedido > APERTURA_CESION_MAX + 1e-9:
            cancelada = await _cancelar(session, account, order_id)
            return OrderResult("timeout", order_id=order_id, raw=final,
                               detalle=(f"no llenó cediendo hasta "
                                        f"${APERTURA_CESION_MAX:.2f} — "
                                        + ("cancelada. No pasó nada."
                                           if cancelada else
                                           "NO SE PUDO CANCELAR, sigue viva en el broker.")))

        if not _cesion_permitida(intent, siguiente):
            # El precio cedido sacaría al trade de MAX_RISK_DOLLARS. Ya no es la
            # estructura que option_selector aprobó: es otra, más cara, que nadie
            # miró. Un límite que cede sin límite no es un límite.
            cancelada = await _cancelar(session, account, order_id)
            return OrderResult("timeout", order_id=order_id, raw=final,
                               detalle=("ceder otro paso sacaría el trade del gate "
                                        "de riesgo — no se abre. "
                                        + ("cancelada." if cancelada else
                                           "NO SE PUDO CANCELAR.")))

        precio  = siguiente
        orden   = _build_order(patas, precio, f"{ext_id}-{intento + 1}")
        intento += 1
        print(f"    no llenó — cediendo a {precio} "
              f"(cedido ${cedido:.2f} de ${APERTURA_CESION_MAX:.2f})")


def abrir_spread(intent, dry_run=False) -> OrderResult:
    """
    Punto de entrada sincrónico. asyncio.run() en el borde — mismo patrón que
    pricing.py y option_selector.py.
    """
    try:
        return asyncio.run(_abrir_async(intent, dry_run=dry_run))
    except Exception as e:
        return OrderResult("error", detalle=f"{type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# CIERRE
# ══════════════════════════════════════════════════════════════════════════════

async def _leer_posicion(ticker):
    """
    Lee del BROKER las dos patas abiertas de `ticker`. No de la DB.

    Devuelve DATOS PLANOS: símbolos OCC, strikes, direcciones. Ni sesión ni
    objetos del SDK.

    POR QUÉ PLANOS — el bug del 17-jul
        La primera versión devolvía la Session y los objetos Option. cerrar_spread
        usa tres asyncio.run() (hace falta: pricing.get_spread_value es síncrona
        y hace el suyo), así que la fase 3 recibía una sesión atada al event loop
        de la fase 1, ya cerrado. Resultado con plata real: "Event loop is closed"
        — el cierre se reportó rechazado y CCL quedó abierta.
        pricing.py lo documenta y lo evita: sesión fresca por operación. Yo lo
        reintroduje. Datos planos cruzan loops; objetos vivos no.
    """
    from tastytrade.instruments import Option

    session, account, env = await _sesion_y_cuenta()

    todas = await account.get_positions(session)
    patas = [p for p in todas
             if str(getattr(p, "underlying_symbol", "")).upper() == ticker.upper()
             and "Option" in str(getattr(getattr(p, "instrument_type", None),
                                         "value", ""))]

    if not patas:
        return None, f"{ticker}: el broker no tiene ninguna pata abierta"
    if len(patas) != 2:
        detalle = ", ".join(f"{p.symbol}({p.quantity_direction})" for p in patas)
        return None, (f"{ticker}: el broker tiene {len(patas)} patas, no 2 — "
                      f"NO se cierra a ciegas. Son: {detalle}")

    # El símbolo OCC no se parsea a mano: se le pregunta al broker qué es.
    contratos = []
    for p in patas:
        try:
            opt = await Option.get(session, p.symbol)
        except Exception as e:
            return None, f"{ticker}: no se pudo resolver {p.symbol}: {e}"
        contratos.append((p, opt))

    contratos.sort(key=lambda x: float(x[1].strike_price))
    (pos_bajo, opt_bajo), (pos_alto, opt_alto) = contratos

    tipos = {str(o.option_type.value) for _, o in contratos}
    if len(tipos) != 1:
        return None, f"{ticker}: las patas no son del mismo tipo ({tipos})"

    exps = {o.expiration_date for _, o in contratos}
    if len(exps) != 1:
        return None, f"{ticker}: las patas tienen expiraciones distintas ({exps})"

    return {
        "env":         env,
        "es_call":     tipos.pop() == "C",
        "strike_low":  float(opt_bajo.strike_price),
        "strike_high": float(opt_alto.strike_price),
        "expiration":  exps.pop(),
        "sym_bajo":    str(pos_bajo.symbol),
        "sym_alto":    str(pos_alto.symbol),
        "dir_bajo":    str(pos_bajo.quantity_direction),
        "dir_alto":    str(pos_alto.quantity_direction),
        "contratos":   int(abs(float(pos_bajo.quantity))),
    }, None


def _accion_cierre(direccion):
    """Long -> se vende para cerrar. Short -> se compra para cerrar."""
    from tastytrade.order import OrderAction
    return (OrderAction.SELL_TO_CLOSE if "Long" in direccion
            else OrderAction.BUY_TO_CLOSE)


def _id_orden(resp):
    """
    place_order devuelve un response con .order; replace_order devuelve el
    PlacedOrder directo. Se aceptan las dos formas.
    """
    for cand in (getattr(resp, "order", None), resp):
        oid = getattr(cand, "id", None)
        if oid and oid != -1:
            return oid, cand
    return None, None


async def _mandar_cierre(ticker, info, valor, reason):
    """
    Manda el cierre al mid y REPRECIA cediendo hasta que llene o hasta el tope.
    Sesión FRESCA: los objetos del SDK se resuelven acá, no vienen de otro loop.
    """
    from decimal import Decimal as D
    from tastytrade.instruments import Option

    session, account, env = await _sesion_y_cuenta()

    # Los contratos se resuelven en ESTE loop, desde los símbolos planos.
    try:
        opt_bajo = await Option.get(session, info["sym_bajo"])
        opt_alto = await Option.get(session, info["sym_alto"])
    except Exception as e:
        return OrderResult("error", detalle=f"{ticker}: no se pudo resolver los "
                                            f"contratos para cerrar: {e}")

    n = D(info["contratos"])
    patas = [
        opt_bajo.build_leg(n, _accion_cierre(info["dir_bajo"])),
        opt_alto.build_leg(n, _accion_cierre(info["dir_alto"])),
    ]

    # SIGNO DEL CIERRE — inverso al de la apertura.
    #   Cerrar un Bull Call Spread: lo VENDÉS   -> cobrás -> crédito -> price > 0
    #   Cerrar un Bull Put Spread : lo RECOMPRÁS -> pagás -> débito  -> price < 0
    # `valor` viene siempre positivo de pricing.get_spread_value.
    precio  = D(str(valor)) if info["es_call"] else D(str(-valor))
    inicial = precio
    paso    = D(str(CIERRE_PASO))
    tope    = D(str(CIERRE_CESION_MAX))

    lado = "crédito" if precio > 0 else "débito"
    print(f"    [{env}] CERRAR {ticker} "
          f"${info['strike_low']:g}/${info['strike_high']:g} "
          f"{info['expiration']} · {lado} {abs(float(precio)):.2f}")
    for p in patas:
        print(f"      {p.action.value:<15} {p.symbol}")

    intento  = 0
    order_id = None

    while True:
        ext_id = f"close-{ticker}-{datetime.date.today().isoformat()}-{intento}"
        orden  = _build_order(patas, precio, ext_id)

        try:
            if order_id is None:
                resp = await account.place_order(session, orden, dry_run=False)
            else:
                # replace_order: una sola orden viva. Cancelar+mandar deja una
                # ventana donde pueden existir dos cierres sobre las mismas patas.
                resp = await account.replace_order(session, order_id, orden)
        except Exception as e:
            cuerpo = getattr(getattr(e, "response", None), "text", "")
            return OrderResult("rejected", order_id=order_id,
                               detalle=f"cierre rechazado: {e} {cuerpo[:200]}")

        nuevo_id, placed = _id_orden(resp)
        if not nuevo_id:
            return OrderResult("error", raw=resp,
                               detalle="el broker no devolvió id de cierre — "
                                       "estado desconocido, la posición puede "
                                       "seguir abierta")
        order_id = nuevo_id
        print(f"    intento {intento} · price={precio} · id={order_id}")

        estado, final = await _seguir(session, account, order_id,
                                      intentos=CIERRE_ESPERA)

        if estado == "filled":
            final = await _esperar_fills(session, account, order_id, final)
            pp = getattr(final, "legs", None) or []
            if pp and not all(getattr(x, "fills", None) for x in pp):
                sin = [x.symbol for x in pp if not getattr(x, "fills", None)]
                return OrderResult("partial", order_id=order_id, raw=final,
                                   detalle=(f"cierre Filled pero {len(sin)} pata(s) "
                                            f"sin fills: {sin}. Revisar A MANO."))
            cedido = abs(float(inicial - precio))
            return OrderResult("filled", order_id=order_id,
                               fill_price=_fill_price(final), raw=final,
                               detalle=(f"cerrada ({reason})" if cedido == 0 else
                                        f"cerrada ({reason}) cediendo ${cedido:.2f}"))

        if estado != "timeout":
            motivo = getattr(final, "reject_reason", None) or getattr(final, "status", "?")
            return OrderResult("rejected", order_id=order_id, raw=final,
                               detalle=f"el broker rechazó el cierre: {motivo}")

        # No llenó a este precio. ¿Queda margen para ceder?
        cedido = abs(float(inicial - precio))
        if cedido + CIERRE_PASO > CIERRE_CESION_MAX + 1e-9:
            cancelada = await _cancelar(session, account, order_id)
            return OrderResult("timeout", order_id=order_id, raw=final,
                               detalle=(f"no llenó cediendo hasta "
                                        f"${CIERRE_CESION_MAX:.2f} — "
                                        + ("cancelado. LA POSICIÓN SIGUE ABIERTA."
                                           if cancelada else
                                           "NO SE PUDO CANCELAR y sigue viva.")))

        precio  = precio - paso      # ceder: mismo operador para BCS y BPS
        intento += 1
        print(f"    no llenó — cediendo a {precio} "
              f"(regalado ${abs(float(inicial - precio)):.2f} de ${CIERRE_CESION_MAX:.2f})")


def cerrar_spread(ticker, reason="") -> OrderResult:
    """
    Cierra las dos patas de `ticker` con un límite al mid.

    TRES FASES, cada una con su propio event loop. No es capricho:
    pricing.get_spread_value es SINCRÓNICA y hace su propio asyncio.run().
    Llamarla desde dentro de un async revienta con "asyncio.run() cannot be
    called from a running event loop".

        1. async — leer del broker qué patas hay
        2. sync  — pricear el spread (fuente única: pricing.py)
        3. async — mandar el cierre y seguirlo
    """
    import pricing

    # ── 1 ─────────────────────────────────────────────────────────────────────
    try:
        info, err = asyncio.run(_leer_posicion(ticker))
    except Exception as e:
        return OrderResult("error", detalle=f"leyendo la posición: {type(e).__name__}: {e}")
    if err:
        return OrderResult("error", detalle=err)

    # ── 2 ─────────────────────────────────────────────────────────────────────
    valor = pricing.get_spread_value(
        ticker, info["strike_low"], info["strike_high"], info["expiration"],
        option_type="call" if info["es_call"] else "put",
    )
    if valor is None:
        # Sin precio real no se manda un límite inventado (§10). Devolver un
        # número falso acá manda una orden a un precio que no existe.
        return OrderResult("error",
                           detalle=f"{ticker}: sin precio real del spread — "
                                   "NO se manda el cierre. Sigue abierta.")
    valor = abs(float(valor))

    # ── 3 ─────────────────────────────────────────────────────────────────────
    try:
        return asyncio.run(_mandar_cierre(ticker, info, valor, reason))
    except Exception as e:
        return OrderResult("error", detalle=f"mandando el cierre: {type(e).__name__}: {e}")


async def _contar_patas(ticker):
    session, account, _ = await _sesion_y_cuenta()
    todas = await account.get_positions(session)
    return [p for p in todas
            if str(getattr(p, "underlying_symbol", "")).upper() == ticker.upper()]


def verificar_cerrada(ticker) -> bool:
    """
    Le pregunta AL BROKER si quedó algo abierto de `ticker`.

    Existe porque un `return True` del cierre no es prueba de nada: si el código
    cree que cerró y no cerró, te quedaste con una posición real creyendo que
    no. La única confirmación válida la da el broker.
    """
    try:
        patas = asyncio.run(_contar_patas(ticker))
    except Exception as e:
        print(f"    no se pudo verificar contra el broker: {e}")
        return False
    if patas:
        print(f"    ⛔ el broker SIGUE mostrando {len(patas)} pata(s) de {ticker}:")
        for p in patas:
            print(f"       {p.symbol}  qty={p.quantity} {p.quantity_direction}")
        return False
    print(f"    ✓ el broker confirma: 0 patas de {ticker}")
    return True