"""
test_executor_flow.py
=====================
Prueba execute_recommendations SIN tocar la DB ni el broker.

Reescrito tras el gate de cartera. El test viejo apuntaba DATABASE_URL a una DB
inválida y contaba con que el código siguiera igual (fail-OPEN). Ahora el código
es fail-CLOSED: sin poder leer la cartera no abre nada. Ese comportamiento es
justamente uno de los casos que hay que probar, así que la DB se FINGE en vez de
romperse.

Dos dobles:
  FakeExecutor  -> registra qué se le pidió abrir/cerrar, no ejecuta nada
  fake_connect  -> reemplaza psycopg2.connect y devuelve filas controladas

Cubre:
  A. position_max_loss: BCS, BPS, contratos > 1
  B. enrutamiento de aperturas y cierres al executor
  C. concentración: mismo ticker dentro del run
  D. concentración: ticker ya OPEN en la DB
  E. cierre fallido -> errors, no closed
  F. tope de cartera: rechaza cuando la cartera existente ya está cerca del tope
  G. tope de cartera: ACUMULA dentro del run (el 2do trade ve el riesgo del 1ro)
  H. fail-closed: DB ilegible -> 0 aperturas, error registrado, cierres conservados
  I. la var es obligatoria: si falta, explota en vez de defaultear a 'sin tope'

Uso (desde cualquier carpeta):
    python scripts/test/test_executor_flow.py
"""
import os
import sys

# El test vive en scripts/test/ y los módulos en scripts/. Python solo mete en
# sys.path la carpeta del propio script, así que hay que subir un nivel.
# Mismo patrón que V2/migrate.py.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ── Entorno ANTES de importar nada del sistema ────────────────────────────────
# CAPITAL se congela al importar option_selector (module-level os.getenv).
os.environ["ACCOUNT_NLV"] = "14100"
os.environ["TRADING_MODE"] = "paper"
os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost:1/fake")

import psycopg2

import auto_run
import executor as executor_mod
import option_selector


CAPITAL = 14100.0

fallos = []


def check(nombre, condicion, detalle=""):
    if condicion:
        print(f"  ✓ {nombre}")
    else:
        print(f"  ✗ {nombre}   {detalle}")
        fallos.append(nombre)


# ══════════════════════════════════════════════════════════════════════════════
# DOBLES
# ══════════════════════════════════════════════════════════════════════════════

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


class FakeCursor:
    """Devuelve siempre las mismas filas, sea cual sea el SQL."""
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return None

    def fetchall(self):
        return self._rows

    def close(self):
        return None


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)

    def close(self):
        return None


_real_connect = psycopg2.connect


def fake_db(rows):
    """
    rows: lista de tuplas (ticker, strike_low, strike_high, premium_paid, contracts)
    Pasar None => la conexión revienta (para probar fail-closed).
    """
    def _connect(*a, **kw):
        if rows is None:
            raise RuntimeError("DB caída (simulado)")
        return FakeConn(rows)
    psycopg2.connect = _connect


def restore_db():
    psycopg2.connect = _real_connect


def run_case(analysis, db_rows=(), pct=None, open_ok=True, close_ok=True):
    """Corre execute_recommendations con la DB y el executor fingidos."""
    fake = FakeExecutor(open_ok=open_ok, close_ok=close_ok)
    executor_mod.get_executor = lambda: fake

    # La expiración real requiere red — se estabiliza.
    import datetime
    option_selector.get_real_expiration = lambda t: datetime.date(2026, 8, 21)

    if pct is None:
        os.environ.pop("MAX_PORTFOLIO_RISK_PCT", None)
    else:
        os.environ["MAX_PORTFOLIO_RISK_PCT"] = str(pct)

    fake_db(db_rows)
    try:
        results = auto_run.execute_recommendations(analysis)
    finally:
        restore_db()
    return fake, results


