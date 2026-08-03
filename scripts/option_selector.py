"""
option_selector.py
==================
Selects optimal option structures for tickers that passed hard filters.

Strategy selection is driven by criteria.py → select_strategy():
    IV < 30%   → Long Call (high Beta + momentum) or Bull Call Spread
    30-60%     → Bull Call Spread
    IV >= 60%  → Bull Put Spread (sell premium, time works FOR us)

For each ticker:
    1. Fetches option chain from Tastytrade API
    2. Finds best expiration in DTE range (20-40 days)
    3. Captures Greeks + Quotes for all candidate strikes
    4. Builds strategy-appropriate candidates
    5. Returns compact markdown for AI interpretation

Called by scanner.py after passes_hard_filters().
"""

import asyncio
import os
from datetime import date, datetime


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DTE_MIN = 20
DTE_MAX = 40

# Long Call / Bull Call Spread — call side
DELTA_MIN        = 0.35
DELTA_MAX        = 0.65
DELTA_IDEAL_LOW  = 0.40
DELTA_IDEAL_HIGH = 0.60

SPREAD_LONG_DELTA_TARGET  = 0.60
SPREAD_SHORT_DELTA_TARGET = 0.30
SPREAD_LONG_DELTA_RANGE   = (0.50, 0.70)
SPREAD_SHORT_DELTA_RANGE  = (0.20, 0.40)

# Bull Put Spread — put side
# Short put: slightly OTM (Delta 0.25-0.45, absolute value)
# Long put: further OTM (Delta 0.10-0.25, absolute value) — protection
PUT_SHORT_DELTA_RANGE = (0.25, 0.45)   # sell this put
PUT_LONG_DELTA_RANGE  = (0.10, 0.25)   # buy this put (lower strike)

MAX_LONG_CALLS = 4
MAX_SPREADS    = 4

# ── Gates de riesgo / calidad — RECHAZAN, no solo advierten ───────────────────
# Pérdida máxima por trade ≤ X% del capital.
#
# EL CAPITAL SALE DEL BROKER, NO DE UNA CONSTANTE (3-ago).
#   `account_snapshots` ya guarda net_liquidating_value con timestamp, poblada
#   por run_sync. En vez de un ACCOUNT_NLV fijo (que envejecía: el default 14100
#   quedó $300 por debajo del capital real ~14400), se lee la última fila cada
#   vez. SIN caché: el proceso de Railway vive entre runs, así que un valor
#   cacheado en el run de la mañana quedaría pegado toda la tarde — justo el bug
#   que esto elimina. La consulta es trivial (tabla chica, LIMIT 1).
#   Fallback: si la tabla está vacía o la lectura falla, ACCOUNT_NLV env (14100).
#
# MAX_RISK_PCT: por env var, default 0.03 (3%), conservador a propósito. Valida
#   el rango: un typo (0.3 = 30% por trade) se rechaza y cae al default.
_ACCOUNT_NLV_FALLBACK = float(os.getenv("ACCOUNT_NLV", "14100"))


