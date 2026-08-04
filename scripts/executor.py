"""
executor.py  ·  RAMA `live` (dinero real)
=========================================
Separa la DECISIÓN (auto_run decide qué hacer) de la EJECUCIÓN (cómo se hace).

auto_run produce INTENCIONES neutras ("abrir spread X", "cerrar Y") que no saben
nada de paper ni live. Un Executor las ejecuta. La bandera TRADING_MODE elige QUÉ
executor se instancia UNA sola vez — no hay 'if paper' desperdigado por el código.

    PaperExecutor : escribe en la DB
    LiveExecutor  : manda ÓRDENES REALES al broker. En ESTA rama está
                    implementado de verdad y mueve dinero.

⚠️ ESTE ARCHIVO DIVERGE ENTRE RAMAS. NUNCA traerlo con
   `git checkout main -- scripts/executor.py` ni con `git merge main`: eso
   pisaría este LiveExecutor con el stub de `main` y el sistema pasaría a
   "operar en real" ejecutando un placeholder. Todo cambio se aplica A MANO en
   las dos ramas. Ver CONTEXTO_PROYECTO.md §23.

Uso desde auto_run:
    from executor import get_executor, OpenIntent
    ex = get_executor()                      # lee TRADING_MODE, default 'paper'
    ok = ex.open_position(OpenIntent(...))   # bool: ejecutado o no
    ok = ex.close_position(ticker, reason)   # bool
    ex.sync_after_opens()                    # una vez por run, tras las aperturas

SOBRE `reason` EN close_position
    auto_run pasa el motivo que escribe el LLM: PROSA de ~450 caracteres.
    La columna `close_reason` es varchar(50) y las métricas agrupan por ella.
    Cada executor parte eso en CÓDIGO (corto, agrupable) + RATIONALE (texto).
    La interfaz no cambia; el que traduce es el executor.
    Ver el bug del 23-jul en trade._split_close_reason.
"""
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class OpenIntent:
    """Intención de abrir, neutra respecto a paper/live."""
    ticker:       str
    strike_low:   float
    strike_high:  float
    expiration:   str          # 'YYYY-MM-DD' (ya resuelta a fecha real de cadena)
    debit:        float        # >0 débito (BCS), <0 crédito (BPS)
    rationale:    str = ""
    context_json: Optional[str] = None


class Executor:
    """Interfaz común entre auto_run y el mundo real."""
    mode = "base"

    def open_position(self, intent: OpenIntent) -> bool:
        raise NotImplementedError

    def close_position(self, ticker: str, reason: str) -> bool:
        raise NotImplementedError

    def sync_after_opens(self) -> None:
        """
        Se llama UNA vez por run, después del bucle de aperturas.

        Paper: no hace nada — cmd_paper_buy ya escribió la fila.
        Live : baja del broker lo que se acaba de abrir. Sin esto la posición
               existe en Tastytrade y NO en `positions`, y el monitor lee
               `positions`: quedaría sin stop loss hasta el próximo run.

        POR QUÉ ES UN MÉTODO Y NO UN `if mode == "live"` EN auto_run
            Porque la bandera vive en get_executor() y en ningún otro lado.
            auto_run llama esto sin preguntar de qué modo es; cada executor sabe
            si tiene algo que sincronizar. Una bandera chequeada en dos lugares
            son dos lugares donde puede discrepar.

        No-op por default, no NotImplementedError: sincronizar es OPCIONAL, y un
        executor que no lo necesita no tiene por qué declararlo.
        """
        pass


class CarteraRechazo(Exception):
    """Un gate determinista de cartera rechazó la apertura. NO es un error: es el
    sistema funcionando. Lleva el motivo para loguearlo."""
    def __init__(self, motivo):
        self.motivo = motivo
        super().__init__(motivo)