# Estructuras reutilizables.
# BCS 100/105 debit 2.00  -> pérdida máx = 2.00 * 100 = $200
BCS_200 = {"ticker": "AAPL", "strike_low": 100, "strike_high": 105, "debit": 2.0}
# BPS 100/105 crédito 1.20 -> pérdida máx = (5 - 1.20) * 100 = $380
BPS_380 = {"ticker": "MSFT", "strike_low": 100, "strike_high": 105, "debit": -1.2}


# ══════════════════════════════════════════════════════════════════════════════
# A — position_max_loss
# ══════════════════════════════════════════════════════════════════════════════
print("\n  === A · position_max_loss ===")

pml = option_selector.position_max_loss

check("BCS: débito 2.00 -> $200",
      pml(100, 105, 2.0) == 200.0, f"dio {pml(100, 105, 2.0)}")

check("BPS: crédito 1.20 sobre ancho 5 -> $380",
      pml(100, 105, -1.2) == 380.0, f"dio {pml(100, 105, -1.2)}")

check("contratos=3 multiplica: $200 -> $600",
      pml(100, 105, 2.0, 3) == 600.0, f"dio {pml(100, 105, 2.0, 3)}")

check("contracts=None se trata como 1",
      pml(100, 105, 2.0, None) == 200.0, f"dio {pml(100, 105, 2.0, None)}")

check("strikes invertidos no cambian el ancho",
      pml(105, 100, -1.2) == 380.0, f"dio {pml(105, 100, -1.2)}")


# ══════════════════════════════════════════════════════════════════════════════
# B — enrutamiento
# ══════════════════════════════════════════════════════════════════════════════
print("\n  === B · enrutamiento (cartera vacía, tope holgado) ===")

fake, res = run_case({
    "new_trades": [BCS_200, BPS_380],
    "close_positions": [{"ticker": "NVDA", "reason": "tesis rota"}],
}, db_rows=[], pct=40)

check("2 aperturas llegan al executor", fake.opened == ["AAPL", "MSFT"], f"opened={fake.opened}")
check("1 cierre llega al executor", fake.closed == ["NVDA"], f"closed={fake.closed}")
check("results.opened refleja las 2", len(res["opened"]) == 2, f"{res['opened']}")
check("sin errores", res["errors"] == [], f"{res['errors']}")


# ══════════════════════════════════════════════════════════════════════════════
# C — concentración dentro del run
# ══════════════════════════════════════════════════════════════════════════════
print("\n  === C · concentración: mismo ticker 2x en el run ===")

fake, res = run_case({
    "new_trades": [
        {"ticker": "AAPL", "strike_low": 100, "strike_high": 105, "debit": 2.0},
        {"ticker": "AAPL", "strike_low": 95,  "strike_high": 100, "debit": 1.0},
    ],
    "close_positions": [],
}, db_rows=[], pct=40)

check("solo se abre 1 AAPL", fake.opened == ["AAPL"], f"opened={fake.opened}")


# ══════════════════════════════════════════════════════════════════════════════
# D — concentración contra la DB
# ══════════════════════════════════════════════════════════════════════════════
print("\n  === D · concentración: ticker ya OPEN en la DB ===")

fake, res = run_case({
    "new_trades": [BCS_200, BPS_380],
    "close_positions": [],
}, db_rows=[("AAPL", 90, 95, 1.0, 1)], pct=40)

check("AAPL bloqueado por estar OPEN", "AAPL" not in fake.opened, f"opened={fake.opened}")
check("MSFT sí se abre", fake.opened == ["MSFT"], f"opened={fake.opened}")


# ══════════════════════════════════════════════════════════════════════════════
# E — cierre fallido
# ══════════════════════════════════════════════════════════════════════════════
print("\n  === E · cierre que el executor NO logra ===")

fake, res = run_case({
    "new_trades": [],
    "close_positions": [{"ticker": "TSLA", "reason": "sin precio"}],
}, db_rows=[], pct=40, close_ok=False)

