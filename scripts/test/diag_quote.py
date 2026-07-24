"""
diag_quote.py
=============
DIAGNÓSTICO: por qué _fetch_spread_quote_async devuelve None en vivo cuando el
scanner SÍ trae quotes para los mismos tickers (21-jul: WRB y VEEV, 6/6 sin dato).

NO modifica pricing.py. Reimplementa la función paso a paso con prints en cada
punto donde el original hace `return None`, para ver EXACTAMENTE dónde muere.

Prueba los dos casos reales del fallo:
    WRB  72/77  call  (Bull Call Spread)
    VEEV 170/175 put  (Bull Put Spread)

REQUISITOS
    - Mercado ABIERTO (9:30-16:00 ET). Con mercado cerrado no hay quotes y esto
      "confirma" un falso positivo.
    - Credenciales de PRODUCCIÓN (TASTYTRADE_CLIENT_SECRET / _REFRESH_TOKEN) en
      el entorno — las MISMAS que usa el scanner y el quote real.

Uso:
    python scripts/test/diag_quote.py
"""
import asyncio
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from dotenv import load_dotenv
load_dotenv()

# expiración real que usó el run que falló
EXP = "2026-08-21"

CASOS = [
    {"ticker": "WRB",  "sl": 72.0,  "sh": 77.0,  "opt": "call"},
    {"ticker": "VEEV", "sl": 170.0, "sh": 175.0, "opt": "put"},
]


def _hora_et():
    from zoneinfo import ZoneInfo
    now = datetime.datetime.now(datetime.timezone.utc).astimezone(
        ZoneInfo("America/New_York"))
    abierto = (now.weekday() < 5 and
               (now.hour, now.minute) >= (9, 30) and now.hour < 16)
    return now, abierto


