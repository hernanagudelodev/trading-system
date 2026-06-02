import os
from dotenv import load_dotenv
load_dotenv()
import sys
sys.path.insert(0, "scripts")
from monitor import get_spread_value_tastytrade
from datetime import date

val = get_spread_value_tastytrade("XYZ", 75.0, 79.0, date(2026, 7, 2))
print(f"Spread value devuelto: {val}")