def _cartera_gates(table, ticker, strike_low, strike_high, debit):
    """
    Gates DETERMINISTAS de cartera, movidos desde auto_run.execute_recommendations
    (probados en produccion). Compartidos entre paper y live: cada executor llama
    con SU tabla ('paper_positions' o 'positions').

    STATELESS: lee la cartera FRESCA de la DB en cada llamada. Una posicion abierta
    hace segundos en el mismo run ya esta en la tabla (cmd_paper_buy / run_sync la
    escribio), asi que leer fresco captura tambien lo de este run — no hace falta un
    set en memoria. La DB es la unica verdad.

    Aplica, en orden:
      1. Lectura de cartera. Si la DB no responde -> FAIL-CLOSED (rechaza).
      2. Concentracion: el ticker ya OPEN en este libro -> rechaza (no apilar nombre).
      3. Riesgo agregado: riesgo_actual + riesgo_nuevo > tope -> rechaza.

    Levanta CarteraRechazo(motivo) si algun gate corta. Devuelve None si todo pasa.
    El CAPITAL sale del broker (get_account_nlv), misma fuente que el gate por-trade.
    """
    import os
    import psycopg2
    from option_selector import get_account_nlv, position_max_loss, portfolio_risk_pct

    CAPITAL            = get_account_nlv()
    PCT                = portfolio_risk_pct()
    MAX_PORTFOLIO_RISK = CAPITAL * PCT / 100.0

    open_tickers = set()
    current_risk = 0.0
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur  = conn.cursor()
        cur.execute(f"""
            SELECT UPPER(ticker), strike_low, strike_high, premium_paid, contracts
            FROM {table}
            WHERE UPPER(status) = 'OPEN'
        """)
        for _tkr, _sl, _sh, _prem, _n in cur.fetchall():
            open_tickers.add(_tkr)
            current_risk += position_max_loss(_sl, _sh, _prem, _n)
        cur.close(); conn.close()
    except Exception as e:
        # FAIL-CLOSED: no poder ver la exposicion es exactamente cuando NO se abre.
        raise CarteraRechazo(f"no se pudo leer la cartera ({table}): {e} — no se abre")

    ticker = ticker.upper()
    if ticker in open_tickers:
        raise CarteraRechazo(f"{ticker} ya tiene posicion abierta — no apilar mismo nombre")

    new_risk = position_max_loss(strike_low, strike_high, debit)
    if current_risk + new_risk > MAX_PORTFOLIO_RISK:
        raise CarteraRechazo(
            f"{ticker} supera el tope de cartera: "
            f"${current_risk:,.0f} + ${new_risk:,.0f} > ${MAX_PORTFOLIO_RISK:,.0f}")

    return None


# Slippage simulado del paper: 1 centavo EN CONTRA, por spread (no por pata).
# BCS (debit>0) paga 1c mas; BPS (debit<0, credito) cobra 1c menos. Los dos casos
# son debit + 0.01: sumar acerca el credito negativo a cero (cobrar menos) y aleja
# el debito positivo (pagar mas). Asumir fill perfecto al mid fue el sesgo que inflo
# el mes de paper (§25); el centavo es chico pero honesto — ejecutar cuesta algo.
PAPER_SLIPPAGE = 0.01


class PaperExecutor(Executor):
    """
    Ejecucion paper. A diferencia del executor viejo (que solo delegaba en
    cmd_paper_buy), este es AUTONOMO: lee su propia cartera, aplica sus propios
    gates deterministas, y simula el slippage — todo lo que antes vivia en
    auto_run.execute_recommendations. auto_run pasa a ser solo orquestador.

    ORDEN de open_position:
      1. Gates de cartera (concentracion + riesgo agregado), stateless, sobre
         paper_positions. Si rechazan -> False, NO registra.
      2. Slippage mid + 1c en contra sobre el debit.
      3. Registro vía cmd_paper_buy (que ya esta endurecida: fix del $0.00).

    POR QUE LOS GATES ACA Y NO EN auto_run
        Que paper abra y live no (o al reves) debe ser por un gate DETERMINISTA de
        cartera, nunca por el LLM pensando distinto. El LLM decide UNA vez (selector,
        idem para ambos); la divergencia legitima entre libros ocurre despues, en
        estos gates. Por eso viven en el executor de cada libro, con su propia tabla.
    """
    mode = "paper"
    TABLE = "paper_positions"

    def open_position(self, intent: OpenIntent) -> bool:
        # 1. Gates de cartera (stateless, fail-closed). Rechazo != error.
        try:
            _cartera_gates(self.TABLE, intent.ticker,
                           intent.strike_low, intent.strike_high, intent.debit)
        except CarteraRechazo as rej:
            print(f"  [paper] {intent.ticker} NO abierta: {rej.motivo}")
            return False

        # 2. Slippage: mid + 1c en contra (mismo signo para BCS y BPS).
        debit_fill = round(intent.debit + PAPER_SLIPPAGE, 2)

        # 3. Registro. cmd_paper_buy no devuelve valor: si no lanza, registro.
        import trade as trade_module
        try:
            trade_module.cmd_paper_buy(
                intent.ticker,
                intent.strike_low,
                intent.strike_high,
                intent.expiration,
                debit_fill,
                context_json=intent.context_json,
                rationale=intent.rationale,
            )
            print(f"  [paper] {intent.ticker} registrada · debit_mid={intent.debit} "
                  f"-> fill={debit_fill} (slippage +{PAPER_SLIPPAGE})")
            return True
        except Exception as e:
            print(f"  [paper] error abriendo {intent.ticker}: {e}")
            return False

    def close_position(self, ticker: str, reason: str) -> bool:
        """
        `reason` llega como PROSA del LLM. Se parte:
            close_reason    = "MANUAL"  -> codigo, entra en varchar(50)
            close_rationale = reason    -> texto completo, columna TEXT

        POR QUE "MANUAL" Y NO UN CODIGO NUEVO
            Es el codigo que este camino viene usando desde siempre: los 19
            cierres "MANUAL (LLM)" de las metricas de paper salieron de aca.
            Estrenar un codigo parte la serie historica en dos sin ganar nada.

        EL BUG DEL 23-jul
            Antes esto pasaba `close_reason=reason` con la prosa entera. El
            UPDATE reventaba con `value too long for type character
            varying(50)`, el except de abajo lo capturaba, y auto_run reportaba
            "cierre NO ejecutado (sin precio real)" — un mensaje FALSO: el
            precio se habia obtenido bien y el fallo era de esquema.
            DLTR y WFC quedaron abiertas por eso.
        """
        import trade as trade_module
        # cmd_paper_close ya devuelve True/False (True solo si cerro de verdad)
        try:
            return bool(trade_module.cmd_paper_close(
                ticker,
                close_reason="MANUAL",
                close_rationale=reason,
            ))
        except Exception as e:
            print(f"  [paper] error cerrando {ticker}: {e}")
            return False


