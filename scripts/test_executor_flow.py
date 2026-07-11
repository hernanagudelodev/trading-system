"""
test_executor_flow.py
=====================
Prueba que execute_recommendations enruta bien las intenciones al executor,
SIN tocar la DB ni el broker. Usa un FakeExecutor que solo registra lo pedido.

Verifica:
  - aperturas y cierres se enrutan al executor
  - el bloqueo de concentración (mismo ticker) funciona
  - solo se cuenta como 'closed'/'opened' lo que el executor confirma (True)
  - un executor que devuelve False => va a 'errors', no a 'opened/closed'

Uso:
    python test_executor_flow.py
"""
import os
import sys

# Evitar que el bloqueo de concentración intente conectarse a la DB real:
# apuntamos a algo inválido y el código ya maneja el fallo (sigue sin bloqueo).
os.environ["DATABASE_URL"] = "postgresql://invalid:invalid@localhost:1/none"
os.environ["TRADING_MODE"] = "paper"

import auto_run
import executor as executor_mod


class FakeExecutor(executor_mod.Executor):
    mode = "fake"
    def __init__(self, open_ok=True, close_ok=True):
        self.opened = []
        self.closed = []
        self.open_ok = open_ok
        self.close_ok = close_ok
    def open_position(self, intent):
        self.opened.append(intent.ticker)
        return self.open_ok
    def close_position(self, ticker, reason):
        self.closed.append(ticker)
        return self.close_ok


def run_case(nombre, analysis, open_ok=True, close_ok=True,
             stub_exp=True):
    fake = FakeExecutor(open_ok=open_ok, close_ok=close_ok)
    executor_mod.get_executor = lambda: fake
    # Evitar llamada real a la cadena para la expiración
    if stub_exp:
        import option_selector
        import datetime
        option_selector.get_real_expiration = lambda t: datetime.date(2026, 7, 17)

    results = auto_run.execute_recommendations(analysis)
    print(f"\n  === {nombre} ===")
    print(f"    executor.open pedidos : {fake.opened}")
    print(f"    executor.close pedidos: {fake.closed}")
    print(f"    results opened : {[o['ticker'] for o in results['opened']]}")
    print(f"    results closed : {[c['ticker'] for c in results['closed']]}")
    print(f"    results errors : {results['errors']}")
    return fake, results


# Caso 1: 2 aperturas distintas + 1 cierre, todo OK
run_case("2 abren + 1 cierra (todo OK)", {
    "new_trades": [
        {"ticker": "AAPL", "strike_low": 100, "strike_high": 105, "debit": -1.2},
        {"ticker": "MSFT", "strike_low": 300, "strike_high": 310, "debit": 2.0},
    ],
    "close_positions": [{"ticker": "NVDA", "reason": "tesis rota"}],
})

# Caso 2: duplicado en el mismo run (AAPL dos veces) -> el 2do se bloquea
run_case("duplicado mismo run (AAPL x2)", {
    "new_trades": [
        {"ticker": "AAPL", "strike_low": 100, "strike_high": 105, "debit": -1.2},
        {"ticker": "AAPL", "strike_low": 95,  "strike_high": 100, "debit": -1.0},
    ],
    "close_positions": [],
})

# Caso 3: cierre que el executor NO logra (False) -> va a errors, no a closed
run_case("cierre falla (executor False)", {
    "new_trades": [],
    "close_positions": [{"ticker": "TSLA", "reason": "sin precio"}],
}, close_ok=False)

print("\n  Revisá: caso 2 debe abrir solo 1 AAPL; caso 3 debe tener TSLA en errors, no en closed.\n")