def get_account_nlv():
    """
    Net Liquidating Value REAL, de la última fila de account_snapshots.

    Sin caché — se lee fresco en cada llamada (el NLV cambia dentro de la vida
    del proceso). Si la tabla está vacía o la lectura falla, cae al fallback
    (env ACCOUNT_NLV, default 14100): un capital conocido es mejor que abortar,
    y si el broker está caído la última fila SIGUE siendo el mejor dato que hay.
    Loguea la fecha del snapshot para que un valor viejo sea visible.
    """
    try:
        import psycopg2
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur = conn.cursor()
        cur.execute("""
            SELECT net_liquidating_value, snapshot_at
            FROM account_snapshots
            ORDER BY snapshot_at DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] is not None:
            nlv = float(row[0])
            print(f"  [capital] NLV ${nlv:,.2f} (snapshot {row[1]})")
            return nlv
        print(f"  ⚠️  [capital] account_snapshots vacía — "
              f"usando fallback ${_ACCOUNT_NLV_FALLBACK:,.2f}")
        return _ACCOUNT_NLV_FALLBACK
    except Exception as e:
        print(f"  ⚠️  [capital] no se pudo leer account_snapshots ({e}) — "
              f"usando fallback ${_ACCOUNT_NLV_FALLBACK:,.2f}")
        return _ACCOUNT_NLV_FALLBACK


def _load_max_risk_pct():
    raw = os.getenv("MAX_RISK_PCT", "0.03")
    try:
        pct = float(raw)
    except ValueError:
        print(f"  ⚠️  MAX_RISK_PCT='{raw}' no es número — usando 0.03 (3%).")
        return 0.03
    if not (0.0 < pct <= 0.20):
        print(f"  ⚠️  MAX_RISK_PCT={pct} fuera de rango (0, 0.20] — "
              f"¿un typo? usando 0.03 (3%).")
        return 0.03
    return pct


MAX_RISK_PCT = _load_max_risk_pct()           # fracción del capital por trade


def max_risk_dollars():
    """Pérdida máxima por trade en dólares = NLV real × MAX_RISK_PCT. Fresco."""
    return get_account_nlv() * MAX_RISK_PCT


MIN_RR_DEBIT     = 1.0                         # Bull Call Spread / Long Call: R/R mínimo
MIN_POP_CREDIT   = 60                          # Bull Put Spread: POP mínimo (%)
MIN_SPREAD_WIDTH = 3                           # ancho mínimo ($): evita spreads de $1-2
                                               # donde el stop de -65% salta a -130% entre chequeos

# ── Gates de LIQUIDEZ — la causa raíz del 23-jul ──────────────────────────────
# PAYX $100/$105 se abrió con un libro que, medido, daba bid 0.15 / ask 2.55
# sobre un ancho de $5: el valor del spread estaba en cualquier punto de una
# banda de $2.40. Con esa dispersión el mid no es un precio, es un promedio de
# dos números que nadie va a pagar. El stop de -65% lo cruzó por ruido y el
# sistema cerró una posición sana pagando 2.02 por algo que valía ~1.18.
#
# Ninguna mejora de pricing arregla eso: el problema es el INSTRUMENTO, no el
# canal (verificado el 24-jul: REST y DXLink coinciden al centavo). El filtro
# tiene que estar al ABRIR.
#
# OI >= 200 (criteria.py) NO lo detecta: mide contratos vivos, no si hay
# alguien cotizando hoy. PAYX tenía tamaños de 415/488 con el libro roto.
MAX_SPREAD_BID_ASK_PCT   = 0.20   # (ask-bid) del spread / ancho de strikes
MAX_MID_MARK_DIVERGENCE  = 0.10   # |mid - mark| / mark, del spread

# Medido contra datos reales:
#                    horquilla/ancho   mid vs mark
#   PAYX  23-jul          48%              16%      -> rechazado por los DOS
#   WRB   22-jul          21%              n/d      -> rechazado (ya lo habías
#                                                      marcado a mano: OI 519)
#   JNJ   24-jul         5.4%               0%      -> pasa


# ══════════════════════════════════════════════════════════════════════════════
# EXPIRACIÓN REAL — fuente única de verdad para la fecha (el LLM NO la elige)
# ══════════════════════════════════════════════════════════════════════════════

def get_real_expiration(ticker):
    """
    Devuelve la fecha de expiración REAL de la cadena: la misma regla que usa
    el spread builder (más cercana a 30 DTE dentro de 20-40). Crea sesión fresca.
    Devuelve un datetime.date, o None si no se pudo obtener.
    """
    try:
        return asyncio.run(_get_real_expiration_async(ticker))
    except Exception as e:
        print(f"  get_real_expiration error for {ticker}: {e}")
        return None


async def _get_real_expiration_async(ticker):
    from tastytrade import Session
    from tastytrade.instruments import NestedOptionChain

    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not client_secret or not refresh_token:
        return None

    session = Session(client_secret, refresh_token)
    chains  = await NestedOptionChain.get(session, ticker)
    if not chains:
        return None
    chain = chains[0]

    target_exp = None
    best_diff  = 9999
    for exp in chain.expirations:
        dte = exp.days_to_expiration
        if DTE_MIN <= dte <= DTE_MAX:
            diff = abs(dte - 30)
            if diff < best_diff:
                best_diff  = diff
                target_exp = exp

    return target_exp.expiration_date if target_exp else None


# ══════════════════════════════════════════════════════════════════════════════
# ASYNC CORE — fetch option chain + Greeks for one ticker
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_option_data(session, ticker, price, strategy):
    """
    Fetch option chain with real-time Greeks for a single ticker.
    strategy: 'Long Call' | 'Bull Call Spread' | 'Bull Put Spread'
    """
    from tastytrade.instruments import NestedOptionChain
    from tastytrade.dxfeed import Greeks
    from tastytrade import DXLinkStreamer

    empty = {"long_calls": [], "spreads": [], "put_spreads": [],
             "exp_date": None, "dte": None, "strategy": strategy}

    try:
        chains = await NestedOptionChain.get(session, ticker)
        if not chains:
            return empty
        chain = chains[0]

        # Best expiration in DTE range (closest to 30d)
        target_exp = None
        best_diff  = 9999
        for exp in chain.expirations:
            dte = exp.days_to_expiration
            if DTE_MIN <= dte <= DTE_MAX:
                diff = abs(dte - 30)
                if diff < best_diff:
                    best_diff  = diff
                    target_exp = exp

        if target_exp is None:
            return empty

        dte_selected = target_exp.days_to_expiration
        exp_date     = target_exp.expiration_date

        # Candidate strikes within ±25% of price
        price_low  = price * 0.75
        price_high = price * 1.25
        candidate_strikes = [
            s for s in target_exp.strikes
            if price_low <= float(s.strike_price) <= price_high
        ]
        if not candidate_strikes:
            return empty

        # ── SÍMBOLOS ──────────────────────────────────────────────────────────
        # streamer  -> Greeks (delta/theta/IV). Sólo DXLink los tiene.
        # OCC       -> precios por REST. Vienen en el mismo objeto Strike
        #              (s.call / s.put), sin ninguna búsqueda extra.
        es_put = strategy == "Bull Put Spread"
        if es_put:
            symbols     = [s.put_streamer_symbol for s in candidate_strikes]
            occ_symbols = [s.put for s in candidate_strikes]
        else:
            symbols     = [s.call_streamer_symbol for s in candidate_strikes]
            occ_symbols = [s.call for s in candidate_strikes]

        # ── GREEKS · por streamer ─────────────────────────────────────────────
        greeks_map = {}
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Greeks, symbols)
            for _ in symbols:
                try:
                    g = await asyncio.wait_for(streamer.get_event(Greeks), timeout=10)
                    greeks_map[g.event_symbol] = g
                except asyncio.TimeoutError:
                    break

        # ── PRECIOS · por REST, en UNA llamada para todos los strikes ─────────
        # POR QUÉ REST Y NO LA SUSCRIPCIÓN A Quote QUE HABÍA ACÁ
        #   DXLink publica eventos "as they occur" y la doc de tastytrade avisa
        #   que en símbolos de baja liquidez puede no haber ninguno por minutos.
        #   El bucle anterior tomaba el PRIMER evento de cada símbolo y cortaba:
        #   para una consulta puntual eso es tomar lo primero que aparezca sin
        #   saber de cuándo es. El endpoint de market data devuelve un snapshot
        #   con bid, ask, mid, MARK y updated_at, y acepta una lista.
        #
        #   Se saca la suscripción a Quote en vez de dejar las dos: dos fuentes
        #   del mismo precio son dos fuentes que pueden divergir, y este proyecto
        #   ya pagó ese patrón con el P&L y con el pricing de spreads.
        #
        #   NOTA: el mark se usa SÓLO para el gate. El precio de referencia de la
        #   estructura sigue siendo el mid, porque los fills reales llegan cerca
        #   del mid (JNJ llenó 0.59 con mark 0.595; PAYX abrió a 1.20). El mark
        #   es mejor para VALORAR, el mid para EJECUTAR.
        from tastytrade.market_data import get_market_data_by_type
        md_map = {}
        try:
            datos  = await get_market_data_by_type(session, options=occ_symbols)
            md_map = {d.symbol: d for d in datos}
        except Exception as e:
            # Fail-closed y RUIDOSO: sin precios no se construye nada para este
            # ticker. Un ticker que desaparece del scanner en silencio es
            # exactamente el fallo que este proyecto persigue.
            print(f"  ⛔ {ticker}: no se pudieron traer precios "
                  f"({type(e).__name__}: {e}) — sin estructuras este run")
            return empty

        # ── TABLA UNIFICADA ───────────────────────────────────────────────────
        strike_table = []
        sin_precio   = 0
        for s in candidate_strikes:
            sym = s.put_streamer_symbol if es_put else s.call_streamer_symbol
            occ = s.put if es_put else s.call
            g   = greeks_map.get(sym)
            md  = md_map.get(occ)
            if g is None or g.delta is None:
                continue
            if md is None:
                sin_precio += 1
                continue

            bid  = float(md.bid)  if md.bid  is not None else None
            ask  = float(md.ask)  if md.ask  is not None else None
            mid  = float(md.mid)  if md.mid  is not None else None
            mark = float(md.mark) if md.mark is not None else None

            # SIN DATO -> se descarta el strike. NO se inventa 0.0 ni se cae al
            # precio teórico. La versión anterior hacía:
            #     mid = (bid+ask)/2 if (bid and ask) else (theo or 0.0)
            # o sea que una pata sin libro entraba valiendo CERO a los gates de
            # riesgo. Un cero que parece precio, alimentando MAX_RISK_DOLLARS.
            if bid is None or ask is None or mid is None or mark is None or ask <= 0:
                sin_precio += 1
                continue

            if getattr(md, "trading_halted", False):
                sin_precio += 1
                continue

            strike_table.append({
                "strike":     float(s.strike_price),
                "delta":      float(g.delta),
                "theta":      float(g.theta) if g.theta else None,
                "iv":         float(g.volatility) * 100 if g.volatility else None,
                "bid":        bid,
                "ask":        ask,
                "mid":        mid,
                "mark":       mark,
                "spread_pct": round((ask - bid) / ask * 100, 1) if ask > 0 else None,
                "updated_at": getattr(md, "updated_at", None),
            })

        if sin_precio:
            print(f"  {ticker}: {sin_precio} strike(s) descartado(s) sin precio usable")

        strike_table.sort(key=lambda x: x["strike"])

        # Build strategy-specific candidates
        long_calls  = []
        spreads     = []
        put_spreads = []

        if strategy == "Long Call":
            long_calls = _build_long_calls(strike_table, price)
        elif strategy == "Bull Call Spread":
            long_calls = _build_long_calls(strike_table, price)
            spreads    = _build_call_spreads(strike_table, price, ticker)
        elif strategy == "Bull Put Spread":
            put_spreads = _build_put_spreads(strike_table, price, ticker)

        return {
            "long_calls":  long_calls,
            "spreads":     spreads,
            "put_spreads": put_spreads,
            "exp_date":    exp_date,
            "dte":         dte_selected,
            "strategy":    strategy,
        }

    except Exception as e:
        print(f"  option_selector error for {ticker}: {e}")
        return empty


# ══════════════════════════════════════════════════════════════════════════════
# GATE DE LIQUIDEZ — fuente única para los dos builders de spreads
# ══════════════════════════════════════════════════════════════════════════════

def spread_liquidity(long_leg, short_leg, width, is_credit):
    """
    Mide si el libro de un spread vertical es LEGIBLE.

    Devuelve (ok: bool, info: dict). `info` sirve para el log y para el markdown:
        bid_ask      horquilla del spread en dólares
        bid_ask_pct  esa horquilla sobre el ancho de strikes
        mid          valor del spread por mid  de cada pata
        mark         valor del spread por mark de cada pata
        divergence   |mid - mark| / mark
        motivo       None si pasa; el texto del rechazo si no

    POR QUÉ LA HORQUILLA ES LA SUMA DE LAS PATAS
        spread_ask - spread_bid = (lask-lbid) + (sask-sbid), tanto para débito
        como para crédito — los términos cruzados se cancelan. Verificado contra
        PAYX (1.00 + 1.40 = 2.40, y el spread daba 0.15/2.55) y contra JNJ
        (0.11 + 0.16 = 0.27, y el spread daba 0.46/0.73). Por eso no hace falta
        ramificar por dirección acá.

    POR QUÉ DOS CRITERIOS Y NO UNO
        Son señales independientes. La horquilla dice si el libro es legible;
        la divergencia mid/mark dice si el broker está de acuerdo con nuestro
        número. PAYX fallaba las dos; un instrumento puede fallar una sola.

    POR QUÉ NO SE MIRAN LOS TAMAÑOS
        PAYX tenía bid_size 415 / ask_size 488 con el libro roto, y JNJ tenía 1/1
        con el libro sano. No discriminan.
    """
    lbid, lask = long_leg["bid"],  long_leg["ask"]
    sbid, sask = short_leg["bid"], short_leg["ask"]

    bid_ask     = round((lask - lbid) + (sask - sbid), 4)
    bid_ask_pct = bid_ask / width if width else None

    if is_credit:
        mid  = round(short_leg["mid"]  - long_leg["mid"],  4)
        mark = round(short_leg["mark"] - long_leg["mark"], 4)
    else:
        mid  = round(long_leg["mid"]  - short_leg["mid"],  4)
        mark = round(long_leg["mark"] - short_leg["mark"], 4)

    divergence = abs(mid - mark) / abs(mark) if mark else None

    info = {"bid_ask": bid_ask, "bid_ask_pct": bid_ask_pct,
            "mid": mid, "mark": mark, "divergence": divergence,
            "motivo": None}

    if bid_ask_pct is None or bid_ask_pct > MAX_SPREAD_BID_ASK_PCT:
        info["motivo"] = (f"horquilla ${bid_ask:.2f} = "
                          f"{(bid_ask_pct or 0)*100:.0f}% del ancho "
                          f"(máx {MAX_SPREAD_BID_ASK_PCT*100:.0f}%)")
        return False, info

    # mark ausente o cero: no se puede contrastar. No se asume que está bien.
    if divergence is None:
        info["motivo"] = "sin mark del broker para contrastar el mid"
        return False, info

    if divergence > MAX_MID_MARK_DIVERGENCE:
        info["motivo"] = (f"mid {mid:.2f} vs mark {mark:.2f} = "
                          f"{divergence*100:.0f}% de divergencia "
                          f"(máx {MAX_MID_MARK_DIVERGENCE*100:.0f}%)")
        return False, info

    return True, info


def _report_descartes(ticker, estrategia, descartes, sobrevivientes):
    """
    Imprime cuántas estructuras cortó el gate de liquidez y por qué.

    POR QUÉ EXISTE
        Un gate que rechaza en silencio no se puede auditar: "no rechazó nada"
        y "no está corriendo" se ven exactamente igual en el log. criteria.py ya
        imprime el motivo de cada eliminación ("✗ Below SMA200 — ..."); este
        gate nace mudo y hay que emparejarlo.

        Importa especialmente cuando un ticker se queda en CERO estructuras: sin
        esta línea, desaparece del scanner sin explicación y parece que el
        mercado no ofrecía nada. Es el mismo fallo silencioso que el bug raíz de
        junio, donde la falta de datos se leyó como "no hay oportunidades".

    Se muestra un ejemplo, no la lista entera: el bucle prueba todas las
    combinaciones long × short, así que una sola pata con el libro roto genera
    muchos rechazos. El número dice cuánto se cortó; el ejemplo, por qué.
    """
    if not descartes:
        return
    marca = " · CERO estructuras" if sobrevivientes == 0 else ""
    print(f"    {ticker} [{estrategia}]: {len(descartes)} estructura(s) "
          f"descartada(s) por liquidez{marca}")
    print(f"      ej: {descartes[0]}")


# ══════════════════════════════════════════════════════════════════════════════
# LONG CALL BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_long_calls(strike_table, price):
    results = []
    max_risk = max_risk_dollars()          # NLV real × pct, una vez por build
    for s in strike_table:
        delta = s["delta"]
        if not (DELTA_MIN <= delta <= DELTA_MAX):
            continue

        mid           = s["mid"]
        breakeven     = round(s["strike"] + mid, 2)
        breakeven_pct = round((breakeven - price) / price * 100, 2)
        premium_total = round(mid * 100, 0)

        # Gate de riesgo: la prima ES la pérdida máxima de un Long Call
        if premium_total > max_risk:
            continue

        profit_50     = round(mid * 0.50 * 100, 0)
        profit_70     = round(mid * 0.70 * 100, 0)
        ideal         = DELTA_IDEAL_LOW <= delta <= DELTA_IDEAL_HIGH
        theta_day     = round(abs(s["theta"]) * 100, 2) if s["theta"] else 0

        results.append({
            "strike":        s["strike"],
            "delta":         round(delta, 3),
            "bid":           s["bid"],
            "ask":           s["ask"],
            "mid":           mid,
            "iv":            round(s["iv"], 1) if s["iv"] else None,
            "theta_day":     theta_day,
            "premium_total": premium_total,
            "breakeven":     breakeven,
            "breakeven_pct": breakeven_pct,
            "profit_50":     profit_50,
            "profit_70":     profit_70,
            "ideal_delta":   ideal,
            "itm":           s["strike"] < price,
            "within_budget": True,
        })

    results.sort(key=lambda x: (not x["ideal_delta"], x["breakeven_pct"]))
    return results[:MAX_LONG_CALLS]


# ══════════════════════════════════════════════════════════════════════════════
# BULL CALL SPREAD BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_call_spreads(strike_table, price, ticker=""):
    max_risk    = max_risk_dollars()       # NLV real × pct, una vez por build
    long_cands  = [s for s in strike_table
                   if SPREAD_LONG_DELTA_RANGE[0] <= s["delta"] <= SPREAD_LONG_DELTA_RANGE[1]]
    short_cands = [s for s in strike_table
                   if SPREAD_SHORT_DELTA_RANGE[0] <= s["delta"] <= SPREAD_SHORT_DELTA_RANGE[1]]

    spreads   = []
    descartes = []      # motivos de rechazo por liquidez, para el log
    for long_leg in long_cands:
        for short_leg in short_cands:
            if short_leg["strike"] <= long_leg["strike"]:
                continue
            spread_width = short_leg["strike"] - long_leg["strike"]
            if spread_width < MIN_SPREAD_WIDTH or spread_width > price * 0.15:
                continue

            net_debit = round(long_leg["mid"] - short_leg["mid"], 2)
            if net_debit <= 0:
                continue

            max_profit    = round((spread_width - net_debit) * 100, 0)
            max_loss      = round(net_debit * 100, 0)
            breakeven     = round(long_leg["strike"] + net_debit, 2)
            breakeven_pct = round((breakeven - price) / price * 100, 2)
            rr            = round(max_profit / max_loss, 2) if max_loss > 0 else 0

            # Gates: riesgo ≤ tope y R/R mínimo (débito)
            if max_loss > max_risk:
                continue
            if rr < MIN_RR_DEBIT:
                continue

            # Gate de LIQUIDEZ — ver spread_liquidity()
            liq_ok, liq = spread_liquidity(long_leg, short_leg, spread_width,
                                           is_credit=False)
            if not liq_ok:
                descartes.append(
                    f"${long_leg['strike']:.1f}/${short_leg['strike']:.1f}: "
                    f"{liq['motivo']}")
                continue

            spreads.append({
                "bid_ask":     liq["bid_ask"],
                "bid_ask_pct": round(liq["bid_ask_pct"] * 100, 1),
                "mark":        liq["mark"],
                "long_strike":   long_leg["strike"],
                "short_strike":  short_leg["strike"],
                "long_delta":    round(long_leg["delta"], 3),
                "short_delta":   round(short_leg["delta"], 3),
                "net_debit":     net_debit,
                "cost_total":    max_loss,
                "max_profit":    max_profit,
                "max_loss":      max_loss,
                "breakeven":     breakeven,
                "breakeven_pct": breakeven_pct,
                "risk_reward":   rr,
                "profit_50":     round(max_profit * 0.50, 0),
                "profit_70":     round(max_profit * 0.70, 0),
                "within_budget": True,
            })

    _report_descartes(ticker, "Bull Call Spread", descartes, len(spreads))
    spreads.sort(key=lambda x: (not x["within_budget"], -x["risk_reward"]))
    return spreads[:MAX_SPREADS]


# ══════════════════════════════════════════════════════════════════════════════
# BULL PUT SPREAD BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_put_spreads(strike_table, price, ticker=""):
    """
    Build Bull Put Spread candidates.

    Structure:
        Sell OTM put (higher strike, Delta 0.25-0.45 abs) — collect premium
        Buy  OTM put (lower strike,  Delta 0.10-0.25 abs) — limit risk

    Note: put deltas are negative. We use absolute values for comparison.
    Profit = net credit received (if price stays above short put at expiry)
    Max loss = spread width - net credit

    We want:
        - Short put slightly OTM (below current price)
        - Long put further OTM (more below current price)
        - Short strike > Long strike (both below price)
    """
    # For puts: delta is negative, abs(delta) is what we compare
    # Puts with higher absolute delta = closer to ATM = higher strike
    max_risk    = max_risk_dollars()       # NLV real × pct, una vez por build
    short_cands = [s for s in strike_table
                   if PUT_SHORT_DELTA_RANGE[0] <= abs(s["delta"]) <= PUT_SHORT_DELTA_RANGE[1]
                   and s["strike"] < price]  # must be OTM (below price)

    long_cands  = [s for s in strike_table
                   if PUT_LONG_DELTA_RANGE[0] <= abs(s["delta"]) <= PUT_LONG_DELTA_RANGE[1]
                   and s["strike"] < price]  # further OTM

    put_spreads = []
    descartes   = []    # motivos de rechazo por liquidez, para el log
    for short_leg in short_cands:
        for long_leg in long_cands:
            # Long put must have lower strike than short put
            if long_leg["strike"] >= short_leg["strike"]:
                continue

            spread_width = short_leg["strike"] - long_leg["strike"]
            if spread_width < MIN_SPREAD_WIDTH or spread_width > price * 0.12:
                continue

            # Net credit = what we receive for selling short - what we pay for long
            net_credit = round(short_leg["mid"] - long_leg["mid"], 2)
            if net_credit <= 0:
                continue

            max_profit  = round(net_credit * 100, 0)       # credit received
            max_loss    = round((spread_width - net_credit) * 100, 0)
            # Breakeven: short put strike - net credit
            breakeven   = round(short_leg["strike"] - net_credit, 2)
            # How far below current price is breakeven? (negative = below price)
            be_pct      = round((breakeven - price) / price * 100, 2)
            # R/R for credit spreads: max_profit / max_loss
            rr          = round(max_profit / max_loss, 2) if max_loss > 0 else 0
            # Probability of profit ≈ 1 - abs(short delta)
            pop_approx  = round((1 - abs(short_leg["delta"])) * 100, 0)

            # Gates: riesgo ≤ tope y POP mínimo (crédito)
            if max_loss > max_risk:
                continue
            if pop_approx < MIN_POP_CREDIT:
                continue

            # Gate de LIQUIDEZ — ver spread_liquidity()
            liq_ok, liq = spread_liquidity(long_leg, short_leg, spread_width,
                                           is_credit=True)
            if not liq_ok:
                descartes.append(
                    f"${short_leg['strike']:.1f}/${long_leg['strike']:.1f}: "
                    f"{liq['motivo']}")
                continue

            put_spreads.append({
                "bid_ask":     liq["bid_ask"],
                "bid_ask_pct": round(liq["bid_ask_pct"] * 100, 1),
                "mark":        liq["mark"],
                "short_strike":   short_leg["strike"],   # sell this (higher)
                "long_strike":    long_leg["strike"],    # buy this (lower, protection)
                "short_delta":    round(short_leg["delta"], 3),
                "long_delta":     round(long_leg["delta"], 3),
                "short_mid":      short_leg["mid"],
                "long_mid":       long_leg["mid"],
                "net_credit":     net_credit,
                "max_profit":     max_profit,
                "max_loss":       max_loss,
                "breakeven":      breakeven,
                "breakeven_pct":  be_pct,
                "risk_reward":    rr,
                "pop_approx":     pop_approx,
                "stop_loss_2x":   round(net_credit * 2 * 100, 0),  # close if spread costs 2x credit
            })

    _report_descartes(ticker, "Bull Put Spread", descartes, len(put_spreads))
    # Sort: best R/R first
    put_spreads.sort(key=lambda x: -x["risk_reward"])
    return put_spreads[:MAX_SPREADS]


# ══════════════════════════════════════════════════════════════════════════════
# MARKDOWN BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_markdown(tickers_data, options_results):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines     = [
        f"# Option Selector — {timestamp}",
        f"DTE: {DTE_MIN}-{DTE_MAX} | Delta ranges auto-selected by strategy",
        "",
    ]

    for ticker, criteria in tickers_data.items():
        data        = options_results.get(ticker, {})
        long_calls  = data.get("long_calls", [])
        spreads     = data.get("spreads", [])
        put_spreads = data.get("put_spreads", [])
        exp_date    = data.get("exp_date")
        dte         = data.get("dte")
        strategy    = data.get("strategy", "Bull Call Spread")

        price = criteria.get("price", 0)
        vol   = criteria.get("volatility", {})
        tech  = criteria.get("technical", {})
        earn  = criteria.get("earnings", {})

        lines.append(f"## {ticker} — ${price:.2f}")
        lines.append("")

        # Criteria summary
        trend     = tech.get("trend_25d", {})
        ma        = tech.get("moving_averages", {})
        rsi       = tech.get("rsi")
        iv        = vol.get("iv")
        ivp       = vol.get("iv_percentile")
        iv_rank   = vol.get("iv_rank")
        hv        = vol.get("hv_30d")
        iv_hv     = vol.get("iv_hv_diff")
        beta      = vol.get("beta")
        pcr       = vol.get("put_call_ratio")
        oi        = vol.get("open_interest")
        days_earn = earn.get("days_to_earnings")

        trend_str = f"{'BULLISH' if trend.get('is_bullish') else 'BEARISH'} ({trend.get('pct_change', 0):+.1f}% 25d)"
        sma_str   = 'Above both' if ma.get('above_sma50') and ma.get('above_sma200') else 'Above SMA50'
        rsi_str   = f"{rsi:.1f}" if rsi else "N/A"

        lines.append("**Criteria:**")
        lines.append(f"Trend: {trend_str} | SMAs: {sma_str} | RSI: {rsi_str}")
        if all(x is not None for x in [iv, ivp, iv_rank, hv, iv_hv]):
            lines.append(f"IV: {iv:.1f}% (P{ivp:.0f} / Rank {iv_rank:.2f}) | HV: {hv:.1f}% | IV-HV: {iv_hv:+.1f}%")
        beta_str  = f"{beta:.2f}"   if beta      is not None else "N/A"
        pcr_str   = f"{pcr:.2f}"    if pcr       is not None else "N/A"
        oi_str    = f"{oi:,.0f}"    if oi        is not None else "N/A"
        earn_str  = f"{days_earn}d" if days_earn             else "N/A"
        lines.append(f"Beta: {beta_str} | P/C: {pcr_str} | OI: {oi_str} | Earnings: {earn_str}")
        lines.append("")
        lines.append(f"**Estrategia recomendada: {strategy}**")
        lines.append("")

        if not long_calls and not spreads and not put_spreads:
            lines.append("_No hay estructuras viables (DTE 20-40 / Delta / riesgo ≤3% / R-R / POP)._")
            lines.append("")
            continue

        lines.append(f"**Exp {exp_date} ({dte} DTE)**")
        lines.append("")

        # ── Long Call ─────────────────────────────────────────────────────────
        if long_calls:
            lines.append("### Long Call")
            lines.append("")
            lines.append("| Strike | Delta | Bid | Ask | Mid | Costo | θ/día | IV | Breakeven | +50% | +70% |")
            lines.append("|--------|-------|-----|-----|-----|-------|-------|----|-----------|------|------|")
            for s in long_calls:
                itm_tag   = " ITM" if s["itm"] else ""
                ideal_tag = " ★"   if s["ideal_delta"] else ""
                budget    = "" if s["within_budget"] else " ⚠️"
                iv_str    = f"{s['iv']:.1f}%" if s['iv'] else "N/A"
                lines.append(
                    f"| ${s['strike']:.1f}{itm_tag}{ideal_tag} "
                    f"| {s['delta']:.3f} "
                    f"| ${s['bid']:.2f} | ${s['ask']:.2f} | ${s['mid']:.2f} "
                    f"| ${s['premium_total']:.0f}{budget} "
                    f"| -${s['theta_day']:.2f} "
                    f"| {iv_str} "
                    f"| ${s['breakeven']:.2f} ({s['breakeven_pct']:+.1f}%) "
                    f"| +${s['profit_50']:.0f} | +${s['profit_70']:.0f} |"
                )
            lines.append("")

        # ── Bull Call Spread ──────────────────────────────────────────────────
        if spreads:
            lines.append("### Bull Call Spread")
            lines.append("")
            lines.append("| Compra/Vende | Δ long/short | Débito | Costo | Ganancia máx | R/R | Breakeven | +50% | +70% |")
            lines.append("|--------------|--------------|--------|-------|--------------|-----|-----------|------|------|")
            best_idx = 0
            for i, s in enumerate(spreads):
                budget = "" if s["within_budget"] else " ⚠️"
                lines.append(
                    f"| ${s['long_strike']:.1f}/${s['short_strike']:.1f} "
                    f"| {s['long_delta']:.2f}/{s['short_delta']:.2f} "
                    f"| ${s['net_debit']:.2f} "
                    f"| ${s['cost_total']:.0f}{budget} "
                    f"| +${s['max_profit']:.0f} "
                    f"| {s['risk_reward']:.2f} "
                    f"| ${s['breakeven']:.2f} ({s['breakeven_pct']:+.1f}%) "
                    f"| +${s['profit_50']:.0f} | +${s['profit_70']:.0f} |"
                )
            best = spreads[best_idx]
            lines.append("")
            lines.append(
                f"**Mejor spread:** Compra ${best['long_strike']:.1f} / Vende ${best['short_strike']:.1f} "
                f"| Débito ${best['net_debit']:.2f} (${best['cost_total']:.0f}) "
                f"| Ganancia máx +${best['max_profit']:.0f} "
                f"| R/R {best['risk_reward']:.2f} "
                f"| Breakeven ${best['breakeven']:.2f} ({best['breakeven_pct']:+.1f}%)"
            )
            lines.append("")

        # ── Bull Put Spread ───────────────────────────────────────────────────
        if put_spreads:
            lines.append("### Bull Put Spread (Credit Spread)")
            lines.append("")
            lines.append("_Vendes el put de strike alto, compras el de strike bajo. "
                         "Cobras crédito desde el día 1. "
                         "Ganas si el precio se mantiene sobre el breakeven._")
            lines.append("")
            lines.append("| Vende/Compra | Δ short/long | Crédito | Max Ganancia | Max Pérdida | R/R | Breakeven | POP | Stop 2x |")
            lines.append("|-------------|--------------|---------|--------------|-------------|-----|-----------|-----|---------|")
            for s in put_spreads:
                lines.append(
                    f"| ${s['short_strike']:.1f}/${s['long_strike']:.1f} "
                    f"| {s['short_delta']:.2f}/{s['long_delta']:.2f} "
                    f"| ${s['net_credit']:.2f} "
                    f"| +${s['max_profit']:.0f} "
                    f"| -${s['max_loss']:.0f} "
                    f"| {s['risk_reward']:.2f} "
                    f"| ${s['breakeven']:.2f} ({s['breakeven_pct']:+.1f}%) "
                    f"| ~{s['pop_approx']:.0f}% "
                    f"| ${s['stop_loss_2x']:.0f} |"
                )
            best = put_spreads[0]
            lines.append("")
            lines.append(
                f"**Mejor spread:** Vende ${best['short_strike']:.1f} / Compra ${best['long_strike']:.1f} "
                f"| Crédito ${best['net_credit']:.2f} (${best['max_profit']:.0f}) "
                f"| Max pérdida -${best['max_loss']:.0f} "
                f"| R/R {best['risk_reward']:.2f} "
                f"| Breakeven ${best['breakeven']:.2f} ({best['breakeven_pct']:+.1f}%) "
                f"| POP ~{best['pop_approx']:.0f}%"
            )
            lines.append("")

    lines.append(f"---")
    lines.append(f"_Generated {timestamp} · option_selector.py_")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# SYNC WRAPPER
# ══════════════════════════════════════════════════════════════════════════════

def get_options_for_tickers(session, tickers_data):
    """Synchronous entry point for scanner.py."""
    try:
        return asyncio.run(_get_options_async(session, tickers_data))
    except Exception as e:
        return f"option_selector error: {e}"


async def _get_options_async(session, tickers_data):
    from criteria import select_strategy

    # THROTTLE entre tickers — evita el 429 de Tastytrade (3-ago).
    #   Cada ticker dispara ~3 operaciones al broker (cadena + streamer +
    #   market_data). 14 tickers sin pausa = ~42 requests en ráfaga, muy por
    #   encima del límite del broker (~2 req/s, referencia empírica). El 429
    #   resultante se disfrazaba de "sin estructura viable" y el LLM lo
    #   racionalizaba como si no hubiera trades — un fallo técnico reportado
    #   como condición de mercado, con errors=0.
    #   Un sleep entre tickers espacia las llamadas. asyncio.sleep (no time.sleep)
    #   porque estamos en async: time.sleep bloquearía el event loop.
    #   Configurable; default 1.0s. Va ENTRE tickers, no tras el último.
    throttle = float(os.getenv("SCANNER_TICKER_DELAY", "1.0"))

    results = {}
    items = list(tickers_data.items())
    for i, (ticker, criteria) in enumerate(items):
        strategy = select_strategy(criteria)
        results[ticker] = await _fetch_option_data(session, ticker,
                                                    criteria.get("price", 0),
                                                    strategy)
        if i < len(items) - 1:          # no dormir tras el último
            await asyncio.sleep(throttle)
    return _build_markdown(tickers_data, results)

def position_max_loss(strike_low, strike_high, debit, contracts=1) -> float:
    """
    Pérdida máxima en DÓLARES de un spread vertical de 2 patas.
    Fuente ÚNICA: la usan el gate de cartera (auto_run) y check_open.py.

    Manda el SIGNO de `debit`, no el string de strategy:
        debit > 0  -> débito  (BCS): pérdida máx = lo que pagaste
        debit < 0  -> crédito (BPS): pérdida máx = ancho - crédito

    `premium_paid` en la DB ya trae ese signo, así que una fila se pasa directo.
    """
    width = abs(float(strike_high) - float(strike_low))
    d     = float(debit)
    n     = int(contracts or 1)
    if d > 0:
        return round(d * 100 * n, 2)
    return round((width - abs(d)) * 100 * n, 2)

def portfolio_risk_pct() -> float:
    """
    Tope de riesgo AGREGADO de cartera, en % del capital.
    Fuente ÚNICA: la usan el gate de auto_run y check_open.py.

    Obligatoria en los DOS libros (paper y live). Sin default: un tope ausente
    no es "sin tope", es un bug. El default silencioso es exactamente cómo
    MAX_COST terminó siendo decorativo y dejó pasar el GS de $3,945 (§12.3).
    """
    raw = os.getenv("MAX_PORTFOLIO_RISK_PCT")
    if raw is None:
        raise RuntimeError(
            "MAX_PORTFOLIO_RISK_PCT no está definida. Obligatoria en paper y en "
            "live: sin ella el gate de cartera no rechazaría nada."
        )
    return float(raw)


def spread_pnl(strike_low, strike_high, premium_paid, contracts, spread_value,
               strategy=None, long_value=None):
    """
    P&L de una posición de opciones. FUENTE ÚNICA.

    Reemplaza a calculate_current_pnl (monitor) y a las 3 copias de la matemática
    de spreads que había en trade/monitor. Maneja las TRES estrategias que el
    sistema opera, y ninguna otra función calcula P&L:

        Bull Call Spread  (débito, 2 patas)   premium_paid > 0
        Bull Put Spread   (crédito, 2 patas)  premium_paid < 0
        Long Call / Put   (1 pata)            strategy in ("Long Call","Long Put")

    EL SIGNO MANDA, NO EL STRING (para spreads)
        Un Bear Call Spread —también crédito— sale bien sin nombrarlo. El string
        `strategy` sólo se mira para distinguir la pata larga: un Long Call no
        tiene `spread_value`, tiene `long_value` (precio de la opción sola).

    PARÁMETROS
        spread_value : valor del spread por acción, POSITIVO. Sólo spreads.
        long_value   : precio de la opción larga por acción. Sólo Long Call/Put.
                       Vienen de fuentes distintas (spread_value de la cadena de
                       Tastytrade; long_value de yfinance), por eso son campos
                       separados y no uno reutilizado.

    Devuelve dict con las mismas claves para las tres estrategias, así el que
    consume (evaluate_alert_level, run_position_monitor) no ramifica:
        max_profit, max_loss, current_value, gross_pnl, pnl_pct,
        profit_pct_of_max, spread_value, strategy_type, dte(None), delta(None)
    pnl_pct y profit_pct_of_max pueden ser None si la base es cero.
    """
    n   = int(contracts or 1)
    prem = float(premium_paid)

    # ── LONG CALL / LONG PUT (1 pata) ─────────────────────────────────────────
    if strategy in ("Long Call", "Long Put"):
        total_cost = round(prem * n * 100, 2) if prem > 0 else round(abs(prem) * n * 100, 2)
        # total_cost real: lo pagado por la opción. premium_paid de una long es
        # el débito (positivo). Si viniera el total_cost directo, usarlo.
        if long_value is None:
            # Sin precio no se inventa: P&L desconocido, no cero (§10).
            return {
                "max_profit": None, "max_loss": total_cost, "current_value": None,
                "gross_pnl": None, "pnl_pct": None, "profit_pct_of_max": None,
                "spread_value": None, "strategy_type": "long_option",
                "dte": None, "delta": None,
            }
        current_value = round(float(long_value) * n * 100, 2)
        gross_pnl     = round(current_value - total_cost, 2)
        # Una long call no tiene máximo real; se usa 2x el costo como referencia
        # para que el objetivo del 70% signifique algo (convención heredada).
        max_profit    = round(total_cost * 2, 2)
        return {
            "max_profit":        max_profit,
            "max_loss":          total_cost,          # una long pierde a lo sumo la prima
            "current_value":     current_value,
            "gross_pnl":         gross_pnl,
            "pnl_pct":           round(gross_pnl / total_cost * 100, 2) if total_cost else None,
            "profit_pct_of_max": round(gross_pnl / max_profit, 4) if max_profit else None,
            "spread_value":      round(float(long_value), 4),
            "strategy_type":     "long_option",
            "dte":               None,
            "delta":             None,
        }

    # ── SPREADS DE 2 PATAS ────────────────────────────────────────────────────
    if spread_value is None:
        return {
            "max_profit": None, "max_loss": None, "current_value": None,
            "gross_pnl": None, "pnl_pct": None, "profit_pct_of_max": None,
            "spread_value": None, "strategy_type": "spread",
            "dte": None, "delta": None,
        }

    width = abs(float(strike_high) - float(strike_low))
    sv    = abs(float(spread_value))

    if prem < 0:
        # CRÉDITO (Bull Put / Bear Call). Cobraste al abrir; cerrar cuesta sv.
        net_credit    = abs(prem)
        max_profit    = round(net_credit * n * 100, 2)
        current_value = round(sv * n * 100, 2)
        gross_pnl     = round(max_profit - current_value, 2)
        base_pct      = max_profit
        tipo          = "credit_spread"
    else:
        # DÉBITO (Bull Call / Bear Put). Pagaste al abrir; cerrar te paga sv.
        total_cost    = round(prem * n * 100, 2)
        max_profit    = round((width - prem) * n * 100, 2)
        current_value = round(sv * n * 100, 2)
        gross_pnl     = round(current_value - total_cost, 2)
        base_pct      = total_cost
        tipo          = "debit_spread"

    return {
        "max_profit":        max_profit,
        "max_loss":          position_max_loss(strike_low, strike_high, prem, n),
        "current_value":     current_value,
        "gross_pnl":         gross_pnl,
        "pnl_pct":           round(gross_pnl / base_pct * 100, 2) if base_pct else None,
        "profit_pct_of_max": round(gross_pnl / max_profit, 4) if max_profit else None,
        "spread_value":      round(sv, 4),
        "strategy_type":     tipo,
        "dte":               None,
        "delta":             None,
    }