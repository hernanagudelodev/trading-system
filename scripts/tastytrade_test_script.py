# test rápido — corre esto antes de tocar criteria.py
import os, asyncio
from dotenv import load_dotenv
load_dotenv()

from criteria import get_volatility_from_tastytrade, get_all_criteria

# Test 1: Tastytrade directo
print("=== Tastytrade raw ===")
tt = get_volatility_from_tastytrade("SLB")
print(tt)

print("\n=== Tastytrade MPC ===")
tt2 = get_volatility_from_tastytrade("MPC")
print(tt2)

# Test 2: get_all_criteria
print("\n=== get_all_criteria SLB ===")
import json
c = get_all_criteria("SLB")
if c:
    print(json.dumps({
        "price": c["price"],
        "volatility": c["volatility"],
        "earnings": c["earnings"]
    }, indent=2, default=str))
else:
    print("None")