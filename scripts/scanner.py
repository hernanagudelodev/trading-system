"""
scripts/scanner.py  (v2-bidi)
=============================
SCANNER de v2 — Capa de Estudio. Mide y expone hechos de mercado; NO decide
(no filtra, no rankea, no puntua, no elige direccion). Fuente UNICA: Tastytrade.

FLUJO DE LA CAPA DE ESTUDIO (4 pasos)
    1. Refresco de velas   <- LISTO. Incremental por defecto; backfill si vacia.
    2. Hechos por accion   <- LISTO (modo --facts TICKER).
    3. Nivel mercado       <- LISTO (modo --market): SPY + regimen + VIX.
    4. Persistir dossier   <- TODO. ticker_study + study_fact (computado = persistido).

REGIMEN / VIX (hechos derivados deterministas, NO veredicto)
    regime: BULLISH si SPY>SMA50 y pct_25d>0; BEARISH si SPY<SMA50 y pct_25d<0;
            NEUTRAL el resto.
    vix_level: CALM <18, ELEVATED <25, HIGH <35, EXTREME >=35 (umbrales de def).
    vix_trend: FALLING si current<avg_5d*0.95, RISING si >*1.05, STABLE el resto.
    Sin score ni verdict — eso es Seleccion. VIX y SPY salen de candle_daily.

USO (PowerShell, venv trading_env)
    python scanner.py --test              # paso 1, lista corta, dry-run
    python scanner.py --commit            # paso 1, refresca candle_daily
    python scanner.py --facts AAPL        # paso 2, hechos de un ticker
    python scanner.py --market            # paso 3, nivel mercado (SPY + regimen + VIX)

VARIABLES DE ENTORNO (en .env.v2)
    TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN, DATABASE_URL
"""
import os
import sys
import time
import asyncio
import argparse
import datetime
from pathlib import Path

from dotenv import load_dotenv

# ── Rutas / entorno ───────────────────────────────────────────────────────────
_THIS_DIR  = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_THIS_DIR))

_ENV_NAME  = os.getenv("ENV_FILE", ".env.v2")
_ENV_PATH  = Path(_ENV_NAME)
if not _ENV_PATH.is_absolute():
    _ENV_PATH = _REPO_ROOT / _ENV_PATH
_ENV_LOADED = load_dotenv(_ENV_PATH)

# ── Parametros ────────────────────────────────────────────────────────────────
MIN_VELAS        = 250
DAYS_BACKFILL    = 400
COLCHON_DIAS     = 7
BATCH_SIZE       = 30
BATCH_DEADLINE_S = 45.0
QUIET_S          = 3.0
RS_WINDOW        = 25
SPY_SYMBOL       = "SPY"
VIX_SYMBOL       = "VIX"
VIX_CALM         = 18
VIX_ELEVATED     = 25
VIX_FEAR         = 35

TICKERS_PRUEBA = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM",
    "V", "JNJ", "WMT", "PG", "XOM", "HD", "CVX", "KO", "PEP", "BAC",
    "DIS", "CSCO", "BRK-B", "BF-B",
]


# ══════════════════════════════════════════════════════════════════════════════
# NORMALIZACION
# ══════════════════════════════════════════════════════════════════════════════

def yahoo_a_canon(t):
    return t.replace("-", ".")


def canon_a_streamer(t):
    return t.replace(".", "/")


def get_universe(usar_test):
    if usar_test:
        print(f"  fuente: lista de prueba ({len(TICKERS_PRUEBA)} tickers)")
        raw = list(TICKERS_PRUEBA)
    else:
        try:
            from universe import get_sp500_tickers
        except Exception as e:
            print(f"  ⛔ no se pudo importar universe.get_sp500_tickers: {e}")
            return []
        raw = get_sp500_tickers()
        if not raw:
            print("  ⛔ get_sp500_tickers() devolvió vacio.")
            return []
    return [yahoo_a_canon(t) for t in raw]


def faltan_credenciales(commit):
    req = ["TASTYTRADE_CLIENT_SECRET", "TASTYTRADE_REFRESH_TOKEN"]
    if commit:
        req.append("DATABASE_URL")
    return [k for k in req if not os.getenv(k)]


# ══════════════════════════════════════════════════════════════════════════════
# DB
# ══════════════════════════════════════════════════════════════════════════════