def _buscar_posicion_abierta(ticker):
    """
    Busca en `positions` la posición OPEN de este ticker.
    Devuelve (id, contracts) o (None, motivo).

    No adivina: 0 filas o más de 1 -> None y el motivo. Cerrar la fila
    equivocada es peor que no cerrar ninguna.
    """
    import os
    import psycopg2
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur  = conn.cursor()
        cur.execute("""
            SELECT id, contracts FROM positions
            WHERE UPPER(ticker) = %s AND UPPER(status) = 'OPEN'
            ORDER BY id DESC
        """, (ticker.upper(),))
        rows = cur.fetchall()
        cur.close(); conn.close()
    except Exception as e:
        return None, f"no se pudo leer la DB: {e}"

    if not rows:
        return None, (f"{ticker} no está OPEN en `positions` — el broker la cerró "
                      f"pero la DB nunca la tuvo")
    if len(rows) > 1:
        return None, (f"{ticker} tiene {len(rows)} rows OPEN en `positions` "
                      f"(ids {[f[0] for f in rows]}) — no se elige a ciegas")
    return rows[0], None


def _alertar_desync(ticker, fill_price, order_id, detalle):
    """
    Push URGENTE cuando el broker cerró y la DB no se enteró.

    POR QUÉ ESTO EXISTE — 23-jul
        El cierre de DLTR llenó a 1.87 y la escritura en la DB falló. El error
        se imprimió y el flujo siguió: auto_run contó `CLOSED (1)`, no salió
        ningún push, y el precio de salida sobrevivió sólo en los logs de
        Railway (que rotan). Un fallo silencioso que parecía éxito, en el camino
        del dinero. Ver §4 y §16.2.

    POR QUÉ EL FILL VA EN EL MENSAJE
        Ese número existe UN instante: la posición ya no está en el broker, así
        que run_sync no lo puede consultar y marca CLOSED_PRICE_UNKNOWN. Si la
        escritura falló, el push es el único lugar donde el dato queda a salvo.
        Con él se reconstruye el P&L a mano; sin él, se perdió.
    """
    cuerpo = (
        f"El broker CERRÓ {ticker} pero la DB no se pudo actualizar.\n\n"
        f"fill = {fill_price}  (convención sistema: <0 = crédito)\n"
        f"order id = {order_id}\n"
        f"detalle: {detalle}\n\n"
        f"ANOTAR EL FILL AHORA: sólo vive acá y en los logs de Railway.\n"
        f"La posición puede quedar OPEN en la DB estando cerrada en el broker."
    )
    try:
        from notify import send_push
        send_push(f"DB DESINCRONIZADA — {ticker}", cuerpo, priority="urgent")
    except Exception as e:
        print(f"  [live] ⚠️  tampoco se pudo avisar del desync: {e}")
    print(f"  [live] ⚠️  DESYNC {ticker} · fill={fill_price} · "
          f"order={order_id} · {detalle}")


