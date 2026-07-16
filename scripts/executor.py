"""
executor.py
===========
Separa la DECISIÓN (auto_run decide qué hacer) de la EJECUCIÓN (cómo se hace).

auto_run produce INTENCIONES neutras ("abrir spread X", "cerrar Y") que no saben
nada de paper ni live. Un Executor las ejecuta. La bandera TRADING_MODE elige QUÉ
executor se instancia UNA sola vez — no hay 'if paper' desperdigado por el código.

    PaperExecutor : escribe en la DB (comportamiento actual, sin cambios)
    LiveExecutor  : manda órdenes al broker + reconcilia (NO implementado aún)

Uso desde auto_run:
    from executor import get_executor, OpenIntent
    ex = get_executor()                      # lee TRADING_MODE, default 'paper'
    ok = ex.open_position(OpenIntent(...))   # bool: ejecutado o no
    ok = ex.close_position(ticker, reason)   # bool
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
    """Interfaz común. Ambos modos implementan estos dos métodos."""
    mode = "base"

    def open_position(self, intent: OpenIntent) -> bool:
        raise NotImplementedError

    def close_position(self, ticker: str, reason: str) -> bool:
        raise NotImplementedError


class PaperExecutor(Executor):
    """
    Ejecución paper: delega en las funciones de trade.py que ya existen y ya
    están endurecidas (fix del $0.00, retorno booleano de cierre). NO cambia
    el comportamiento actual — solo lo envuelve tras la interfaz.
    """
    mode = "paper"

    def open_position(self, intent: OpenIntent) -> bool:
        import trade as trade_module
        try:
            trade_module.cmd_paper_buy(
                intent.ticker,
                intent.strike_low,
                intent.strike_high,
                intent.expiration,
                intent.debit,
                context_json=intent.context_json,
                rationale=intent.rationale,
            )
            return True
        except Exception as e:
            print(f"  [paper] error abriendo {intent.ticker}: {e}")
            return False

    def close_position(self, ticker: str, reason: str) -> bool:
        import trade as trade_module
        # cmd_paper_close ya devuelve True/False (True solo si cerró de verdad)
        try:
            return bool(trade_module.cmd_paper_close(ticker))
        except Exception as e:
            print(f"  [paper] error cerrando {ticker}: {e}")
            return False


class LiveExecutor(Executor):
    """
    Ejecución REAL. El ciclo de vida de la orden vive en broker_orders.py;
    acá está solo el pegamento con la interfaz que auto_run conoce.

    ESTADO: open_position manda y confirma órdenes. NO ESCRIBE LA DB todavía.
    close_position sigue sin implementar.

    Consecuencia, y por eso este executor NO es desplegable aún: una posición
    abierta por acá existe en el broker y NO en `positions`. El monitor no la
    ve, o sea que no tiene stop loss. Solo sirve para probar contra sandbox.
    El escritor de DB es el bloque siguiente, y sale de ver la forma real de un
    fill — que se conoce mandando una orden de verdad en sandbox, no
    adivinándola.

    Lo que NO hace y es deliberado:
      - No arregla una pata suelta. La detecta, grita y devuelve False. Un
        arreglo automático equivocado deja una opción desnuda.
      - No reintenta a ciegas si el broker no devolvió id: estado desconocido.
      - No reporta True por nada que no sea un fill confirmado por el broker.
    """
    mode = "live"

    def open_position(self, intent: OpenIntent) -> bool:
        from broker_orders import abrir_spread

        r = abrir_spread(intent)

        if r.ok:
            print(f"  [live] {intent.ticker} LLENA · id={r.order_id} · "
                  f"fill={r.fill_price}")
            print(f"  [live] ⚠️  la posición NO quedó en la DB — el escritor "
                  f"todavía no existe. El monitor no la ve.")
            return True

        if r.estado == "partial":
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
            print(f"  [live] ⛔ {intent.ticker}: {r.detalle}")
            return False

        print(f"  [live] {intent.ticker} NO abierta ({r.estado}): {r.detalle}")
        return False

    def close_position(self, ticker: str, reason: str) -> bool:
        raise NotImplementedError(
            "LiveExecutor.close_position no implementado. Un cierre en vivo "
            "necesita leer la posición del broker, no de la DB."
        )


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
        print("  ⚠️  TRADING_MODE=live — LiveExecutor no implementado, abortará.")
        return LiveExecutor()
    return PaperExecutor()