def _conn():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS candle_daily (
            ticker         VARCHAR(10)    NOT NULL,
            candle_date    DATE           NOT NULL,
            open           DECIMAL(14,4)  NOT NULL,
            high           DECIMAL(14,4)  NOT NULL,
            low            DECIMAL(14,4)  NOT NULL,
            close          DECIMAL(14,4)  NOT NULL,
            volume         BIGINT,
            imp_volatility DECIMAL(8,4),
            updated_at     TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
            PRIMARY KEY (ticker, candle_date)
        );
    """)


def _ultima_fecha_global(cur):
    cur.execute("SELECT MAX(candle_date) FROM candle_daily")
    return cur.fetchone()[0]


def _counts_por_ticker(cur, canon_tickers):
    cur.execute(
        "SELECT ticker, COUNT(*) FROM candle_daily WHERE ticker = ANY(%s) GROUP BY ticker",
        (canon_tickers,),
    )
    return dict(cur.fetchall())


def _upsert_bulk(cur, all_rows):
    from psycopg2.extras import execute_values
    sql = """
        INSERT INTO candle_daily
            (ticker, candle_date, open, high, low, close, volume, imp_volatility, updated_at)
        VALUES %s
        ON CONFLICT (ticker, candle_date) DO UPDATE SET
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, volume = EXCLUDED.volume,
            imp_volatility = EXCLUDED.imp_volatility, updated_at = NOW();
    """
    template = "(%s,%s,%s,%s,%s,%s,%s,%s,NOW())"
    data = [
        (r["ticker"], r["date"], r["open"], r["high"], r["low"],
         r["close"], r["volume"], r["iv"])
        for r in all_rows
    ]
    execute_values(cur, sql, data, template=template, page_size=1000)


def _load_df(cur, ticker):
    import pandas as pd
    cur.execute("""
        SELECT candle_date, open, high, low, close, volume
        FROM candle_daily WHERE ticker = %s ORDER BY candle_date ASC
    """, (ticker,))
    rows = cur.fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "Open", "High", "Low", "Close", "Volume"])
    df = df.set_index("date")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = df[col].astype(float)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# FETCH DE CANDLES (DXLink)
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_batch_async(canon_symbols, start_dt):
    from tastytrade import Session, DXLinkStreamer
    from tastytrade.dxfeed import Candle

    cs = os.getenv("TASTYTRADE_CLIENT_SECRET")
    rt = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not cs or not rt:
        print("  ⛔ faltan credenciales de Tastytrade")
        return {}

    session   = Session(cs, rt)
    streamers = [canon_a_streamer(s) for s in canon_symbols]
    canon_de  = {canon_a_streamer(s): s for s in canon_symbols}
    by_ticker = {s: {} for s in canon_symbols}

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe_candle(streamers, "1d", start_time=start_dt)

        loop          = asyncio.get_running_loop()
        hard_deadline = loop.time() + BATCH_DEADLINE_S
        last_progress = loop.time()

        while True:
            now = loop.time()
            if now - last_progress > QUIET_S:
                break
            if now > hard_deadline:
                break
            try:
                c = await asyncio.wait_for(streamer.get_event(Candle), timeout=QUIET_S)
            except asyncio.TimeoutError:
                break

            if None in (c.open, c.high, c.low, c.close):
                continue
            base  = c.event_symbol.split("{")[0]
            canon = canon_de.get(base)
            if canon is None:
                continue
            d = datetime.datetime.fromtimestamp(
                    c.time / 1000, tz=datetime.timezone.utc).date()
            es_nueva = d not in by_ticker[canon]
            by_ticker[canon][d] = {
                "ticker": canon, "date": d,
                "open": float(c.open), "high": float(c.high),
                "low": float(c.low), "close": float(c.close),
                "volume": int(c.volume) if c.volume is not None else None,
                "iv": float(c.imp_volatility) if c.imp_volatility is not None else None,
            }
            if es_nueva:
                last_progress = loop.time()

    return {t: [by_ticker[t][d] for d in sorted(by_ticker[t])] for t in canon_symbols}


def fetch_batch(canon_symbols, start_dt, retries=2, delay=3):
    for attempt in range(retries):
        try:
            res = asyncio.run(_fetch_batch_async(canon_symbols, start_dt))
            if res:
                return res
        except Exception as e:
            print(f"     batch error intento {attempt+1}/{retries}: {e}")
        if attempt < retries - 1:
            time.sleep(delay)
    return {t: [] for t in canon_symbols}


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ══════════════════════════════════════════════════════════════════════════════
# PASO 1 — REFRESCO DE VELAS
# ══════════════════════════════════════════════════════════════════════════════

def refresh_candles(canon_tickers, commit):
    conn = _conn(); cur = conn.cursor()
    _ensure_table(cur); conn.commit()

    ultima = _ultima_fecha_global(cur)
    hoy    = datetime.datetime.now()
    if ultima is None:
        start = hoy - datetime.timedelta(days=DAYS_BACKFILL)
        modo  = f"BACKFILL (tabla vacia, {DAYS_BACKFILL}d)"
    else:
        start = datetime.datetime(ultima.year, ultima.month, ultima.day) \
                - datetime.timedelta(days=COLCHON_DIAS)
        modo  = f"INCREMENTAL (desde {start.date()}, ultima en tabla {ultima})"
    print(f"  modo: {modo}")

    all_rows = []
    t0 = time.time()
    n_batches = (len(canon_tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    for i, batch in enumerate(_chunks(canon_tickers, BATCH_SIZE), 1):
        tb  = time.time()
        res = fetch_batch(batch, start)
        traidas = sum(len(v) for v in res.values())
        for rows in res.values():
            all_rows.extend(rows)
        print(f"  batch {i}/{n_batches}: {traidas} velas  ({time.time()-tb:.1f}s)")

    print(f"  fetch: {len(all_rows)} velas en {time.time()-t0:.1f}s")

    if commit and all_rows:
        tw = time.time()
        _upsert_bulk(cur, all_rows)
        conn.commit()
        print(f"  ✅ upsert de {len(all_rows)} velas en candle_daily ({time.time()-tw:.1f}s)")
    elif not commit:
        print(f"  DRY RUN — no se escribió. Con --commit se hace el upsert.")

    counts = _counts_por_ticker(cur, canon_tickers)
    completos   = [t for t in canon_tickers if counts.get(t, 0) >= MIN_VELAS]
    incompletos = {t: counts.get(t, 0) for t in canon_tickers if counts.get(t, 0) < MIN_VELAS}
    cur.close(); conn.close()
    return completos, incompletos


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS DE HECHOS
# ══════════════════════════════════════════════════════════════════════════════

def _try(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _days_to(d):
    if d is None:
        return None
    try:
        if isinstance(d, str):
            d = datetime.date.fromisoformat(d[:10])
        elif isinstance(d, datetime.datetime):
            d = d.date()
        return (d - datetime.date.today()).days
    except Exception:
        return None


def _ret_pct(closes, n=RS_WINDOW):
    if closes is None:
        return None
    vals = [float(x) for x in (closes.values if hasattr(closes, "values") else closes)]
    if len(vals) < n + 1:
        return None
    c_now, c_then = vals[-1], vals[-1 - n]
    if c_then == 0:
        return None
    return round((c_now / c_then - 1) * 100, 2)


# ══════════════════════════════════════════════════════════════════════════════
# PASO 2 — HECHOS POR ACCION
# ══════════════════════════════════════════════════════════════════════════════

def _spy_return(cur, n=RS_WINDOW):
    cur.execute("SELECT close FROM candle_daily WHERE ticker = %s ORDER BY candle_date ASC",
                (SPY_SYMBOL,))
    rows = cur.fetchall()
    return _ret_pct([r[0] for r in rows], n) if rows else None


def _sector_return(cur, sector, sector_map, n=RS_WINDOW):
    if not sector:
        return None
    tickers = [t for t, s in sector_map.items() if s == sector]
    if not tickers:
        return None
    cur.execute("""SELECT ticker, close FROM candle_daily
                   WHERE ticker = ANY(%s) ORDER BY ticker, candle_date ASC""", (tickers,))
    from collections import defaultdict
    series = defaultdict(list)
    for tk, c in cur.fetchall():
        series[tk].append(c)
    rets = [_ret_pct(cl, n) for cl in series.values()]
    rets = [r for r in rets if r is not None]
    return round(sum(rets) / len(rets), 2) if rets else None


async def _is_etf_async(canon_ticker):
    from tastytrade import Session
    from tastytrade.instruments import Equity
    session = Session(os.getenv("TASTYTRADE_CLIENT_SECRET"), os.getenv("TASTYTRADE_REFRESH_TOKEN"))
    res = await Equity.get(session, [canon_ticker])
    eq  = res[0] if isinstance(res, list) else res
    return bool(eq.is_etf)


def get_is_etf(canon_ticker):
    try:
        return asyncio.run(_is_etf_async(canon_ticker))
    except Exception:
        return None


def study_stock(ticker, df, sector=None, ret_spy=None, ret_sector=None):
    from criteria import (
        get_trend_25d, get_moving_averages, get_rsi, get_52_week_position,
        get_support_resistance, get_candlestick_pattern, get_historical_volatility,
        get_volume, get_volatility_from_tastytrade,
    )

    closes = df["Close"]
    price  = float(closes.iloc[-1])
    facts  = {"ticker": ticker, "price": round(price, 2), "sector": sector}

    grupos = {
        "trend_25d":          _try(get_trend_25d, closes),
        "moving_averages":    _try(get_moving_averages, closes),
        "week_52":            _try(get_52_week_position, closes, price),
        "support_resistance": _try(get_support_resistance, closes, price),
        "candlestick":        _try(get_candlestick_pattern, df),
        "volume":             _try(get_volume, df),
        "volatility_tt":      _try(get_volatility_from_tastytrade, ticker),
    }
    facts["rsi"]    = _try(get_rsi, closes)
    facts["hv_30d"] = _try(get_historical_volatility, closes)

    for g, v in grupos.items():
        if isinstance(v, dict):
            facts.update(v)
        else:
            facts[g] = None

    tt = grupos["volatility_tt"] if isinstance(grupos["volatility_tt"], dict) else {}
    facts["days_to_earnings"] = _days_to(tt.get("earnings_date"))
    facts["is_etf"]           = get_is_etf(ticker)

    ret_stock = _ret_pct(closes, RS_WINDOW)
    facts["rs_vs_spy"]    = round(ret_stock - ret_spy, 2)    if (ret_stock is not None and ret_spy is not None) else None
    facts["rs_vs_sector"] = round(ret_stock - ret_sector, 2) if (ret_stock is not None and ret_sector is not None) else None

    faltantes = [g for g, v in grupos.items() if v is None]
    return facts, faltantes


def mostrar_facts(ticker):
    from universe import get_sp500_sectors
    sector_map = _try(get_sp500_sectors) or {}
    sector     = sector_map.get(ticker)

    conn = _conn(); cur = conn.cursor()
    df = _load_df(cur, ticker)
    if df is None:
        cur.close(); conn.close()
        print(f"  ⛔ {ticker} no esta en candle_daily. Refrescá primero (paso 1).")
        return 1
    ret_spy    = _spy_return(cur, RS_WINDOW)
    ret_sector = _sector_return(cur, sector, sector_map, RS_WINDOW)
    cur.close(); conn.close()

    print(f"  {ticker}: {len(df)} velas  ({df.index[0]} → {df.index[-1]})  "
          f"sector={sector}  ret_SPY_25d={ret_spy}  ret_sector_25d={ret_sector}")

    facts, faltantes = study_stock(ticker, df, sector, ret_spy, ret_sector)
    print(f"\n  HECHOS ({len(facts)} campos):")
    for k in sorted(facts):
        v = facts[k]
        marca = "  ·None" if v is None else ""
        print(f"     {k:<22} {v}{marca}")
    if faltantes:
        print(f"\n  grupos que salieron None: {faltantes}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# PASO 3 — NIVEL MERCADO (SPY + regimen + VIX)
# ══════════════════════════════════════════════════════════════════════════════

def _vix_facts(cur):
    """Hechos de VIX desde candle_daily. Claves estables (None honesto si falta VIX)."""
    keys = {"vix_current": None, "vix_avg_5d": None, "vix_avg_10d": None,
            "vix_trend": None, "vix_level": None}
    cur.execute("SELECT close FROM candle_daily WHERE ticker = %s ORDER BY candle_date ASC",
                (VIX_SYMBOL,))
    closes = [float(r[0]) for r in cur.fetchall()]
    if len(closes) < 5:
        return keys
    current = closes[-1]
    avg_5d  = sum(closes[-5:]) / 5
    avg_10d = sum(closes[-10:]) / 10 if len(closes) >= 10 else None

    trend = "FALLING" if current < avg_5d * 0.95 else \
            "RISING"  if current > avg_5d * 1.05 else "STABLE"
    level = "CALM"     if current < VIX_CALM     else \
            "ELEVATED" if current < VIX_ELEVATED else \
            "HIGH"     if current < VIX_FEAR     else "EXTREME"

    keys.update(
        vix_current=round(current, 2),
        vix_avg_5d=round(avg_5d, 2),
        vix_avg_10d=round(avg_10d, 2) if avg_10d is not None else None,
        vix_trend=trend, vix_level=level,
    )
    return keys


def study_market(cur):
    """
    Bloque de mercado: SPY + regimen + VIX. Todo hecho determinista desde
    candle_daily, reutilizando criteria.py. SIN score/verdict (van a Seleccion).
    """
    from criteria import get_moving_averages, get_trend_25d

    facts = {}

    df = _load_df(cur, SPY_SYMBOL)
    if df is None:
        return None, [f"{SPY_SYMBOL} no esta en candle_daily — backfilleá SPY primero"]

    closes = df["Close"]
    price  = float(closes.iloc[-1])
    ma = _try(get_moving_averages, closes) or {}
    tr = _try(get_trend_25d, closes) or {}

    above_50  = ma.get("above_sma50")
    above_200 = ma.get("above_sma200")
    pct_25d   = tr.get("pct_change")

    if above_50 and above_200:
        sma_status = "ABOVE BOTH"
    elif above_50:
        sma_status = "ABOVE SMA50"
    elif above_200:
        sma_status = "BELOW SMA50"
    else:
        sma_status = "BELOW BOTH"

    if above_50 and pct_25d is not None and pct_25d > 0:
        regime = "BULLISH"
    elif (not above_50) and pct_25d is not None and pct_25d < 0:
        regime = "BEARISH"
    else:
        regime = "NEUTRAL"

    facts.update({
        "spy_price":        round(price, 2),
        "spy_sma50":        ma.get("sma50"),
        "spy_sma200":       ma.get("sma200"),
        "spy_above_sma50":  above_50,
        "spy_above_sma200": above_200,
        "spy_sma_status":   sma_status,
        "spy_sma50_dir":    ma.get("sma50_direction"),
        "spy_pct_25d":      pct_25d,
        "regime":           regime,
    })
    facts.update(_vix_facts(cur))
    return facts, []


def mostrar_market():
    conn = _conn(); cur = conn.cursor()
    facts, notas = study_market(cur)
    cur.close(); conn.close()
    if facts is None:
        for n in notas:
            print(f"  ⛔ {n}")
        return 1
    print(f"\n  NIVEL MERCADO ({len(facts)} campos):")
    for k in sorted(facts):
        v = facts[k]
        marca = "  ·None" if v is None else ""
        print(f"     {k:<22} {v}{marca}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Scanner v2 — Capa de Estudio")
    p.add_argument("--test", action="store_true", help="lista corta (paso 1)")
    p.add_argument("--commit", action="store_true", help="escribe en candle_daily (paso 1)")
    p.add_argument("--facts", metavar="TICKER", help="paso 2: hechos de un ticker")
    p.add_argument("--market", action="store_true", help="paso 3: nivel mercado (SPY + regimen + VIX)")
    a = p.parse_args()

    print(f"\n{'═'*55}")
    print(f"  SCANNER v2 · CAPA DE ESTUDIO")
    print(f"{'═'*55}")
    estado_env = "cargado" if _ENV_LOADED else "NO encontrado — usando variables del sistema"
    print(f"  env: {_ENV_PATH}  ({estado_env})")

    if a.market:
        if not os.getenv("DATABASE_URL"):
            print(f"  ⛔ falta DATABASE_URL (revisá {_ENV_PATH})")
            return 1
        print("  paso 3 · nivel mercado")
        return mostrar_market()

    if a.facts:
        faltan = faltan_credenciales(commit=True)
        if faltan:
            print(f"  ⛔ faltan variables: {', '.join(faltan)} (revisá {_ENV_PATH})")
            return 1
        canon = yahoo_a_canon(a.facts.upper())
        print(f"  paso 2 · hechos por accion · {canon}")
        return mostrar_facts(canon)

    print(f"  paso 1 · refresco de velas  {'[COMMIT]' if a.commit else '[dry-run]'}")
    faltan = faltan_credenciales(a.commit)
    if faltan:
        print(f"  ⛔ faltan variables requeridas: {', '.join(faltan)} (revisá {_ENV_PATH})")
        return 1

    tickers = get_universe(a.test)
    if not tickers:
        print("  ⛔ sin universo — abortando.")
        return 1
    print(f"  universo: {len(tickers)} tickers")

    completos, incompletos = refresh_candles(tickers, a.commit)
    print(f"\n{'─'*55}")
    print(f"  completos (>= {MIN_VELAS} velas en DB): {len(completos)}/{len(tickers)}")
    print(f"  incompletos: {len(incompletos)}")
    if incompletos:
        muestra = list(incompletos.items())[:15]
        print(f"     {muestra}{' ...' if len(incompletos) > 15 else ''}")
    print(f"{'─'*55}")
    print("\n  [paso 4 de la Capa de Estudio: pendiente]")
    return 0


if __name__ == "__main__":
    sys.exit(main())