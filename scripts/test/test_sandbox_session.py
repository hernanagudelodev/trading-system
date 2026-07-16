"""
test_sandbox_session.py
=======================
Prueba que la sesión SANDBOX de Tastytrade funciona, ANTES de escribir una sola
línea de LiveExecutor. No manda órdenes. Solo lee.

Qué verifica:
  1. Las credenciales TASTYTRADE_SANDBOX_* existen y autentican
  2. is_test=True apunta a cert (api.cert.tastyworks.com), no a producción
  3. La cuenta sandbox existe y coincide con TASTYTRADE_SANDBOX_ACCOUNT
  4. LA CUENTA NO ES LA REAL — la aserción que impide mandar una orden de verdad
     por un copy-paste. 5WI77328 (real) y 5WV27378 (sandbox) difieren en dos
     caracteres en el medio.
  5. Hay fondos. Una cuenta en cero rechaza todo por margen y parece un bug tuyo.

Sigue el patrón de scripts/test/tastytrade_test_script.py: async, sesión fresca.

Uso:
    python scripts/test/test_sandbox_session.py
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from dotenv import load_dotenv
from tastytrade import Session
from tastytrade.account import Account

load_dotenv()

# La cuenta REAL. Nunca debe aparecer en una sesión sandbox.
PROD_ACCOUNT = "5WI77328"

fallos = []


def check(nombre, ok, detalle=""):
    print(f"  {'✓' if ok else '✗'} {nombre}" + (f"   {detalle}" if detalle and not ok else ""))
    if not ok:
        fallos.append(nombre)


async def main():
    print("\n  === SESIÓN SANDBOX ===\n")

    secret  = os.getenv("TASTYTRADE_SANDBOX_CLIENT_SECRET")
    refresh = os.getenv("TASTYTRADE_SANDBOX_REFRESH_TOKEN")
    esperada = os.getenv("TASTYTRADE_SANDBOX_ACCOUNT")

    check("TASTYTRADE_SANDBOX_CLIENT_SECRET presente", bool(secret))
    check("TASTYTRADE_SANDBOX_REFRESH_TOKEN presente", bool(refresh))
    check("TASTYTRADE_SANDBOX_ACCOUNT presente", bool(esperada))
    if fallos:
        print("\n  Faltan credenciales. Revisá el .env.\n")
        return

    # is_test=True -> api.cert.tastyworks.com. Sin esto, estas credenciales
    # sandbox se mandarían contra producción y fallarían (o peor, no fallarían).
    session = Session(secret, refresh, is_test=True)
    print(f"  Sesión creada (is_test=True)\n")

    accounts = await Account.get(session)
    check("la sesión devuelve al menos una cuenta", len(accounts) > 0)
    if not accounts:
        return

    for a in accounts:
        print(f"    cuenta encontrada: {a.account_number}")

    account = accounts[0]
    num = account.account_number

    # ── La aserción que importa ───────────────────────────────────────────────
    check("la cuenta NO es la de producción", num != PROD_ACCOUNT,
          f"¡{num} ES LA CUENTA REAL! is_test no está haciendo efecto — ABORTAR")

    check(f"la cuenta coincide con el .env ({esperada})", num == esperada,
          f"sesión devolvió {num}, el .env dice {esperada}")

    # ── Balances ──────────────────────────────────────────────────────────────
    bal = await account.get_balances(session)
    nlv = float(bal.net_liquidating_value or 0)
    dbp = float(bal.derivative_buying_power or 0)
    cash = float(bal.cash_balance or 0)

    print(f"\n  BALANCES:")
    print(f"    net_liquidating_value   : ${nlv:,.2f}")
    print(f"    derivative_buying_power : ${dbp:,.2f}")
    print(f"    cash_balance            : ${cash:,.2f}")

    check("la cuenta tiene fondos (NLV > 0)", nlv > 0,
          "cuenta en cero -> todo se rechaza por margen. "
          "Usar POST /sandbox/accounts/{n}/deposits")

    check("hay buying power de derivados", dbp > 0,
          "sin BP no entra ningún spread — revisar que la cuenta sea Margin, no Cash")

    # ── Posiciones (debería estar vacío al arrancar) ───────────────────────────
    positions = await account.get_positions(session)
    print(f"\n  POSICIONES ABIERTAS: {len(positions)}")
    for p in positions:
        print(f"    {p.symbol}  qty={p.quantity}")

    print("\n" + "=" * 60)
    if fallos:
        print(f"  {len(fallos)} FALLO(S): {fallos}")
    else:
        print("  Sandbox operativo. Se puede escribir LiveExecutor.")
        print("  Recordá: el sandbox se resetea cada 24h y limpia balances.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if fallos else 0)