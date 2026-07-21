"""
test_reprice_logic.py
=====================
Valida el loop de reprice de apertura (B1) SIN red, SIN broker, SIN dinero.

QUÉ PRUEBA
    La lógica que cambió en _open_async: subir del mid hacia el ask/bid real,
    frenar en el ask/bid (price_floor) y en el gate de riesgo, y saltar el
    candidato si el quote no llega en QUOTE_INTENTOS.

QUÉ NO PRUEBA (y por qué no puede)
    El fill real. El mercado se modela con un `fill_at` por caso: el precio al
    que "el mercado" llena. Es un modelo, no Tastytrade. Confirma que el loop
    DECIDE bien dado un mercado; no que el feed exista. Eso se prueba en vivo.

TODO lo que toca red está mockeado:
    _session_and_account, _resolve_legs, _track_order, _wait_for_fills,
    _fill_price, _cancel_order, _build_order, pricing._fetch_spread_quote_async

Lo que corre DE VERDAD (bajo prueba):
    el flujo de _open_async, _order_price (conversión de signo), el clamp al
    floor, la secuencia de reintentos del quote, y las ramas de decisión.

REQUISITOS
    - Rama `live` (broker_orders.py solo existe ahí).
    - Los tres bloques del cambio B1 YA aplicados. Si no, las aserciones del
      floor y del skip fallan — y eso te avisa que el cambio no está puesto.

Uso:
    python scripts/test/test_reprice_logic.py
"""
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Defaults defensivos: el test no los usa (todo mockeado), pero evitan que un
# import incidental de option_selector explote por env vars ausentes.
os.environ.setdefault("ACCOUNT_NLV", "14100")
os.environ.setdefault("MAX_PORTFOLIO_RISK_PCT", "40")
os.environ.setdefault("EXECUTOR_ENV", "sandbox")
os.environ.setdefault("TRADING_MODE", "live")

import broker_orders as bo
import pricing
from executor import OpenIntent

# Sin esperas reales entre reintentos de quote.
bo.QUOTE_ESPERA = 0


# ══════════════════════════════════════════════════════════════════════════════
# DOBLES — el mundo exterior, controlado
# ══════════════════════════════════════════════════════════════════════════════

# Estado del caso en curso: los fakes lo leen. Se resetea por caso.
CASE = {}


class FakeOrder:
    """Lo único que el loop necesita de una orden: su precio."""
    def __init__(self, price):
        self.price = price


class FakeResp:
    def __init__(self, oid):
        self.id    = oid
        self.order = None      # _order_id_from mira .order y después .id


class FakePreview:
    buying_power_effect = None


class FakeFinal:
    legs = []                  # sin legs -> la rama "partial" no se dispara


class FakeAccount:
    def __init__(self):
        self.last_price = None
        self.placed     = 0
        self.replaced   = 0
        self.cancelled  = 0

    async def place_order(self, session, order, dry_run=False):
        if dry_run:
            return FakePreview()
        self.placed    += 1
        self.last_price = order.price
        return FakeResp(f"ord-{self.placed}")

    async def replace_order(self, session, order_id, order):
        self.replaced  += 1
        self.last_price = order.price
        return FakeResp(order_id)


def _cruza(opt, price):
    """¿El límite `price` cruza el mercado modelado (CASE['fill_at'])?"""
    fill_at = CASE["fill_at"]
    if fill_at is None:
        return False
    if opt == "call":                       # débito: llenás al pagar >= ask
        return abs(float(price)) >= fill_at - 1e-9
    return float(price) <= fill_at + 1e-9   # crédito: llenás al pedir <= bid


# ── Reemplazos de las funciones que tocan red ─────────────────────────────────

async def fake_session_and_account():
    return None, CASE["account"], "sandbox"

async def fake_resolve_legs(session, intent):
    return ["leg_low", "leg_high"], None

async def fake_track_order(session, account, order_id, attempts=None):
    filled = _cruza(CASE["opt"], account.last_price)
    return ("filled" if filled else "timeout"), FakeFinal()

async def fake_wait_for_fills(session, account, order_id, placed):
    return placed

def fake_fill_price(placed):
    return round(abs(float(CASE["account"].last_price)), 2)

async def fake_cancel_order(session, account, order_id):
    account.cancelled += 1
    return True

def fake_build_order(legs, price, ext_id):
    return FakeOrder(price)


def _make_quote_mock(returns):
    """Devuelve cada valor de `returns` en orden; cuenta las llamadas."""
    it = iter(returns)
    async def _mock(ticker, sl, sh, exp, opt):
        CASE["quote_calls"] += 1
        try:
            return next(it)
        except StopIteration:
            return returns[-1]
    return _mock


def _install_fakes():
    bo._session_and_account = fake_session_and_account
    bo._resolve_legs        = fake_resolve_legs
    bo._track_order         = fake_track_order
    bo._wait_for_fills      = fake_wait_for_fills
    bo._fill_price          = fake_fill_price
    bo._cancel_order        = fake_cancel_order
    bo._build_order         = fake_build_order
    # _order_price y _order_id_from se dejan REALES: son la conversión de signo
    # y el parseo de id, justo lo que queremos ejercitar.


# ══════════════════════════════════════════════════════════════════════════════
# CASOS
# ══════════════════════════════════════════════════════════════════════════════

