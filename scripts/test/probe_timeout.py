import asyncio, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import httpx
from dotenv import load_dotenv
from tastytrade import Session
from tastytrade.account import Account
from tastytrade.instruments import NestedOptionChain

load_dotenv()

async def main():
    t = time.time()
    s = Session(os.getenv("TASTYTRADE_SANDBOX_CLIENT_SECRET"),
                os.getenv("TASTYTRADE_SANDBOX_REFRESH_TOKEN"),
                is_test=True)
    print(f"  Session()            {time.time()-t:5.1f}s")
    print(f"  timeout por default  {s._client.timeout}")

    s._client.timeout = httpx.Timeout(60.0)
    print(f"  timeout subido a     {s._client.timeout}")

    for nombre, coro in [
        ("Account.get",        lambda: Account.get(s)),
        ("NestedOptionChain HD", lambda: NestedOptionChain.get(s, "HD")),
    ]:
        t = time.time()
        try:
            r = await coro()
            print(f"  {nombre:22} {time.time()-t:5.1f}s   OK")
        except Exception as e:
            print(f"  {nombre:22} {time.time()-t:5.1f}s   {type(e).__name__}")

asyncio.run(main())