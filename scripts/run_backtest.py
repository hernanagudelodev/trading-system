"""
Wrapper para Railway — corre backtest una vez y termina limpiamente.
Railway no reinicia workers que terminan con exit code 0.
"""
import sys
import os

# Asegura que el directorio de scripts esté en el path
sys.path.insert(0, os.path.dirname(__file__))

from backtest import run_backtest, print_summary, DEFAULT_TICKERS

print("Starting backtest on Railway...")
run_backtest(
    tickers=DEFAULT_TICKERS,
    lookback_days=365,
    resume=True  # siempre resume para no repetir trabajo
)
print("Backtest complete. Exiting.")
sys.exit(0)