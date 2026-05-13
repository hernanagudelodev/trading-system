"""
run_backtest.py
===============
Wrapper para Railway — corre backtest una vez y termina limpiamente.
Railway no reinicia workers que terminan con exit code 0.

Cambios vs versión anterior:
    - Lee los 503 tickers del S&P 500 desde sp500_tickers.json
    - Pasa ticker_sector a run_backtest para guardar sector en DB
    - Siempre corre en modo resume para no repetir trabajo ya hecho
"""

import sys
import os

# Asegura que el directorio de scripts esté en el path
sys.path.insert(0, os.path.dirname(__file__))

from backtest import run_backtest, print_summary, load_sp500_tickers

print("Starting backtest on Railway...")

# Cargar los 503 tickers del S&P 500 con sus sectores
ticker_sector = load_sp500_tickers()
tickers       = list(ticker_sector.keys())

print(f"Loaded {len(tickers)} tickers. Starting backtest...")

run_backtest(
    tickers=tickers,
    lookback_days=365,
    resume=True,          # siempre resume para no repetir trabajo
    ticker_sector=ticker_sector
)

print("Backtest complete. Exiting.")
sys.exit(0)