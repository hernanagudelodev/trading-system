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
import datetime


def _leg_quote(q):
    """(bid, ask, mid) de una pata. Si falta bid O ask (0/None), devuelve None."""
    if q is None:
        return None
    bid = float(q.bid_price) if q.bid_price else 0.0
    ask = float(q.ask_price) if q.ask_price else 0.0
    if bid <= 0 or ask <= 0:
        return None
    return (bid, ask, (bid + ask) / 2)


def _leg_mid(q):
    """Solo el mid de una pata, o None. Envuelve a _leg_quote."""
    lq = _leg_quote(q)
    return lq[2] if lq else None


async def _fetch_spread_quote_async(ticker, strike_low, strike_high,
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

    # La expiración llega como STRING ISO ('2026-08-21') desde intent.expiration,
    # pero exp.expiration_date del SDK es un datetime.date. `date == str` es
    # SIEMPRE False aunque impriman igual — por eso el 21-jul fallaba 6/6 con la
    # expiración correcta en la cadena. Se normaliza a date antes de comparar
    # (mismo patrón que broker_orders._resolve_legs). Acepta str o date de
    # entrada: get_spread_value (cierre) pasa un date y sigue funcionando.
    target = datetime.date.fromisoformat(str(expiration))
    target_exp = None
    for exp in chain.expirations:
        if exp.expiration_date == target:
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

    long_q  = _leg_quote(quotes_map.get(long_sym))
    short_q = _leg_quote(quotes_map.get(short_sym))
    if long_q is None or short_q is None:
        return None          # sin dato confiable de dos lados => None

    lbid, lask, lmid = long_q
    sbid, sask, smid = short_q

    if option_type == "put":
        spread_mid = round(smid - lmid, 2)            # Bull Put: short - long
        # bid del spread = lo peor al ejecutar; ask = lo mejor. Extremos cruzados.
        spread_bid = round(sbid - lask, 2)
        spread_ask = round(sask - lbid, 2)
    else:
        spread_mid = round(lmid - smid, 2)            # Bull Call: long - short
        spread_bid = round(lbid - sask, 2)
        spread_ask = round(lask - sbid, 2)

    # Un spread con 20-40 DTE nunca vale $0.00. <= 0 => sin dato real => None.
    if spread_mid <= 0:
        return None
    return {"mid": spread_mid, "bid": spread_bid, "ask": spread_ask}


def get_spread_quote(ticker, strike_low, strike_high, expiration,
                     option_type="call", retries=3, delay=2):
    """
    Devuelve {"mid","bid","ask"} del spread (floats), o None si falla.

    bid = lo que te pagarían / lo que cuesta al peor precio de ejecución;
    ask = el otro extremo. La brecha ask-bid dice por qué una orden al mid no
    llena: si el ask está lejos del mid, ceder de a centavos no cruza el spread.
    Fuente ÚNICA del quote del spread. get_spread_value delega acá para el mid.
    """
    for attempt in range(retries):
        try:
            q = asyncio.run(_fetch_spread_quote_async(
                ticker, strike_low, strike_high, expiration, option_type))
            if q is not None and q["mid"] > 0:
                return q
        except Exception as e:
            print(f"  pricing error ({ticker}): {e}")
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def get_spread_value(ticker, strike_low, strike_high, expiration,
                     option_type="call", retries=3, delay=2):
    """
    Mid del spread por acción (float > 0), o None. Envuelve get_spread_quote —
    la firma y el contrato NO cambian: los ~5 llamadores siguen igual.
    """
    q = get_spread_quote(ticker, strike_low, strike_high, expiration,
                         option_type, retries, delay)
    return q["mid"] if q else None