check("TSLA NO va a closed", res["closed"] == [], f"closed={res['closed']}")
check("TSLA va a errors", len(res["errors"]) == 1, f"errors={res['errors']}")


# ══════════════════════════════════════════════════════════════════════════════
# F — tope de cartera rechaza
# ══════════════════════════════════════════════════════════════════════════════
print("\n  === F · tope de cartera: la cartera existente ya llena el tope ===")

# Tope 3% de 14100 = $423. Cartera existente: BCS con débito 4.00 = $400.
# Nuevo AAPL = $200. 400 + 200 = 600 > 423 -> rechazo.
fake, res = run_case({
    "new_trades": [BCS_200],
    "close_positions": [],
}, db_rows=[("XYZ", 50, 55, 4.0, 1)], pct=3)

check("AAPL rechazado por tope agregado", fake.opened == [], f"opened={fake.opened}")
check("el rechazo NO cuenta como error", res["errors"] == [], f"errors={res['errors']}")


# ══════════════════════════════════════════════════════════════════════════════
# G — el tope ACUMULA dentro del run
# ══════════════════════════════════════════════════════════════════════════════
print("\n  === G · tope de cartera: acumula dentro del run ===")

# Tope 4% de 14100 = $564. Cartera vacía.
# MSFT (BPS) = $380  -> 0 + 380 = 380 <= 564  -> abre
# GOOG (BPS) = $380  -> 380 + 380 = 760 > 564 -> rechaza
fake, res = run_case({
    "new_trades": [
        {"ticker": "MSFT", "strike_low": 100, "strike_high": 105, "debit": -1.2},
        {"ticker": "GOOG", "strike_low": 100, "strike_high": 105, "debit": -1.2},
    ],
    "close_positions": [],
}, db_rows=[], pct=4)

check("el 1ro abre", "MSFT" in fake.opened, f"opened={fake.opened}")
check("el 2do se rechaza por el riesgo del 1ro", "GOOG" not in fake.opened,
      f"opened={fake.opened} — si abre, current_risk NO se está acumulando")


# ══════════════════════════════════════════════════════════════════════════════
# H — fail-closed
# ══════════════════════════════════════════════════════════════════════════════
print("\n  === H · DB ilegible -> no se abre nada ===")

fake, res = run_case({
    "new_trades": [BCS_200, BPS_380],
    "close_positions": [{"ticker": "NVDA", "reason": "tesis rota"}],
}, db_rows=None, pct=40)

check("CERO aperturas con la DB caída", fake.opened == [], f"opened={fake.opened}")
check("se registra el error", len(res["errors"]) >= 1, f"errors={res['errors']}")
check("los cierres SÍ se conservan", res["closed"] and res["closed"][0]["ticker"] == "NVDA",
      f"closed={res['closed']} — un cierre no depende de poder leer la cartera")


# ══════════════════════════════════════════════════════════════════════════════
# I — la var es OBLIGATORIA en los dos libros
# ══════════════════════════════════════════════════════════════════════════════
print("\n  === I · sin MAX_PORTFOLIO_RISK_PCT -> explota (no defaultea) ===")

# Antes esto caía a 100% = sin tope. Un tope ausente no es "sin tope", es un bug:
# es como MAX_COST terminó decorativo y dejó pasar el GS de $3,945.
try:
    fake, res = run_case({
        "new_trades": [BCS_200],
        "close_positions": [],
    }, db_rows=[], pct=None)
    check("falta la var -> RuntimeError", False,
          f"NO explotó: abrió {fake.opened} — el tope volvió a ser decorativo")
except RuntimeError as e:
    check("falta la var -> RuntimeError", "MAX_PORTFOLIO_RISK_PCT" in str(e), str(e))
except Exception as e:
    check("falta la var -> RuntimeError", False, f"explotó pero con {type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
if fallos:
    print(f"  {len(fallos)} FALLO(S): {fallos}")
    sys.exit(1)
print("  Todo OK.")
print("=" * 60 + "\n")