import os
import asyncio
from dotenv import load_dotenv
from tastytrade import Session
from tastytrade.account import Account

load_dotenv()

async def main():
    session = Session(
        os.getenv("TASTYTRADE_CLIENT_SECRET"),
        os.getenv("TASTYTRADE_REFRESH_TOKEN")
    )

    accounts = await Account.get(session)
    account = accounts[0]
    print(f"Cuenta: {account.account_number}")

    # Balances
    balances = await account.get_balances(session)
    print(f"\nBalances:")
    print(f"  Atributos: {[a for a in dir(balances) if not a.startswith('_') and not callable(getattr(balances, a))]}")
    print(f"  Raw: {balances}")

    # Posiciones
    positions = await account.get_positions(session)
    print(f"\nPosiciones: {len(positions)}")
    if positions:
        p = positions[0]
        print(f"  Atributos: {[a for a in dir(p) if not a.startswith('_') and not callable(getattr(p, a))]}")
        print(f"  Raw: {p}")

    # Historial reciente
    history = await account.get_history(session)
    print(f"\nHistorial: {len(history)} items")
    if history:
        h = history[0]
        print(f"  Atributos: {[a for a in dir(h) if not a.startswith('_') and not callable(getattr(h, a))]}")
        print(f"  Raw: {h}")

asyncio.run(main())