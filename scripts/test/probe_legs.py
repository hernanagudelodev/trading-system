import asyncio, os, sys, datetime, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import httpx
from decimal import Decimal
from dotenv import load_dotenv
from tastytrade import Session
from tastytrade.instruments import NestedOptionChain, Option
from tastytrade.order import OrderAction

load_dotenv()

async def main():
    s = Session(os.getenv("TASTYTRADE_SANDBOX_CLIENT_SECRET"),
                os.getenv("TASTYTRADE_SANDBOX_REFRESH_TOKEN"), is_test=True)
    s._client.timeout = httpx.Timeout(60.0)

    chains = await NestedOptionChain.get(s, "HD")
    ch = chains[0]
    hoy = datetime.date.today()

    exp = next(e for e in ch.expirations if 20 <= (e.expiration_date - hoy).days <= 40)
    print(f"  expiración: {exp.expiration_date}  ({exp.days_to_expiration} DTE)")

    strikes = sorted(exp.strikes, key=lambda x: x.strike_price)
    a, b = strikes[len(strikes)//2], strikes[len(strikes)//2 + 1]
    print(f"  strikes   : {a.strike_price} / {b.strike_price}")
    print(f"  símbolo call de a: {a.call!r}")
    print(f"  atributos de un strike: {[x for x in dir(a) if not x.startswith('_')]}")

    # ¿Option.get acepta una lista?
    t = time.time()
    try:
        opts = await Option.get(s, [a.call, b.call])
        print(f"  Option.get(lista)  {time.time()-t:.1f}s  -> {len(opts)} objetos")
    except Exception as e:
        print(f"  Option.get(lista)  falla: {type(e).__name__}: {e}")
        t = time.time()
        opts = [await Option.get(s, a.call), await Option.get(s, b.call)]
        print(f"  Option.get(uno a uno)  {time.time()-t:.1f}s  -> {len(opts)} objetos")

    leg = opts[0].build_leg(Decimal(1), OrderAction.BUY_TO_OPEN)
    print(f"  build_leg OK: {leg.action.value} {leg.symbol}")

asyncio.run(main())