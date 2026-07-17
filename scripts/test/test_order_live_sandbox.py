"""
test_order_live_sandbox.py
==========================
MANDA UNA ORDEN DE VERDAD. En SANDBOX, cuenta 5WV27378, plata falsa.

POR QUÉ EXISTE
    El escritor de DB necesita saber qué campos trae un fill: precio, cantidad,
    timestamps. Esa forma no se adivina — se ve mandando una orden. Este script
    manda una, la sigue hasta el fill, y VUELCA EL OBJETO ENTERO.

    Aprovecha la regla de juguete del sandbox: un límite < $3 llena al instante.

TRES CANDADOS ANTES DE MANDAR
    1. EXECUTOR_ENV debe ser 'sandbox'. Si no, aborta.
    2. La cuenta no puede ser 5WI77328 (la real). broker_orders ya lo verifica;
       acá se verifica otra vez, porque es lo único irreversible del script.
    3. Pide confirmación escrita. Es la única cosa de todo el sistema que manda
       una orden a mano.

Uso:
    python scripts/test/test_order_live_sandbox.py            # SPY
    python scripts/test/test_order_live_sandbox.py HD
"""
import asyncio
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from dotenv import load_dotenv

load_dotenv()

PROD_ACCOUNT = "5WI77328"


def _volcar(obj, titulo, sangria="    "):
    """Todos los atributos públicos y no callables. Sin filtrar: buscamos qué HAY."""
    print(f"\n{sangria}{titulo}")
    print(f"{sangria}{'-' * len(titulo)}")
    if obj is None:
        print(f"{sangria}  (None)")
        return
    for a in sorted(dir(obj)):
        if a.startswith("_"):
            continue
        try:
            v = getattr(obj, a)
        except Exception:
            continue
        if callable(v):
            continue
        print(f"{sangria}  {a:<28} = {v!r}")


async def _elegir_spread(ticker):
    """Dos strikes REALES y una expiración REAL de la cadena."""
    from tastytrade import Session
    from tastytrade.account import Account
    from tastytrade.instruments import get_option_chain
    from option_selector import DTE_MIN, DTE_MAX

    session = Session(
        os.getenv("TASTYTRADE_SANDBOX_CLIENT_SECRET"),
        os.getenv("TASTYTRADE_SANDBOX_REFRESH_TOKEN"),
        is_test=True,
    )
    accounts = await Account.get(session)
    account  = accounts[0]

    if account.account_number == PROD_ACCOUNT:
        raise SystemExit(f"⛔ la sesión devolvió {PROD_ACCOUNT} — ES LA REAL. ABORTADO.")

    chain = await get_option_chain(session, ticker)
    hoy   = datetime.date.today()

    exp = None
    for e in sorted(chain):
        if DTE_MIN <= (e - hoy).days <= DTE_MAX:
            exp = e
            break
    if exp is None:
        raise SystemExit(f"No hay expiración entre {DTE_MIN}-{DTE_MAX} DTE para {ticker}")

    calls = sorted([o for o in chain[exp] if o.option_type.value == "C"],
                   key=lambda o: o.strike_price)
    medio = len(calls) // 2
    bajo, alto = calls[medio], calls[medio + 1]

    return (account.account_number, float(bajo.strike_price),
            float(alto.strike_price), exp)


def main(ticker):
    print("\n" + "=" * 62)
    print("  ORDEN REAL EN SANDBOX — plata falsa, orden de verdad")
    print("=" * 62)

    # ── Candado 1 ─────────────────────────────────────────────────────────────
    env = os.getenv("EXECUTOR_ENV", "").strip().lower()
    if env != "sandbox":
        raise SystemExit(
            f"⛔ EXECUTOR_ENV='{env}'. Este script SOLO corre con 'sandbox'. "
            "Abortado."
        )
    print(f"\n  EXECUTOR_ENV = {env}  ✓")

    cuenta, sl, sh, exp = asyncio.run(_elegir_spread(ticker))

    # ── Candado 2 ─────────────────────────────────────────────────────────────
    print(f"  cuenta       = {cuenta}  ✓ (no es {PROD_ACCOUNT})")

    debit = 0.50      # débito pequeño -> límite < $3 -> el sandbox llena al toque
    print(f"\n  LA ORDEN:")
    print(f"    {ticker} Bull Call Spread ${sl:g}/${sh:g}")
    print(f"    expiración : {exp}")
    print(f"    débito     : ${debit:.2f}  (price {-debit:.2f} para el SDK)")
    print(f"    1 contrato · pérdida máx ficticia ${debit*100:.0f}")

    # ── Candado 3 ─────────────────────────────────────────────────────────────
    print(f"\n  Esto MANDA la orden. Escribí 'sandbox' para confirmar.")
    try:
        r = input("  > ").strip()
    except EOFError:
        r = ""
    if r != "sandbox":
        print("\n  Cancelado. No se mandó nada.\n")
        return

    from executor import OpenIntent
    from broker_orders import abrir_spread, client_order_id

    intent = OpenIntent(
        ticker=ticker,
        strike_low=sl,
        strike_high=sh,
        expiration=exp.isoformat(),
        debit=debit,
        rationale="prueba de forma de fill — sandbox",
    )

    print(f"\n  client_order_id: {client_order_id(intent)}")
    print(f"\n  --- mandando ---\n")

    res = abrir_spread(intent)

    print(f"\n  --- resultado ---")
    print(f"    estado     : {res.estado}")
    print(f"    order_id   : {res.order_id}")
    print(f"    fill_price : {res.fill_price}")
    print(f"    detalle    : {res.detalle}")

    # ── LO QUE VENIMOS A VER ─────────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("  FORMA DEL OBJETO — esto alimenta el escritor de DB")
    print("=" * 62)

    _volcar(res.raw, "PlacedOrder")

    patas = getattr(res.raw, "legs", None)
    if patas:
        for i, p in enumerate(patas):
            _volcar(p, f"Leg[{i}]", sangria="      ")
            fills = getattr(p, "fills", None)
            if fills:
                for j, f in enumerate(fills):
                    _volcar(f, f"Leg[{i}].fills[{j}]", sangria="        ")
            else:
                print(f"        Leg[{i}].fills = {fills!r}")

    print("\n" + "=" * 62)
    if res.estado == "filled":
        print("  Llenó. Con esta forma se escribe el writer de `positions`.")
    else:
        print(f"  No llenó ({res.estado}). El volcado de arriba igual sirve:")
        print("  muestra qué devuelve el broker en ese camino.")
    print("=" * 62 + "\n")


if __name__ == "__main__":
    main(sys.argv[1].upper() if len(sys.argv) > 1 else "SPY")