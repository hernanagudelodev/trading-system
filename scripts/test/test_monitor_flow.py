"""
test_monitor_flow.py
====================
Prueba run_position_monitor SIN tocar la DB, el broker ni la red.

POR QUÉ EXISTE
    run_position_monitor es lo que EJECUTA los stops. Hasta hoy vivía clavada a
    `paper_positions` y cerraba con un UPDATE — o sea que en live no existía
    cierre automático de ninguna clase. Ahora decide igual para los dos libros y
    delega la ejecución al executor.
    El enrutamiento (qué tabla, qué executor) y la decisión (qué reason) son lo
    único que se puede probar sin mercado abierto. El cierre real necesita
    precio en vivo, y eso no se finge: se prueba con el mercado abierto.

Tres dobles:
    fake_db        reemplaza psycopg2.connect y devuelve filas controladas
    FakeExecutor   registra qué se le pidió cerrar, no ejecuta nada
    precio fijo    reemplaza get_spread_value_tastytrade

Cubre:
  A. enrutamiento: paper -> paper_positions · live -> positions
  B. STOP_LOSS dispara al pasar el umbral por DTE
  C. TARGET_REACHED al 70% del máximo
  D. TIME_EXPIRED con DTE <= 7
  E. el reason VIAJA hasta el executor (antes se perdía y todo era MANUAL)
  F. sin precio fresco NO se cierra
  G. cierre fallido -> la posición sigue OPEN y se avisa
  H. sin cierre -> UPDATE de P&L sobre la tabla del libro activo

Uso:
    python scripts/test/test_monitor_flow.py
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

os.environ["ACCOUNT_NLV"] = "14100"
os.environ["MAX_PORTFOLIO_RISK_PCT"] = "40"
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:1/fake")
os.environ.setdefault("EXECUTOR_ENV", "sandbox")

import psycopg2

import executor as executor_mod
import monitor

fallos = []


def check(nombre, ok, detalle=""):
    print(f"  {'✓' if ok else '✗'} {nombre}" + (f"\n      {detalle}" if detalle and not ok else ""))
    if not ok:
        fallos.append(nombre)


# ══════════════════════════════════════════════════════════════════════════════
# DOBLES
# ══════════════════════════════════════════════════════════════════════════════

class FakeExecutor(executor_mod.Executor):
    mode = "fake"

    def __init__(self, close_ok=True):
        self.closed = []          # [(ticker, reason)]
        self.close_ok = close_ok

    def close_position(self, ticker, reason):
        self.closed.append((ticker, reason))
        return self.close_ok


class FakeCursor:
    def __init__(self, rows, registro):
        self._rows = rows
        self._reg  = registro
        self.description = [
            ("id",), ("ticker",), ("strategy",), ("strike_low",), ("strike_high",),
            ("expiration",), ("contracts",), ("total_cost",), ("premium_paid",),
            ("current_spread_value",), ("gross_pnl",), ("pnl_pct",),
            ("profit_pct_of_max",), ("opened_at",),
        ]

    def execute(self, sql, params=None):
        self._reg.append(" ".join(sql.split()))
        return None

    def fetchall(self):
        return self._rows

    def close(self):
        return None


class FakeConn:
    def __init__(self, rows, registro):
        self._rows, self._reg = rows, registro

    def cursor(self):
        return FakeCursor(self._rows, self._reg)

    def commit(self):
        return None

    def close(self):
        return None


_real_connect = psycopg2.connect


def fila(ticker, strategy, sl, sh, premium, total_cost, dte, contracts=1):
    """Una fila de posición, en el orden exacto del SELECT del monitor."""
    return (1, ticker, strategy, sl, sh, date.today() + timedelta(days=dte),
            contracts, total_cost, premium, None, None, None, None, None)


def correr(rows, mode, spread_value, close_ok=True):
    """Corre run_position_monitor con todo fingido. Devuelve (fake, sqls)."""
    registro = []
    psycopg2.connect = lambda *a, **k: FakeConn(rows, registro)

    fake = FakeExecutor(close_ok=close_ok)
    executor_mod.get_executor = lambda: fake
    executor_mod.current_mode = lambda: mode
    monitor.get_spread_value_tastytrade = lambda *a, **k: spread_value
    monitor.send_push = lambda *a, **k: True
    monitor.time.sleep = lambda s: None

    try:
        monitor.run_position_monitor()
    finally:
        psycopg2.connect = _real_connect
    return fake, registro


# BCS 100/105, débito 2.00 -> costo $200, max profit $300
BCS = dict(strategy="Bull Call Spread", sl=100, sh=105, premium=2.0, total_cost=200.0)
# BPS 100/105, crédito 1.50 -> max profit $150, max loss $350
BPS = dict(strategy="Bull Put Spread", sl=100, sh=105, premium=-1.5, total_cost=-150.0)


# ══════════════════════════════════════════════════════════════════════════════
print("\n  === A · enrutamiento: la tabla sale del modo ===")

_, sqls = correr([fila("AAPL", **BCS, dte=30)], "paper", 2.10)
sel = next((q for q in sqls if "SELECT" in q), "")
check("paper  -> FROM paper_positions", "FROM paper_positions" in sel, sel[:90])

_, sqls = correr([fila("AAPL", **BCS, dte=30)], "live", 2.10)
sel = next((q for q in sqls if "SELECT" in q), "")
check("live   -> FROM positions", "FROM positions" in sel and "paper_positions" not in sel,
      sel[:90])


# ══════════════════════════════════════════════════════════════════════════════
print("\n  === B · STOP_LOSS (umbral por DTE) ===")

# BCS costo $200, DTE 30 -> stop -65% -> cierra bajo $70 de valor -> 0.70/acción
fake, _ = correr([fila("AAPL", **BCS, dte=30)], "paper", 0.65)
check("−67.5% con 30 DTE dispara STOP_LOSS",
      fake.closed == [("AAPL", "STOP_LOSS")], f"closed={fake.closed}")

fake, _ = correr([fila("AAPL", **BCS, dte=30)], "paper", 0.75)
check("−62.5% con 30 DTE NO dispara (umbral −65%)",
      fake.closed == [], f"closed={fake.closed}")

# Con 10 DTE el umbral se aprieta a −55%: el mismo −62.5% ahora sí cierra
fake, _ = correr([fila("AAPL", **BCS, dte=10)], "paper", 0.75)
check("el mismo −62.5% con 10 DTE SÍ dispara (umbral −55%)",
      fake.closed == [("AAPL", "STOP_LOSS")], f"closed={fake.closed}")


# ══════════════════════════════════════════════════════════════════════════════
print("\n  === C · TARGET_REACHED (70% del máximo) ===")

# BCS: max profit $300. 70% = $210 de ganancia -> valor $410 -> 4.10/acción
fake, _ = correr([fila("AAPL", **BCS, dte=30)], "paper", 4.15)
check("+71.7% del máximo dispara TARGET_REACHED",
      fake.closed == [("AAPL", "TARGET_REACHED")], f"closed={fake.closed}")

fake, _ = correr([fila("AAPL", **BCS, dte=30)], "paper", 4.00)
check("+66.7% del máximo NO dispara",
      fake.closed == [], f"closed={fake.closed}")


# ══════════════════════════════════════════════════════════════════════════════
print("\n  === D · TIME_EXPIRED (DTE <= 7) ===")

fake, _ = correr([fila("AAPL", **BCS, dte=5)], "paper", 2.10)
check("DTE 5 dispara TIME_EXPIRED",
      fake.closed == [("AAPL", "TIME_EXPIRED")], f"closed={fake.closed}")

fake, _ = correr([fila("AAPL", **BCS, dte=5)], "paper", 4.50)
check("TIME_EXPIRED gana sobre TARGET (se evalúa primero)",
      fake.closed == [("AAPL", "TIME_EXPIRED")], f"closed={fake.closed}")


# ══════════════════════════════════════════════════════════════════════════════
print("\n  === E · el reason VIAJA hasta el executor ===")
# Antes: PaperExecutor tiraba `reason` y cmd_paper_close clavaba 'MANUAL'.
# Todo stop se habría registrado como MANUAL y la estadística sería inservible.

fake, _ = correr([fila("HD", **BPS, dte=30)], "paper", 3.00)
check("BPS: crédito 1.50, cerrar cuesta 3.00 -> STOP_LOSS con el reason intacto",
      fake.closed == [("HD", "STOP_LOSS")], f"closed={fake.closed}")


# ══════════════════════════════════════════════════════════════════════════════
print("\n  === F · sin precio fresco NO se cierra ===")

fake, _ = correr([fila("AAPL", **BCS, dte=30)], "paper", None)
check("precio None -> cero cierres",
      fake.closed == [], f"closed={fake.closed}")


# ══════════════════════════════════════════════════════════════════════════════
print("\n  === G · el cierre FALLA -> la posición sigue abierta ===")

fake, sqls = correr([fila("AAPL", **BCS, dte=30)], "paper", 0.65, close_ok=False)
check("se intentó cerrar", fake.closed == [("AAPL", "STOP_LOSS")], f"{fake.closed}")
ups = [q for q in sqls if q.startswith("UPDATE")]
check("cae al UPDATE de P&L (sigue viva, su estado importa)",
      len(ups) == 1, f"{len(ups)} UPDATE(s)")
check("el UPDATE NO la marca CLOSED",
      ups and "status = 'CLOSED'" not in ups[0], ups[0][:90] if ups else "")


# ══════════════════════════════════════════════════════════════════════════════
print("\n  === H · sin cierre -> UPDATE sobre la tabla del libro ===")

_, sqls = correr([fila("AAPL", **BCS, dte=30)], "live", 2.10)
ups = [q for q in sqls if q.startswith("UPDATE")]
check("live -> UPDATE positions",
      ups and "UPDATE positions SET" in ups[0], ups[0][:60] if ups else "sin UPDATE")

_, sqls = correr([fila("AAPL", **BCS, dte=30)], "paper", 2.10)
ups = [q for q in sqls if q.startswith("UPDATE")]
check("paper -> UPDATE paper_positions",
      ups and "UPDATE paper_positions SET" in ups[0], ups[0][:60] if ups else "sin UPDATE")


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
if fallos:
    print(f"  {len(fallos)} FALLO(S): {fallos}")
    sys.exit(1)
print("  Todo OK.")
print("=" * 60 + "\n")