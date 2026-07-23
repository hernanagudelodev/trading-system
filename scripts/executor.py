"""
executor.py  ·  RAMA `main` (paper)
===================================
Separa la DECISIÓN (auto_run decide qué hacer) de la EJECUCIÓN (cómo se hace).

auto_run produce INTENCIONES neutras ("abrir spread X", "cerrar Y") que no saben
nada de paper ni live. Un Executor las ejecuta. La bandera TRADING_MODE elige QUÉ
executor se instancia UNA sola vez — no hay 'if paper' desperdigado por el código.

    PaperExecutor : escribe en la DB
    LiveExecutor  : manda órdenes al broker. EN ESTA RAMA es un stub que se
                    niega a correr, a propósito: si algún día TRADING_MODE=live
                    cae en el worker de paper, explota en vez de operar.
                    El LiveExecutor REAL vive en la rama `live` (§23).

⚠️ ESTE ARCHIVO DIVERGE ENTRE RAMAS. No se propaga con
   `git checkout main -- scripts/executor.py`: eso pisaría el LiveExecutor real
   de `live` con el stub de acá. Todo cambio se aplica A MANO en las dos ramas.

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


class PaperExecutor(Executor):
    """
    Ejecución paper: delega en las funciones de trade.py que ya existen y ya
    están endurecidas (fix del $0.00, retorno booleano de cierre).
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
        """
        `reason` llega como PROSA del LLM. Se parte:
            close_reason    = "MANUAL"  -> código, entra en varchar(50)
            close_rationale = reason    -> texto completo, columna TEXT

        POR QUÉ "MANUAL" Y NO UN CÓDIGO NUEVO
            Es el código que este camino viene usando desde siempre: los 19
            cierres "MANUAL (LLM)" de las métricas de paper salieron de acá.
            Estrenar un código parte la serie histórica en dos sin ganar nada.

        EL BUG DEL 23-jul
            Antes esto pasaba `close_reason=reason` con la prosa entera. El
            UPDATE reventaba con `value too long for type character
            varying(50)`, el except de abajo lo capturaba, y auto_run reportaba
            "cierre NO ejecutado (sin precio real)" — un mensaje FALSO: el
            precio se había obtenido bien y el fallo era de esquema.
            DLTR y WFC quedaron abiertas por eso.
            trade._split_close_reason ahora lo ataja aunque el caller falle,
            pero el caller correcto es este.
        """
        import trade as trade_module
        # cmd_paper_close ya devuelve True/False (True solo si cerró de verdad)
        try:
            return bool(trade_module.cmd_paper_close(
                ticker,
                close_reason="MANUAL",
                close_rationale=reason,
            ))
        except Exception as e:
            print(f"  [paper] error cerrando {ticker}: {e}")
            return False


class LiveExecutor(Executor):
    """
    Ejecución real. NO implementado EN ESTA RAMA — placeholder deliberado que se
    niega a correr.

    El LiveExecutor de verdad vive en la rama `live` (broker_orders.py + el
    ciclo de vida de la orden). Este stub existe para que, si TRADING_MODE=live
    cayera por error en el worker de paper, el proceso EXPLOTE en vez de
    aparentar que operó.

    Ver CONTEXTO_PROYECTO.md §11 (executor) y §23 (las dos codebases).
    """
    mode = "live"

    def open_position(self, intent: OpenIntent) -> bool:
        raise NotImplementedError(
            "LiveExecutor no implementado en la rama `main`. El ejecutor real "
            "vive en la rama `live`. Ver CONTEXTO_PROYECTO.md §23."
        )

    def close_position(self, ticker: str, reason: str) -> bool:
        raise NotImplementedError(
            "LiveExecutor no implementado en la rama `main`. El ejecutor real "
            "vive en la rama `live`. Ver CONTEXTO_PROYECTO.md §23."
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
        print("  ⚠️  TRADING_MODE=live en la rama `main` — LiveExecutor es un "
              "stub, abortará. El ejecutor real está en la rama `live`.")
        return LiveExecutor()
    return PaperExecutor()