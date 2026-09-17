"""
scripts/scanner.py  (v2-bidi)
=============================
SCANNER de v2 — Capa de Estudio. Mide y expone hechos de mercado; NO decide
(no filtra, no rankea, no puntua, no elige direccion). Fuente UNICA: Tastytrade.

FLUJO (4 pasos) — el barrido (--scan) los encadena sobre el universo:
    1. Refresco de velas   — incremental por defecto; backfill si vacia.
    2. Hechos por accion   — tecnico desde candle_daily (reutiliza criteria.py) +
       market_metrics (batch) + sector + is_etf (batch) + days_to_earnings +
       fuerza relativa (vs SPY y vs sector). Simetrico, None honesto.
       put_call_ratio/open_interest NO van aca (cadena de opciones -> Seleccion).
    3. Nivel mercado       — SPY + regimen (determinista) + VIX.
    4. Persistir dossier   — ticker_study + study_fact (computado = persistido).
       Cada accion es una fila; el mercado es la fila sintetica __MARKET__.

BATCH (medido: get_market_metrics y Equity.get son ~25-28x mas rapidos en lista)
    Las dos llamadas TT del paso 2 se piden para TODO el universo de un saque, no
    por ticker. study_stock recibe tt_metrics e is_etf ya resueltos y NO toca la
    red — es puro computo sobre datos que le llegan (como ret_spy/ret_sector).

REUTILIZACION (criteria.py, traido de def)
    Funciones tecnicas de criteria.py sobre serie/DataFrame, alimentadas desde
    candle_daily. get_volatility_from_tastytrade de def YA NO se usa (su parseo se
    migro a _parse_metric_obj, en batch). DEUDA: importar criteria arrastra
    yfinance; podar criteria.py (sacar yfinance + fetch muertos) queda pendiente.

USO (PowerShell, venv trading_env)
    python scanner.py --test              # paso 1, lista corta, dry-run
    python scanner.py --commit            # paso 1, refresca candle_daily
    python scanner.py --facts AAPL        # paso 2, hechos de un ticker
    python scanner.py --market            # paso 3, nivel mercado
    python scanner.py --persist AAPL --commit        # paso 4a, persiste un ticker
    python scanner.py --persist-market --commit      # paso 4b, persiste el mercado
    python scanner.py --scan --test                  # paso 4c, barrido (dry-run)
    python scanner.py --scan --test --commit         # paso 4c, barrido + persistencia
    python scanner.py --scan --commit                # barrido de las 500

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
MIN_CANDLES        = 250
DAYS_BACKFILL    = 400
CUSHION_DAYS     = 7
BATCH_SIZE       = 30
BATCH_DEADLINE_S = 45.0
QUIET_S          = 3.0
RS_WINDOW        = 25
SPY_SYMBOL       = "SPY"
VIX_SYMBOL       = "VIX"
# Frescura de las referencias de mercado: si la última vela de SPY/VIX es más vieja
# que esto (días calendario), el refresco falló -> None honesto en vez de servir el
# dato viejo con cara de fresco. 4 cubre un fin de semana largo; más que eso, algo
# no se está refrescando.
MARKET_STALENESS_DAYS = 4
VIX_CALM         = 18
VIX_ELEVATED     = 25
VIX_FEAR         = 35
MARKET_TICKER    = "__MARKET__"
META_BATCH       = 100     # simbolos por llamada batch de metadata (metrics / equity)

TEST_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM",
    "V", "JNJ", "WMT", "PG", "XOM", "HD", "CVX", "KO", "PEP", "BAC",
    "DIS", "CSCO", "BRK-B", "BF-B",
]


# ══════════════════════════════════════════════════════════════════════════════
# NORMALIZACION
# ══════════════════════════════════════════════════════════════════════════════

def yahoo_to_canon(t):
    return t.replace("-", ".")


def canon_to_streamer(t):
    return t.replace(".", "/")


def get_universe(use_test):
    if use_test:
        print(f"  source: test list ({len(TEST_TICKERS)} tickers)")
        raw = list(TEST_TICKERS)
    else:
        try:
            from universe import get_sp500_tickers
        except Exception as e:
            print(f"  ⛔ could not import universe.get_sp500_tickers: {e}")
            return []
        raw = get_sp500_tickers()
        if not raw:
            print("  ⛔ get_sp500_tickers() returned empty.")
            return []
    return [yahoo_to_canon(t) for t in raw]


def missing_credentials(commit):
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


def _ensure_study_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ticker_study (
            id         SERIAL PRIMARY KEY,
            ticker     VARCHAR(12) NOT NULL,
            scan_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            slot       VARCHAR(20),
            price      DECIMAL(14,4),
            sector     VARCHAR(50),
            regime     VARCHAR(10),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS study_fact (
            id         BIGSERIAL PRIMARY KEY,
            study_id   INTEGER      NOT NULL REFERENCES ticker_study(id),
            criterion  VARCHAR(50)  NOT NULL,
            value_num  DOUBLE PRECISION,
            value_bool BOOLEAN,
            value_text VARCHAR(100),
            is_null    BOOLEAN      NOT NULL DEFAULT FALSE,
            UNIQUE (study_id, criterion)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_study_fact_criterion ON study_fact (criterion);")


def _max_candle_date(cur):
    cur.execute("SELECT MAX(candle_date) FROM candle_daily")
    return cur.fetchone()[0]


def _counts_by_ticker(cur, canon_tickers):
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


def _load_all_dfs(cur, tickers):
    """
    UNA sola query -> {ticker: DataFrame OHLCV}. Reemplaza N llamadas a _load_df
    (una por ticker) en el barrido. Mismo formato de DataFrame que _load_df.
    """
    import pandas as pd
    from collections import defaultdict
    cur.execute("""
        SELECT ticker, candle_date, open, high, low, close, volume
        FROM candle_daily WHERE ticker = ANY(%s)
        ORDER BY ticker, candle_date ASC
    """, (tickers,))
    por_tk = defaultdict(list)
    for r in cur.fetchall():
        por_tk[r[0]].append(r[1:])       # (date, o, h, l, c, v)

    out = {}
    for tk, filas in por_tk.items():
        df = pd.DataFrame(filas, columns=["date", "Open", "High", "Low", "Close", "Volume"])
        df = df.set_index("date")
        for col in ("Open", "High", "Low", "Close", "Volume"):
            df[col] = df[col].astype(float)
        out[tk] = df
    return out


# ══════════════════════════════════════════════════════════════════════════════
# FETCH DE CANDLES (DXLink)
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_batch_async(canon_symbols, start_dt):
    from tastytrade import Session, DXLinkStreamer
    from tastytrade.dxfeed import Candle

    cs = os.getenv("TASTYTRADE_CLIENT_SECRET")
    rt = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not cs or not rt:
        print("  ⛔ missing Tastytrade credentials")
        return {}

    session   = Session(cs, rt)
    streamers = [canon_to_streamer(s) for s in canon_symbols]
    canon_de  = {canon_to_streamer(s): s for s in canon_symbols}
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

            # Descarta velas incompletas o basura: OHLC en None, o <= 0. La vela
            # VIVA del día en curso puede llegar con 0.0 en algún estado del stream;
            # sin este filtro entra como último close y arruina precio/régimen.
            ohlc = (c.open, c.high, c.low, c.close)
            if any(x is None for x in ohlc) or any(float(x) <= 0 for x in ohlc):
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
            print(f"     batch error attempt {attempt+1}/{retries}: {e}")
        if attempt < retries - 1:
            time.sleep(delay)
    return {t: [] for t in canon_symbols}


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ══════════════════════════════════════════════════════════════════════════════
# PASO 1 — REFRESCO DE VELAS
# ══════════════════════════════════════════════════════════════════════════════

def refresh_candles(canon_tickers, commit, verbose=True):
    conn = _conn(); cur = conn.cursor()
    _ensure_table(cur); conn.commit()

    # SPY y VIX son referencias de mercado (régimen, VIX, fuerza relativa): se
    # refrescan SIEMPRE junto al universo, aunque no sean del S&P 500 ni candidatas.
    # Sin esto quedan congelados en su backfill inicial y el régimen/VIX salen viejos
    # con cara de frescos (silent failure). NO entran al estudio de acciones.
    refs     = [s for s in (SPY_SYMBOL, VIX_SYMBOL) if s not in canon_tickers]
    to_fetch = list(canon_tickers) + refs

    ultima = _max_candle_date(cur)
    hoy    = datetime.datetime.now()
    if ultima is None:
        start = hoy - datetime.timedelta(days=DAYS_BACKFILL)
        mode_desc  = f"BACKFILL (tabla vacia, {DAYS_BACKFILL}d)"
    else:
        start = datetime.datetime(ultima.year, ultima.month, ultima.day)\
                - datetime.timedelta(days=CUSHION_DAYS)
        mode_desc  = f"INCREMENTAL (desde {start.date()}, ultima en tabla {ultima})"
    if verbose:
        print(f"  mode: {mode_desc}  (+ refs: {', '.join(refs) if refs else 'ninguna'})")

    all_rows = []
    t0 = time.time()
    n_batches = (len(to_fetch) + BATCH_SIZE - 1) // BATCH_SIZE
    for i, batch in enumerate(_chunks(to_fetch, BATCH_SIZE), 1):
        tb  = time.time()
        res = fetch_batch(batch, start)
        fetched = sum(len(v) for v in res.values())
        for rows in res.values():
            all_rows.extend(rows)
        if verbose:
            print(f"  batch {i}/{n_batches}: {fetched} candles  ({time.time()-tb:.1f}s)")

    if verbose:
        print(f"  fetch: {len(all_rows)} candles in {time.time()-t0:.1f}s")

    if commit and all_rows:
        tw = time.time()
        _upsert_bulk(cur, all_rows)
        conn.commit()
        if verbose:
            print(f"  ✅ upsert of {len(all_rows)} candles ({time.time()-tw:.1f}s)")
    elif not commit and verbose:
        print(f"  DRY RUN — nothing written (candles).")

    # complete/incomplete SOLO sobre el universo: SPY/VIX se refrescan pero no son
    # candidatas (no van al estudio de acciones).
    counts = _counts_by_ticker(cur, canon_tickers)
    complete   = [t for t in canon_tickers if counts.get(t, 0) >= MIN_CANDLES]
    incomplete = {t: counts.get(t, 0) for t in canon_tickers if counts.get(t, 0) < MIN_CANDLES}
    cur.close(); conn.close()
    return complete, incomplete


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
# METADATA TT EN BATCH (market_metrics + is_etf)
# ══════════════════════════════════════════════════════════════════════════════

_METRIC_KEYS = ("iv", "iv_30d", "iv_percentile", "iv_rank", "iv_hv_diff", "beta",
                "put_call_ratio", "open_interest", "earnings_date", "pe", "eps",
                "market_cap", "dividend_ex_date", "liquidity_rating")


def _parse_metric_obj(m):
    """MarketMetricInfo -> dict de hechos (mismo parseo que criteria de def)."""
    if m is None:
        return {k: None for k in _METRIC_KEYS}

    def pct_to_100(val):
        if val is None:
            return None
        try:
            f = float(val)
            return round(f * 100, 1) if f <= 1.0 else round(f, 1)
        except Exception:
            return None

    def safe_float(val, mult=1.0, dec=2):
        if val is None:
            return None
        try:
            return round(float(val) * mult, dec)
        except (ValueError, TypeError):
            return None

    earnings_date = None
    try:
        if getattr(m, "earnings", None) and getattr(m.earnings, "expected_report_date", None):
            earnings_date = m.earnings.expected_report_date
    except Exception:
        pass

    liq = getattr(m, "liquidity_rating", None)
    return {
        "iv":               safe_float(getattr(m, "implied_volatility_index", None), 100.0),
        "iv_30d":           safe_float(getattr(m, "implied_volatility_30_day", None)),
        "iv_percentile":    pct_to_100(getattr(m, "implied_volatility_percentile", None)),
        "iv_rank":          safe_float(getattr(m, "tw_implied_volatility_index_rank", None)),
        "iv_hv_diff":       safe_float(getattr(m, "iv_hv_30_day_difference", None)),
        "beta":             safe_float(getattr(m, "beta", None)),
        "put_call_ratio":   None,
        "open_interest":    None,
        "earnings_date":    earnings_date,
        "pe":               safe_float(getattr(m, "price_earnings_ratio", None)),
        "eps":              safe_float(getattr(m, "earnings_per_share", None)),
        "market_cap":       None,
        "dividend_ex_date": None,
        "liquidity_rating": int(liq) if liq is not None else None,
    }


async def _fetch_metrics_async(symbols):
    from tastytrade import Session
    from tastytrade.metrics import get_market_metrics
    session = Session(os.getenv("TASTYTRADE_CLIENT_SECRET"), os.getenv("TASTYTRADE_REFRESH_TOKEN"))
    res = await get_market_metrics(session, symbols)
    return {getattr(m, "symbol", None): m for m in (res or [])}


async def _fetch_etf_async(symbols):
    from tastytrade import Session
    from tastytrade.instruments import Equity
    session = Session(os.getenv("TASTYTRADE_CLIENT_SECRET"), os.getenv("TASTYTRADE_REFRESH_TOKEN"))
    res = await Equity.get(session, symbols)
    res = res if isinstance(res, list) else [res]
    return {getattr(e, "symbol", None): bool(e.is_etf) for e in res}


def fetch_metadata(symbols):
    """
    {ticker: {"metrics": <dict parseado>, "is_etf": <bool|None>}} para todo el
    universo, en batches de META_BATCH. Un ticker que no vuelva queda con metrics
    de None y is_etf None (None honesto).
    """
    metrics_obj = {}
    etf_map     = {}
    for chunk in _chunks(symbols, META_BATCH):
        try:
            metrics_obj.update(asyncio.run(_fetch_metrics_async(chunk)))
        except Exception as e:
            print(f"     metrics batch error: {str(e)[:60]}")
        try:
            etf_map.update(asyncio.run(_fetch_etf_async(chunk)))
        except Exception as e:
            print(f"     equity batch error: {str(e)[:60]}")
    return {
        s: {"metrics": _parse_metric_obj(metrics_obj.get(s)), "is_etf": etf_map.get(s)}
        for s in symbols
    }


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


def _all_sector_returns(cur, sector_map, n=RS_WINDOW):
    """{sector: ret_25d promedio} computado UNA vez por sector (no por ticker)."""
    out = {}
    for sec in set(sector_map.values()):
        if sec:
            out[sec] = _sector_return(cur, sec, sector_map, n)
    return out


def study_stock(ticker, df, sector=None, ret_spy=None, ret_sector=None,
                tt_metrics=None, is_etf=None):
    """
    Hechos por accion. PURO COMPUTO: tt_metrics e is_etf llegan ya resueltos (batch),
    no se toca la red aca. Simetrico, None honesto. Devuelve (facts, faltantes).
    """
    from criteria import (
        get_trend_25d, get_moving_averages, get_rsi, get_52_week_position,
        get_support_resistance, get_candlestick_pattern, get_historical_volatility,
        get_volume,
    )

    closes = df["Close"]
    price  = float(closes.iloc[-1])
    facts  = {"ticker": ticker, "price": round(price, 2), "sector": sector}

    groups = {
        "trend_25d":          _try(get_trend_25d, closes),
        "moving_averages":    _try(get_moving_averages, closes),
        "week_52":            _try(get_52_week_position, closes, price),
        "support_resistance": _try(get_support_resistance, closes, price),
        "candlestick":        _try(get_candlestick_pattern, df),
        "volume":             _try(get_volume, df),
    }
    facts["rsi"]    = _try(get_rsi, closes)
    facts["hv_30d"] = _try(get_historical_volatility, closes)

    for g, v in groups.items():
        if isinstance(v, dict):
            facts.update(v)
        else:
            facts[g] = None

    # market_metrics ya resueltos (batch)
    tt = tt_metrics if isinstance(tt_metrics, dict) else _parse_metric_obj(None)
    facts.update(tt)
    facts["days_to_earnings"] = _days_to(tt.get("earnings_date"))
    facts["is_etf"]           = is_etf

    ret_stock = _ret_pct(closes, RS_WINDOW)
    facts["rs_vs_spy"]    = round(ret_stock - ret_spy, 2)    if (ret_stock is not None and ret_spy is not None) else None
    facts["rs_vs_sector"] = round(ret_stock - ret_sector, 2) if (ret_stock is not None and ret_sector is not None) else None

    missing = [g for g, v in groups.items() if v is None]
    return facts, missing


def show_facts(ticker):
    from universe import get_sp500_sectors
    sector_map = _try(get_sp500_sectors) or {}
    sector     = sector_map.get(ticker)

    meta = fetch_metadata([ticker]).get(ticker, {})

    conn = _conn(); cur = conn.cursor()
    df = _load_df(cur, ticker)
    if df is None:
        cur.close(); conn.close()
        print(f"  ⛔ {ticker} not in candle_daily. Refresh first (step 1).")
        return 1
    ret_spy    = _spy_return(cur, RS_WINDOW)
    ret_sector = _sector_return(cur, sector, sector_map, RS_WINDOW)
    cur.close(); conn.close()

    print(f"  {ticker}: {len(df)} candles  sector={sector}  "
          f"ret_SPY_25d={ret_spy}  ret_sector_25d={ret_sector}")
    facts, missing = study_stock(ticker, df, sector, ret_spy, ret_sector,
                                   meta.get("metrics"), meta.get("is_etf"))
    print(f"\n  FACTS ({len(facts)} fields):")
    for k in sorted(facts):
        v = facts[k]
        marker = "  ·None" if v is None else ""
        print(f"     {k:<22} {v}{marker}")
    if missing:
        print(f"\n  groups that came out None: {missing}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# PASO 3 — NIVEL MERCADO
# ══════════════════════════════════════════════════════════════════════════════

# Fechas FOMC 2026 (calendario fijo). ACTUALIZAR anualmente (mismo dato que def).
FOMC_DATES_2026 = [
    datetime.date(2026, 1, 27), datetime.date(2026, 1, 28),
    datetime.date(2026, 3, 17), datetime.date(2026, 3, 18),
    datetime.date(2026, 4, 28), datetime.date(2026, 4, 29),
    datetime.date(2026, 6, 16), datetime.date(2026, 6, 17),
    datetime.date(2026, 7, 28), datetime.date(2026, 7, 29),
    datetime.date(2026, 9, 15), datetime.date(2026, 9, 16),
    datetime.date(2026, 11, 3), datetime.date(2026, 11, 4),
    datetime.date(2026, 12, 15), datetime.date(2026, 12, 16),
]


def get_macro_events(look_days=7):
    """
    Eventos macro programados en la ventana (calendario fijo, sin red). Portado de
    def market_context.get_macro_events. Lista de dicts: event, date, days_away, impact.
    """
    today  = datetime.date.today()
    events = []
    for d in range(look_days + 1):
        check   = today + datetime.timedelta(days=d)
        day     = check.day
        weekday = check.weekday()
        if weekday == 4 and day <= 7:                       # NFP: 1er viernes
            events.append({"event": "NFP — Non-Farm Payrolls", "date": str(check),
                           "days_away": d, "impact": "HIGH"})
        if day in (10, 11, 12) and weekday not in (5, 6):   # CPI: ~10-12
            events.append({"event": "CPI — Consumer Price Index", "date": str(check),
                           "days_away": d, "impact": "HIGH"})
        if day in (14, 15) and weekday not in (5, 6):       # PPI: ~14-15
            events.append({"event": "PPI — Producer Price Index", "date": str(check),
                           "days_away": d, "impact": "MEDIUM"})
        if check in FOMC_DATES_2026:                        # FOMC: lista fija
            events.append({"event": "FOMC — Fed Meeting", "date": str(check),
                           "days_away": d, "impact": "VERY_HIGH"})
    seen, unique = set(), []
    for e in events:
        if e["event"] not in seen:
            seen.add(e["event"]); unique.append(e)
    return sorted(unique, key=lambda x: x["days_away"])


def _macro_next_high(events):
    """(days_away, nombre) del proximo evento de alto impacto (HIGH/VERY_HIGH), o (None, None)."""
    altos = [e for e in events if e["impact"] in ("HIGH", "VERY_HIGH")]
    if not altos:
        return None, None
    nxt = min(altos, key=lambda e: e["days_away"])
    return nxt["days_away"], nxt["event"]


def _candle_age_days(last_date):
    """Días calendario entre hoy y la fecha de la última vela (None si no se puede)."""
    if last_date is None:
        return None
    try:
        return (datetime.date.today() - last_date).days
    except Exception:
        return None


def _vix_facts(cur):
    keys = {"vix_current": None, "vix_avg_5d": None, "vix_avg_10d": None,
            "vix_trend": None, "vix_level": None, "vix_stale": None}
    cur.execute("SELECT candle_date, close FROM candle_daily WHERE ticker = %s ORDER BY candle_date ASC",
                (VIX_SYMBOL,))
    rows = cur.fetchall()
    closes = [float(r[1]) for r in rows]
    if len(closes) < 5:
        return keys
    # Fix B — None honesto: si la última vela de VIX es vieja (no se refrescó), no
    # servir el valor viejo. Se marca stale y los hechos quedan en None.
    vix_age = _candle_age_days(rows[-1][0])
    if vix_age is None or vix_age > MARKET_STALENESS_DAYS:
        keys["vix_stale"] = True
        return keys
    keys["vix_stale"] = False
    current = closes[-1]
    avg_5d  = sum(closes[-5:]) / 5
    avg_10d = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    trend = "FALLING" if current < avg_5d * 0.95 else\
            "RISING"  if current > avg_5d * 1.05 else "STABLE"
    level = "CALM"     if current < VIX_CALM     else\
            "ELEVATED" if current < VIX_ELEVATED else\
            "HIGH"     if current < VIX_FEAR     else "EXTREME"
    keys.update(vix_current=round(current, 2), vix_avg_5d=round(avg_5d, 2),
                vix_avg_10d=round(avg_10d, 2) if avg_10d is not None else None,
                vix_trend=trend, vix_level=level)
    return keys


def study_market(cur):
    from criteria import get_moving_averages, get_trend_25d

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

    # Regimen por ESTRUCTURA (no por el signo de un solo momentum). Asimetrico a
    # proposito: BEARISH pide solo estructura bajista (bajo ambas medias, aunque el
    # movimiento sea gradual); BULLISH pide estructura + momentum. Asi un bajista
    # lento no se cuela como NEUTRAL y habilita alcistas (beta adversa).
    if above_50 is None or above_200 is None or pct_25d is None:
        regime = "NEUTRAL"                                   # fail-safe: sin dato, no asumir tendencia
    elif above_50 and above_200 and pct_25d > 0:
        regime = "BULLISH"
    elif (not above_50) and (not above_200):
        regime = "BEARISH"
    else:
        regime = "NEUTRAL"

    facts = {
        "spy_price":        round(price, 2),
        "spy_sma50":        ma.get("sma50"),
        "spy_sma200":       ma.get("sma200"),
        "spy_above_sma50":  above_50,
        "spy_above_sma200": above_200,
        "spy_sma_status":   sma_status,
        "spy_sma50_dir":    ma.get("sma50_direction"),
        "spy_pct_25d":      pct_25d,
        "regime":           regime,
    }

    # Fix B — None honesto: si la última vela de SPY es vieja (el refresco no la
    # actualizó), no servir el dato viejo con cara de fresco. spy_* y regime -> None.
    spy_age   = _candle_age_days(df.index[-1])
    spy_stale = (spy_age is None) or (spy_age > MARKET_STALENESS_DAYS)
    facts["spy_stale"] = spy_stale
    if spy_stale:
        for k in ("spy_price", "spy_sma50", "spy_sma200", "spy_above_sma50",
                  "spy_above_sma200", "spy_sma_status", "spy_sma50_dir",
                  "spy_pct_25d", "regime"):
            facts[k] = None

    facts.update(_vix_facts(cur))
    d_high, ev_high = _macro_next_high(get_macro_events())
    facts["macro_next_high_days"]  = d_high      # dias al proximo HIGH/VERY_HIGH, o None
    facts["macro_next_high_event"] = ev_high     # nombre del evento, o None
    return facts, []


def show_market():
    conn = _conn(); cur = conn.cursor()
    facts, notas = study_market(cur)
    cur.close(); conn.close()
    if facts is None:
        for n in notas:
            print(f"  ⛔ {n}")
        return 1
    print(f"\n  MARKET LEVEL ({len(facts)} fields):")
    for k in sorted(facts):
        v = facts[k]
        marker = "  ·None" if v is None else ""
        print(f"     {k:<22} {v}{marker}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# PASO 4 — PERSISTIR DOSSIER
# ══════════════════════════════════════════════════════════════════════════════

_HEADER_KEYS = {"ticker", "price", "sector"}


def _route(v):
    if v is None:
        return (None, None, None, True)
    if isinstance(v, bool):
        return (None, v, None, False)
    if isinstance(v, (int, float)):
        return (float(v), None, None, False)
    return (None, None, str(v)[:100], False)


def _fact_rows(study_id, facts):
    rows = []
    for k, v in facts.items():
        if k in _HEADER_KEYS:
            continue
        num, bl, txt, isnull = _route(v)
        rows.append((study_id, k, num, bl, txt, isnull))
    return rows


def _print_routing(rows):
    for _sid, crit, num, bl, txt, isnull in sorted(rows, key=lambda r: r[1]):
        if isnull:
            col = "is_null=TRUE"
        elif bl is not None:
            col = f"value_bool={bl}"
        elif num is not None:
            col = f"value_num={num}"
        else:
            col = f"value_text={txt!r}"
        print(f"     {crit:<22} {col}")


def write_study(cur, ticker, facts, regime, slot):
    from psycopg2.extras import execute_values
    cur.execute("""
        INSERT INTO ticker_study (ticker, scan_at, slot, price, sector, regime)
        VALUES (%s, NOW(), %s, %s, %s, %s) RETURNING id
    """, (ticker, slot, facts.get("price"), facts.get("sector"), regime))
    study_id = cur.fetchone()[0]
    rows = _fact_rows(study_id, facts)
    execute_values(cur, """
        INSERT INTO study_fact (study_id, criterion, value_num, value_bool, value_text, is_null)
        VALUES %s
    """, rows, page_size=500)
    return study_id, len(rows)


def write_studies_batch(cur, items, regime, slot):
    """
    Persiste MUCHOS sujetos en 2 statements (para el barrido): todas las cabeceras
    de un saque, luego todos los hechos de un saque. items: lista de (ticker, facts).

    Emparejamiento por TICKER, no por posicion: RETURNING id,ticker -> {ticker:id}.
    RETURNING no garantiza el orden de las filas, asi que mapear por posicion
    cruzaria hechos con el study_id equivocado. El ticker es unico dentro del scan.
    scan_at/created_at toman DEFAULT NOW(); NOW() es constante en la transaccion,
    asi que todas las cabeceras del scan comparten el mismo scan_at.
    """
    from psycopg2.extras import execute_values

    headers = [
        (tk, slot, facts.get("price"), facts.get("sector"), regime)
        for tk, facts in items
    ]
    filas = execute_values(cur, """
        INSERT INTO ticker_study (ticker, slot, price, sector, regime)
        VALUES %s RETURNING id, ticker
    """, headers, template="(%s,%s,%s,%s,%s)", page_size=1000, fetch=True)

    id_por_ticker = {tk: sid for sid, tk in filas}

    all_rows = []
    for tk, facts in items:
        sid = id_por_ticker.get(tk)
        if sid is None:
            continue                      # no deberia pasar (ticker unico en el scan)
        all_rows.extend(_fact_rows(sid, facts))

    execute_values(cur, """
        INSERT INTO study_fact (study_id, criterion, value_num, value_bool, value_text, is_null)
        VALUES %s
    """, all_rows, page_size=2000)
    return len(id_por_ticker), len(all_rows)


def persist_stock(ticker, commit):
    from universe import get_sp500_sectors
    sector_map = _try(get_sp500_sectors) or {}
    sector     = sector_map.get(ticker)
    meta       = fetch_metadata([ticker]).get(ticker, {})

    conn = _conn(); cur = conn.cursor()
    _ensure_study_tables(cur); conn.commit()
    mkt, _ = study_market(cur)
    regime = mkt.get("regime") if mkt else None

    df = _load_df(cur, ticker)
    if df is None:
        cur.close(); conn.close()
        print(f"  ⛔ {ticker} no esta en candle_daily.")
        return 1
    ret_spy    = _spy_return(cur, RS_WINDOW)
    ret_sector = _sector_return(cur, sector, sector_map, RS_WINDOW)
    facts, _   = study_stock(ticker, df, sector, ret_spy, ret_sector,
                             meta.get("metrics"), meta.get("is_etf"))

    rows = _fact_rows(0, facts)
    print(f"  scan regime: {regime}   ·   header: ticker={ticker} "
          f"price={facts.get('price')} sector={sector}")
    print(f"\n  ROUTING ({len(rows)} facts -> study_fact):")
    _print_routing(rows)

    if not commit:
        cur.close(); conn.close()
        print("\n  DRY RUN — nothing written.")
        return 0

    study_id, n = write_study(cur, ticker, facts, regime, slot="manual")
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM study_fact WHERE study_id = %s", (study_id,))
    n_db = cur.fetchone()[0]
    cur.close(); conn.close()
    print(f"\n  ✅ persisted: ticker_study.id={study_id}, {n_db} facts.")
    return 0


def persist_market(commit):
    conn = _conn(); cur = conn.cursor()
    _ensure_study_tables(cur); conn.commit()
    facts, notas = study_market(cur)
    if facts is None:
        cur.close(); conn.close()
        for n in notas:
            print(f"  ⛔ {n}")
        return 1
    fp = dict(facts)
    regime = fp.pop("regime", None)
    fp["price"] = fp.get("spy_price")
    rows = _fact_rows(0, fp)
    print(f"  scan regime: {regime}   ·   header: ticker={MARKET_TICKER} "
          f"price={fp.get('price')} sector=None")
    print(f"\n  ROUTING ({len(rows)} facts -> study_fact):")
    _print_routing(rows)
    if not commit:
        cur.close(); conn.close()
        print("\n  DRY RUN — nothing written.")
        return 0
    study_id, n = write_study(cur, MARKET_TICKER, fp, regime, slot="manual")
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM study_fact WHERE study_id = %s", (study_id,))
    n_db = cur.fetchone()[0]
    cur.close(); conn.close()
    print(f"\n  ✅ persisted market: ticker_study.id={study_id}, {n_db} facts.")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# PASO 4c — BARRIDO COMPLETO (1 -> 2 -> 3 -> 4)
# ══════════════════════════════════════════════════════════════════════════════

def scan_universe(tickers, commit, slot="scan"):
    from universe import get_sp500_sectors
    t_ini = time.time()

    # ── paso 1: refresco ──────────────────────────────────────────────────────
    print("  [1/4] candle refresh")
    complete, incomplete = refresh_candles(tickers, commit, verbose=False)
    print(f"        complete {len(complete)}/{len(tickers)}, incomplete {len(incomplete)}")
    if not complete:
        print("  ⛔ no complete ticker — nothing to study.")
        return 1

    # ── metadata TT en batch (para los completos) ─────────────────────────────
    print(f"  [2/4] TT metadata in batch ({len(complete)} tickers)")
    tm = time.time()
    meta = fetch_metadata(complete)
    print(f"        metrics + is_etf in {time.time()-tm:.1f}s")
    sector_map = _try(get_sp500_sectors) or {}

    conn = _conn(); cur = conn.cursor()
    _ensure_study_tables(cur); conn.commit()

    # ── paso 3: nivel mercado (una vez) ───────────────────────────────────────
    mkt, notas = study_market(cur)
    if mkt is None:
        cur.close(); conn.close()
        for n in notas:
            print(f"  ⛔ {n}")
        return 1
    regime = mkt.get("regime")
    print(f"  [3/4] market: regime={regime}, vix_level={mkt.get('vix_level')}")

    ret_spy        = _spy_return(cur, RS_WINDOW)
    sector_returns = _all_sector_returns(cur, sector_map, RS_WINDOW)

    # ── paso 2 + 4: por accion ────────────────────────────────────────────────
    print(f"  [4/4] facts + persistence per stock")
    dfs = _load_all_dfs(cur, complete)          # UNA query en vez de N _load_df
    tp = time.time()

    items = []
    for tk in complete:
        df = dfs.get(tk)
        if df is None:
            continue
        sector = sector_map.get(tk)
        facts, _ = study_stock(tk, df, sector, ret_spy, sector_returns.get(sector),
                               meta.get(tk, {}).get("metrics"), meta.get(tk, {}).get("is_etf"))
        items.append((tk, facts))

    # mercado como fila sintetica (un item mas)
    fp = dict(mkt); fp.pop("regime", None); fp["price"] = fp.get("spy_price")
    items.append((MARKET_TICKER, fp))

    if commit:
        n_subj, n_facts = write_studies_batch(cur, items, regime, slot)
        conn.commit()
        print(f"        persisted {n_subj} subjects ({n_subj-1} tickers + market), "
              f"{n_facts} facts  ({time.time()-tp:.1f}s)")
    else:
        print(f"        DRY RUN — nothing written ({len(items)} subjects)  ({time.time()-tp:.1f}s)")

    cur.close(); conn.close()
    print(f"\n  full scan in {time.time()-t_ini:.1f}s")
    if incomplete:
        print(f"  (incomplete, not studied: {list(incomplete)[:15]})")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Scanner v2 — Study layer")
    p.add_argument("--test", action="store_true", help="short list instead of the 500")
    p.add_argument("--commit", action="store_true", help="writes to the DB")
    p.add_argument("--facts", metavar="TICKER", help="step 2: facts of a ticker")
    p.add_argument("--market", action="store_true", help="step 3: market level")
    p.add_argument("--persist", metavar="TICKER", help="step 4a: persist a ticker")
    p.add_argument("--persist-market", action="store_true", dest="persist_market",
                   help="step 4b: persist the market")
    p.add_argument("--scan", action="store_true", help="step 4c: full scan (1->2->3->4)")
    a = p.parse_args()

    print(f"\n{'═'*55}")
    print(f"  SCANNER v2 · STUDY LAYER")
    print(f"{'═'*55}")
    env_state = "loaded" if _ENV_LOADED else "NOT found — using system env vars"
    print(f"  env: {_ENV_PATH}  ({env_state})")

    if a.scan:
        missing_vars = missing_credentials(commit=True)
        if missing_vars:
            print(f"  ⛔ missing variables: {', '.join(missing_vars)} (check {_ENV_PATH})")
            return 1
        tickers = get_universe(a.test)
        if not tickers:
            print("  ⛔ no universe — aborting.")
            return 1
        print(f"  SCAN · {len(tickers)} tickers · {'[COMMIT]' if a.commit else '[dry-run]'}")
        return scan_universe(tickers, a.commit)

    if a.market:
        if not os.getenv("DATABASE_URL"):
            print(f"  ⛔ missing DATABASE_URL (check {_ENV_PATH})")
            return 1
        print("  step 3 · market level")
        return show_market()

    if a.persist_market:
        if not os.getenv("DATABASE_URL"):
            print(f"  ⛔ missing DATABASE_URL (check {_ENV_PATH})")
            return 1
        print(f"  step 4b · persist market  {'[COMMIT]' if a.commit else '[dry-run]'}")
        return persist_market(a.commit)

    if a.persist:
        missing_vars = missing_credentials(commit=True)
        if missing_vars:
            print(f"  ⛔ missing variables: {', '.join(missing_vars)} (check {_ENV_PATH})")
            return 1
        canon = yahoo_to_canon(a.persist.upper())
        print(f"  step 4a · persist dossier · {canon}  {'[COMMIT]' if a.commit else '[dry-run]'}")
        return persist_stock(canon, a.commit)

    if a.facts:
        missing_vars = missing_credentials(commit=True)
        if missing_vars:
            print(f"  ⛔ missing variables: {', '.join(missing_vars)} (check {_ENV_PATH})")
            return 1
        canon = yahoo_to_canon(a.facts.upper())
        print(f"  step 2 · facts per stock · {canon}")
        return show_facts(canon)

    # por defecto: paso 1 (refresco de velas)
    print(f"  step 1 · candle refresh  {'[COMMIT]' if a.commit else '[dry-run]'}")
    missing_vars = missing_credentials(a.commit)
    if missing_vars:
        print(f"  ⛔ missing required variables: {', '.join(missing_vars)} (check {_ENV_PATH})")
        return 1
    tickers = get_universe(a.test)
    if not tickers:
        print("  ⛔ no universe — aborting.")
        return 1
    print(f"  universe: {len(tickers)} tickers")
    complete, incomplete = refresh_candles(tickers, a.commit)
    print(f"\n{'─'*55}")
    print(f"  complete (>= {MIN_CANDLES} candles in DB): {len(complete)}/{len(tickers)}")
    print(f"  incomplete: {len(incomplete)}")
    if incomplete:
        print(f"     {list(incomplete.items())[:15]}")
    print(f"{'─'*55}")
    return 0


if __name__ == "__main__":
    sys.exit(main())