async def _diagnosticar(ticker, strike_low, strike_high, expiration, option_type):
    """
    Copia instrumentada de pricing._fetch_spread_quote_async. Cada return None
    del original acá imprime POR QUÉ y devuelve un código, no un None mudo.
    """
    from tastytrade import Session, DXLinkStreamer
    from tastytrade.instruments import NestedOptionChain
    from tastytrade.dxfeed import Quote

    print(f"\n{'=' * 60}")
    print(f"  {ticker} {strike_low}/{strike_high} {option_type} · exp {expiration}")
    print(f"{'=' * 60}")

    cs = os.getenv("TASTYTRADE_CLIENT_SECRET")
    rt = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    print(f"  credenciales: client_secret={'SÍ' if cs else 'FALTA'} · "
          f"refresh_token={'SÍ' if rt else 'FALTA'}")
    if not cs or not rt:
        print("  ⛔ MUERE EN: credenciales ausentes (return None #1)")
        return

    t0 = asyncio.get_running_loop().time()
    session = Session(cs, rt)
    print(f"  ✓ sesión creada ({asyncio.get_running_loop().time() - t0:.1f}s)")

    chains = await NestedOptionChain.get(session, ticker)
    if not chains:
        print("  ⛔ MUERE EN: cadena vacía (return None #2)")
        return
    chain = chains[0]
    print(f"  ✓ cadena obtenida · {len(chain.expirations)} expiraciones")

    # FIX: la expiración llega como string ISO; exp.expiration_date es date.
    # date == str es SIEMPRE False. Se normaliza a date antes de comparar.
    target = datetime.date.fromisoformat(str(expiration))
    target_exp = None
    for exp in chain.expirations:
        if exp.expiration_date == target:
            target_exp = exp
            break
    if target_exp is None:
        disponibles = sorted(str(e.expiration_date) for e in chain.expirations)[:8]
        print(f"  ⛔ MUERE EN: expiración {expiration} NO está en la cadena "
              f"(return None #3)")
        print(f"      primeras disponibles: {disponibles}")
        return
    print(f"  ✓ expiración encontrada (match date==date) · "
          f"tipo={type(target_exp.expiration_date).__name__}")

    long_obj = short_obj = None
    for s in target_exp.strikes:
        sp = float(s.strike_price)
        if abs(sp - strike_low) < 0.01:
            long_obj = s
        elif abs(sp - strike_high) < 0.01:
            short_obj = s
    if not long_obj or not short_obj:
        strikes_cerca = sorted(float(s.strike_price) for s in target_exp.strikes
                               if abs(float(s.strike_price) - strike_low) < 20)
        print(f"  ⛔ MUERE EN: strike no encontrado (return None #4)")
        print(f"      long ({strike_low}): {'OK' if long_obj else 'FALTA'} · "
              f"short ({strike_high}): {'OK' if short_obj else 'FALTA'}")
        print(f"      strikes cerca: {strikes_cerca}")
        return
    print(f"  ✓ ambos strikes resueltos")

    if option_type == "put":
        long_sym  = long_obj.put_streamer_symbol
        short_sym = short_obj.put_streamer_symbol
    else:
        long_sym  = long_obj.call_streamer_symbol
        short_sym = short_obj.call_streamer_symbol
    print(f"      long_sym  = {long_sym}")
    print(f"      short_sym = {short_sym}")
    if not long_sym or not short_sym:
        print(f"  ⛔ MUERE EN: streamer_symbol vacío — símbolo no existe para "
              f"este tipo de opción")
        return

    symbols    = [long_sym, short_sym]
    quotes_map = {}

    t_stream = asyncio.get_running_loop().time()
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, symbols)
        print(f"  ✓ suscrito a DXLink · esperando quotes (deadline 12s)...")
        loop     = asyncio.get_running_loop()
        deadline = loop.time() + 12
        while len(quotes_map) < len(symbols):
            remaining = deadline - loop.time()
            if remaining <= 0:
                print(f"  ⛔ TIMEOUT del streamer tras "
                      f"{loop.time() - t_stream:.1f}s · "
                      f"llegaron {len(quotes_map)}/{len(symbols)} quotes")
                break
            try:
                q = await asyncio.wait_for(streamer.get_event(Quote),
                                           timeout=remaining)
                quotes_map[q.event_symbol] = q
                print(f"      quote recibido: {q.event_symbol} · "
                      f"bid={q.bid_price} ask={q.ask_price}")
            except asyncio.TimeoutError:
                print(f"  ⛔ wait_for expiró esperando un evento Quote")
                break

    print(f"  quotes recolectados: {len(quotes_map)}/{len(symbols)}")

    # _leg_quote: exige bid>0 Y ask>0
    def _leg(sym):
        q = quotes_map.get(sym)
        if q is None:
            print(f"      {sym}: NO llegó quote")
            return None
        bid = float(q.bid_price) if q.bid_price else 0.0
        ask = float(q.ask_price) if q.ask_price else 0.0
        if bid <= 0 or ask <= 0:
            print(f"      {sym}: bid={bid} ask={ask} — uno es <=0, "
                  f"_leg_quote devuelve None")
            return None
        return (bid, ask, (bid + ask) / 2)

    long_q  = _leg(long_sym)
    short_q = _leg(short_sym)
    if long_q is None or short_q is None:
        print(f"  ⛔ MUERE EN: _leg_quote None en al menos una pata "
              f"(return None #5) — ESTA es la causa más probable del 6/6")
        return

    lbid, lask, lmid = long_q
    sbid, sask, smid = short_q
    if option_type == "put":
        spread_mid = round(smid - lmid, 2)
    else:
        spread_mid = round(lmid - smid, 2)

    if spread_mid <= 0:
        print(f"  ⛔ MUERE EN: spread_mid={spread_mid} <= 0 (return None #6)")
        return

    print(f"\n  ✓✓✓ QUOTE OK · mid={spread_mid} — la función SÍ funciona aislada")
    print(f"      si aislada funciona pero en auto_run falla, el problema es "
          f"CONCURRENCIA con el streamer del scanner")


async def _main():
    now, abierto = _hora_et()
    print(f"\n  Hora ET: {now.strftime('%Y-%m-%d %H:%M')} · "
          f"mercado {'ABIERTO ✓' if abierto else 'CERRADO ⛔'}")
    if not abierto:
        print(f"  ⚠️  MERCADO CERRADO. Sin quotes, este diagnóstico da falso "
              f"negativo — el paso del streamer va a fallar por no haber feed,")
        print(f"  no por bug. PERO el paso de la expiración (el bug de ayer) SÍ "
              f"se valida igual: si WRB/VEEV pasan el '✓ expiración encontrada',")
        print(f"  el fix de tipo funciona. Corré con mercado abierto para el "
              f"veredicto completo (✓✓✓ QUOTE OK).")
        print(f"  Horario: 9:30-16:00 ET (8:30-15:00 Colombia).\n")

    for c in CASOS:
        try:
            await _diagnosticar(c["ticker"], c["sl"], c["sh"], EXP, c["opt"])
        except Exception as e:
            import traceback
            print(f"  ⛔ EXCEPCIÓN en {c['ticker']}: {type(e).__name__}: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(_main())