def q(mid, bid, ask):
    return {"mid": mid, "bid": bid, "ask": ask}


# Cada caso declara su mercado (quote + fill_at), su gate de riesgo, y lo que
# se espera. techo se expresa como el débito/crédito máximo por acción que el
# gate permite — reproduce exactamente position_max_loss<=MAX_RISK para 1
# contrato (débito*100<=MAX_RISK  ->  débito<=MAX_RISK/100).
CASES = [
    {
        "name":     "1. call · ask cerca del mid (FITB) -> llena",
        "opt":      "call", "debit": 2.62,
        "quotes":   [q(2.40, 2.34, 2.46)],
        "fill_at":  2.46, "gate": lambda i, p: abs(float(p)) <= 4.50,
        "expect":   {"status": "filled", "fill": 2.46},
    },
    {
        "name":     "2. call · ask lejos del mid (DLTR) -> llena en el ask",
        "opt":      "call", "debit": 4.00,
        "quotes":   [q(4.00, 3.85, 4.15)],
        "fill_at":  4.15, "gate": lambda i, p: abs(float(p)) <= 4.30,
        "expect":   {"status": "filled", "fill": 4.15},
    },
    {
        "name":     "3. call · ask > techo -> corta en el gate, NO abre",
        "opt":      "call", "debit": 4.00,
        "quotes":   [q(4.00, 3.85, 4.30)],
        "fill_at":  4.30, "gate": lambda i, p: abs(float(p)) <= 4.10,
        "expect":   {"status": "timeout", "reason": "riesgo"},
    },
    {
        "name":     "4. quote None x6 -> salta el candidato, NO manda orden",
        "opt":      "call", "debit": 3.00,
        "quotes":   [None, None, None, None, None, None],
        "fill_at":  None, "gate": lambda i, p: True,
        "expect":   {"status": "timeout", "reason": "no se obtuvo quote",
                     "placed": 0, "quote_calls": 6},
    },
    {
        "name":     "5. quote None,None,OK -> el retry se recupera y llena",
        "opt":      "call", "debit": 2.62,
        "quotes":   [None, None, q(2.40, 2.34, 2.46)],
        "fill_at":  2.46, "gate": lambda i, p: abs(float(p)) <= 4.50,
        "expect":   {"status": "filled", "fill": 2.46, "quote_calls": 3},
    },
    {
        "name":     "6. put · crédito -> llena en el bid (signo invertido)",
        "opt":      "put", "debit": -1.38,
        "quotes":   [q(1.38, 1.30, 1.46)],
        "fill_at":  1.30, "gate": lambda i, p: True,
        "expect":   {"status": "filled", "fill": 1.30},
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def _run_case(c):
    account = FakeAccount()
    CASE.clear()
    CASE.update({
        "opt":         c["opt"],
        "fill_at":     c["fill_at"],
        "account":     account,
        "quote_calls": 0,
    })
    bo._concession_allowed = c["gate"]
    pricing._fetch_spread_quote_async = _make_quote_mock(c["quotes"])

    intent = OpenIntent(
        ticker="TEST",
        strike_low=100.0,
        strike_high=105.0,
        expiration="2026-08-21",
        debit=c["debit"],
        rationale="test reprice",
    )

    res  = asyncio.run(bo._open_async(intent))
    exp  = c["expect"]
    errs = []

    if res.status != exp["status"]:
        errs.append(f"status: esperado {exp['status']}, obtuvo {res.status} "
                    f"({res.detail})")

    if "reason" in exp and exp["reason"].lower() not in res.detail.lower():
        errs.append(f"detalle: esperaba contener '{exp['reason']}', "
                    f"obtuvo '{res.detail}'")

    if "fill" in exp:
        got = round(abs(float(account.last_price)), 2)
        if abs(got - exp["fill"]) > 1e-6:
            errs.append(f"precio final: esperado {exp['fill']:.2f}, obtuvo {got:.2f}")

    if "placed" in exp and account.placed != exp["placed"]:
        errs.append(f"ordenes enviadas: esperado {exp['placed']}, "
                    f"obtuvo {account.placed}")

    if "quote_calls" in exp and CASE["quote_calls"] != exp["quote_calls"]:
        errs.append(f"intentos de quote: esperado {exp['quote_calls']}, "
                    f"obtuvo {CASE['quote_calls']}")

    ok = not errs
    marca = "✓" if ok else "✗"
    print(f"\n  {marca} {c['name']}")
    print(f"      status={res.status} · price_final={account.last_price} · "
          f"placed={account.placed} · replaced={account.replaced} · "
          f"quote_calls={CASE['quote_calls']}")
    if not ok:
        for e in errs:
            print(f"        - {e}")
    return ok


def main():
    print("=" * 64)
    print("  TEST — lógica de reprice de apertura (B1) · sin red, sin dinero")
    print("=" * 64)

    _install_fakes()

    fallos = [c["name"] for c in CASES if not _run_case(c)]

    print("\n" + "=" * 64)
    if fallos:
        print(f"  {len(fallos)} CASO(S) FALLARON:")
        for n in fallos:
            print(f"    ✗ {n}")
        print("=" * 64 + "\n")
        sys.exit(1)
    print(f"  TODOS LOS CASOS PASARON ({len(CASES)}/{len(CASES)})")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    main()