class LiveExecutor(Executor):
    """
    Ejecución REAL. El ciclo de vida de la orden vive en broker_orders.py; acá
    está solo el pegamento con la interfaz que auto_run conoce.

    QUÉ HACE CADA MÉTODO
        open_position    : manda, sigue y confirma la orden contra el broker.
        sync_after_opens : baja a `positions` lo que se abrió (run_sync).
        close_position   : cierra contra el broker, VERIFICA contra el broker, y
                           registra el precio de salida real.

    LA ASIMETRÍA ENTRE ABRIR Y CERRAR
        Al abrir, la posición QUEDA en el broker: la verdad sigue ahí y run_sync
        la lee después. Al cerrar, la posición DESAPARECE y el precio de salida
        se pierde — sólo lo vio el executor, en el instante del fill. Por eso el
        cierre escribe directo y la apertura delega en el sync.

    LO QUE NO HACE, A PROPÓSITO
      - No arregla una pata suelta. La detecta, grita y devuelve False. Un
        arreglo automático equivocado deja una opción desnuda.
      - No reintenta si el broker no devolvió id: estado desconocido.
      - No reporta True por nada que el broker no haya confirmado.
      - No reintenta la escritura en la DB si falla: si la base está caída, el
        reintento tampoco escribe. Lo que sí hace es GRITAR con el fill adentro
        del mensaje, que es el dato irrecuperable.
    """
    mode = "live"

    def open_position(self, intent: OpenIntent) -> bool:
        from broker_orders import open_spread

        r = open_spread(intent)

        if r.ok:
            print(f"  [live] {intent.ticker} LLENA · id={r.order_id} · "
                  f"fill={r.fill_price}")
            # La DB se escribe en sync_after_opens(), una vez por run, cuando
            # auto_run termina el bucle de aperturas.
            return True

        if r.status == "partial":
            # Lo peor que puede pasar. Push urgente y parar.
            try:
                from notify import send_push
                send_push(
                    f"PATA SUELTA — {intent.ticker}",
                    f"Orden {r.order_id} con fill PARCIAL.\n"
                    f"{intent.ticker} ${intent.strike_low}/${intent.strike_high} "
                    f"{intent.expiration}\n\n"
                    f"Puede haber una opción DESNUDA en la cuenta. "
                    f"Revisar A MANO ya.",
                    priority="urgent",
                )
            except Exception as e:
                print(f"  [live] no se pudo avisar de la pata suelta: {e}")
            print(f"  [live] ⛔ {intent.ticker}: {r.detail}")
            return False

        print(f"  [live] {intent.ticker} NO abierta ({r.status}): {r.detail}")
        return False

    def close_position(self, ticker: str, reason: str) -> bool:
        """
        Cierra contra el BROKER y verifica contra el BROKER.

        Devuelve True SOLO si Tastytrade confirma que no queda ninguna pata.
        Un fill reportado no alcanza: si el código cree que cerró y no cerró,
        te quedaste con una posición real creyendo que no. Mismo espíritu que
        cmd_paper_close devolviendo False cuando no pudo pricear.

        `reason` llega como PROSA del LLM y se parte en código + rationale.
        Ver el docstring del módulo y trade._split_close_reason.
        """
        from broker_orders import close_spread, verify_closed

        r = close_spread(ticker, reason)

        if r.status == "partial":
            try:
                from notify import send_push
                send_push(
                    f"PATA SUELTA al cerrar — {ticker}",
                    f"Orden {r.order_id}: cierre PARCIAL.\n\n"
                    f"Puede haber una opción DESNUDA en la cuenta. "
                    f"Revisar A MANO ya.",
                    priority="urgent",
                )
            except Exception as e:
                print(f"  [live] no se pudo avisar de la pata suelta: {e}")
            print(f"  [live] ⛔ {ticker}: {r.detail}")
            return False

        if not r.ok:
            print(f"  [live] {ticker} NO cerrada ({r.status}): {r.detail}")
            return False

        print(f"  [live] {ticker} cierre llenó · id={r.order_id} · "
              f"fill={r.fill_price}")

        # ── REGISTRAR EL PRECIO DE SALIDA ─────────────────────────────────────
        # Este es el ÚNICO momento en que el precio de salida existe. En cuanto
        # la posición desaparece del broker, el dato se pierde para siempre:
        # run_sync detecta el cierre justamente porque ya no está, así que no
        # tiene qué precio consultar y marca CLOSED_PRICE_UNKNOWN.
        # El 17-jul CCL cerró a 0.43 y la DB registró -$44.00 (la pérdida máxima
        # entera) porque nadie escribió este número.
        # El 23-jul DLTR cerró a 1.87 y la DB quedó con P&L NULL porque la
        # escritura reventó por varchar(50) y el error sólo se imprimió.
        #
        # SIGNO: r.fill_price viene en la convención del sistema (>0 débito,
        # <0 crédito). close_position_in_db espera la prima RECIBIDA por acción.
        # Cerrar un BCS: fill -0.43 = crédito 0.43 = recibiste 0.43 -> se invierte.
        if r.fill_price is None:
            print(f"  [live] ⚠️  el broker no dio price de fill — el P&L de "
                  f"{ticker} va a quedar SIN DATO, no en cero.")
        else:
            recibido = -float(r.fill_price)
            row, why = _buscar_posicion_abierta(ticker)
            if row is None:
                # La posición cerró en el broker y no hay fila donde anotarlo.
                # El fill se pierde salvo que lo mandemos en el push.
                _alertar_desync(ticker, r.fill_price, r.order_id,
                                f"no se encontró la fila: {why}")
            else:
                pos_id, _ = row
                try:
                    import trade as trade_module
                    pnl = trade_module.close_position_in_db(
                        pos_id,
                        recibido,
                        close_reason="CLOSED_LIVE",
                        close_rationale=reason,
                    )
                    if pnl is not None:
                        print(f"  [live] DB id={pos_id} cerrada · prima recibida "
                              f"${recibido:.2f} · P&L ${pnl:.2f}")
                    else:
                        print(f"  [live] DB id={pos_id} cerrada · P&L SIN DATO")
                except Exception as e:
                    # NO se traga el error. La orden REAL ya se ejecutó: esto es
                    # el hueco entre el broker y la DB, donde vive todo el riesgo.
                    _alertar_desync(ticker, r.fill_price, r.order_id,
                                    f"falló el UPDATE de positions id={pos_id}: {e}")

        # La confirmación no es el fill: es que el broker no tenga nada.
        if not verify_closed(ticker):
            try:
                from notify import send_push
                send_push(
                    f"CIERRE DUDOSO — {ticker}",
                    f"La order {r.order_id} reportó fill, pero el broker todavía "
                    f"muestra legs abiertas de {ticker}.\n\nRevisar A MANO.",
                    priority="urgent",
                )
            except Exception:
                pass
            return False

        return True

    def sync_after_opens(self) -> None:
        """
        Baja del broker lo que auto_run acaba de abrir.

        Sin esto la posición existe en Tastytrade y NO en `positions`, y el
        monitor lee `positions`: quedaría sin stop loss hasta el próximo run
        —hasta 4 horas y media entre las 10:00 y las 14:30 ET— con plata real.

        UNA vez por run, no por posición: run_sync baja la cuenta entera.

        POR QUÉ run_sync Y NO ESCRIBIR LA FILA ACÁ
            Al ABRIR, la posición QUEDA en el broker: la verdad sigue disponible
            y group_spreads la lee con los precios reales de apertura.
            Al CERRAR es al revés — la posición desaparece y el precio se pierde
            — y por eso close_position sí escribe directo.
            La asimetría no es capricho: es dónde vive la verdad en cada momento.
        """
        import trade as trade_module
        print("  [live] sincronizando la DB con el broker...")
        trade_module.run_sync()


VALID_MODES = ("paper", "live")

def current_mode() -> str:
    """
    Fuente ÚNICA de TRADING_MODE. Ausente -> 'paper' (default seguro).
    Presente pero inválido -> explota: un typo no se adivina.
    """
    mode = os.getenv("TRADING_MODE", "paper").strip().lower()
    if mode not in VALID_MODES:
        raise RuntimeError(
            f"TRADING_MODE='{mode}' inválido. Válidos: {VALID_MODES}."
        )
    return mode


def get_executor() -> Executor:
    mode = current_mode()
    if mode == "live":
        # OJO: en la rama `live` este executor SÍ opera con plata real. El
        # mensaje anterior decía "no implementado, abortará" — heredado del stub
        # de la rama main. Leer eso en un log y creer que no pasó nada es
        # exactamente el fallo que este proyecto persigue: el log mintiendo
        # sobre lo que el sistema hizo.
        from broker_orders import executor_env
        print(f"  ⚠️  TRADING_MODE=live · EXECUTOR_ENV={executor_env()} — "
              f"LiveExecutor OPERA de verdad.")
        return LiveExecutor()
    return PaperExecutor()