"""
pricing.py
==========
Fuente ÚNICA de pricing de spreads de 2 patas (Bull Call / Bull Put).
Reemplaza las copias duplicadas que vivían en trade.py y monitor.py, que se
habían desincronizado y causaron bugs (el $0.00 fantasma, el loop roto de quotes).

Combina lo mejor de ambas:
  - loop de quotes con presupuesto total (no abandona por un evento lento)
  - reintentos con sesión y event-loop frescos por intento
  - mid conservador: si a una pata le falta bid o ask, NO se inventa mid → None
    (sin dato real => None, nunca 0.0; evita cierres fantasma)

API pública:
    get_spread_value(ticker, strike_low, strike_high, expiration,
                     option_type='call'|'put', retries=3, delay=2) -> float | None
"""
import os
import time
import asyncio


def _leg_mid(q):
    """Mid de una pata. Si falta bid O ask (0/None), devuelve None (sin dato)."""
    if q is None:
        return None
    bid = float(q.bid_price) if q.bid_price else 0.0
    ask = float(q.ask_price) if q.ask_price else 0.0
    if bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2


async def _fetch_spread_value_async(ticker, strike_low, strike_high,
                                    expiration, option_type):
    from tastytrade import Session, DXLinkStreamer
    from tastytrade.instruments import NestedOptionChain
    from tastytrade.dxfeed import Quote

    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not client_secret or not refresh_token:
        return None

    # Sesión fresca por intento (evita 'Event loop is closed' / conexión obsoleta)
    session = Session(client_secret, refresh_token)

    chains = await NestedOptionChain.get(session, ticker)
    if not chains:
        return None
    chain = chains[0]

    target_exp = None
    for exp in chain.expirations:
        if exp.expiration_date == expiration:
            target_exp = exp
            break
    if target_exp is None:
        return None

    long_obj = short_obj = None
    for s in target_exp.strikes:
        sp = float(s.strike_price)
        if abs(sp - strike_low) < 0.01:
            long_obj = s
        elif abs(sp - strike_high) < 0.01:
            short_obj = s
    if not long_obj or not short_obj:
        return None

    if option_type == "put":
        long_sym  = long_obj.put_streamer_symbol
        short_sym = short_obj.put_streamer_symbol
    else:
        long_sym  = long_obj.call_streamer_symbol
        short_sym = short_obj.call_streamer_symbol

    symbols    = [long_sym, short_sym]
    quotes_map = {}

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, symbols)
        # Recolecta AMBOS quotes o agota un presupuesto total; no abandona por
        # un solo evento lento (clave en alta volatilidad).
        loop     = asyncio.get_running_loop()
        deadline = loop.time() + 12
        while len(quotes_map) < len(symbols):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                q = await asyncio.wait_for(streamer.get_event(Quote), timeout=remaining)
                quotes_map[q.event_symbol] = q
            except asyncio.TimeoutError:
                break

    long_mid  = _leg_mid(quotes_map.get(long_sym))
    short_mid = _leg_mid(quotes_map.get(short_sym))
    if long_mid is None or short_mid is None:
        return None          # sin dato confiable de dos lados => None

    if option_type == "put":
        spread_val = round(short_mid - long_mid, 2)   # Bull Put: short - long
    else:
        spread_val = round(long_mid - short_mid, 2)   # Bull Call: long - short

    # Un spread con 20-40 DTE nunca vale $0.00. <= 0 => sin dato real => None.
    return spread_val if spread_val > 0 else None


def get_spread_value(ticker, strike_low, strike_high, expiration,
                     option_type="call", retries=3, delay=2):
    """
    Wrapper síncrono con reintentos. Cada intento = event loop + sesión frescos.
    Devuelve el valor del spread por acción (float > 0), o None si falla.
    """
    for attempt in range(retries):
        try:
            val = asyncio.run(_fetch_spread_value_async(
                ticker, strike_low, strike_high, expiration, option_type))
            if val is not None and val > 0:
                return val
        except Exception as e:
            print(f"  pricing error ({ticker}): {e}")
        if attempt < retries - 1:
            time.sleep(delay)
    return None