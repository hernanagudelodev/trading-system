import os
from dotenv import load_dotenv
load_dotenv()

from criteria import get_volatility_from_tastytrade

for symbol in ["MPC", "NVDA", "SLB"]:
    print(f"\n{symbol}:")
    tt = get_volatility_from_tastytrade(symbol)
    print(f"  iv:            {tt['iv']}")
    print(f"  iv_percentile: {tt['iv_percentile']}")