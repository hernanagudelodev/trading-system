"""
trade.py
========
Position and account tracker — syncs with Tastytrade API.
Also supports paper trading for strategy validation.

Commands — Real positions:
    python trade.py --sync
    python trade.py --list
    python trade.py --account
    python trade.py --history

Commands — Paper trading:
    python trade.py --paper-buy CVS --strike-low 94 --strike-high 101 \
        --expiration 2026-07-02 --debit 2.62 \
        --context '{"ivp":27,"iv_rank":0.36,"rsi":50.6,"trend":17.1,"beta":0.6,"pcr":0.09,"vix":16.45,"spy_trend":4.0,"macro":"FAVORABLE"}' \
        --rationale "CVS mostró tendencia +17.1% con RSI 50.6 saludable..."
    python trade.py --paper-sync
    python trade.py --paper-list
    python trade.py --paper-history
    python trade.py --paper-close CVS

For real trades, context is saved via:
    python trade.py --save-context --position-id 5 \
        --context '{"ivp":27,...}' --rationale "..."
"""

import os
import sys
import json
import asyncio
from datetime import datetime, date
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# TASTYTRADE — fetch positions and balances
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_tastytrade_data():
    from tastytrade import Session
    from tastytrade.account import Account

    client_secret = os.getenv("TASTYTRADE_CLIENT_SECRET")
    refresh_token = os.getenv("TASTYTRADE_REFRESH_TOKEN")

    session  = Session(client_secret, refresh_token)
    accounts = await Account.get(session)
    account  = accounts[0]

    bal = await account.get_balances(session)
    balances = {
        "account_number":          account.account_number,
        "net_liquidating_value":   float(bal.net_liquidating_value or 0),
        "equity_buying_power":     float(bal.equity_buying_power or 0),
        "derivative_buying_power": float(bal.derivative_buying_power or 0),
        "cash_balance":            float(bal.cash_balance or 0),
        "pending_cash":            float(bal.pending_cash or 0),
        "long_derivative_value":   float(bal.long_derivative_value or 0),
        "maintenance_excess":      float(bal.maintenance_excess or 0),
    }

    raw_positions = await account.get_positions(session)
    positions = []

    for p in raw_positions:
        if p.instrument_type not in ("Equity Option",):
            continue

        parsed     = _parse_option_symbol(p.symbol)
        avg_open   = float(p.average_open_price or 0)
        quantity   = abs(int(p.quantity))
        multiplier = float(p.multiplier or 100)
        cost_basis = round(avg_open * quantity * multiplier, 2)
        close_px   = float(p.close_price or 0)
        mark       = float(p.mark or close_px)

        positions.append({
            "symbol":             p.symbol,
            "ticker":             parsed["ticker"],
            "expiration":         parsed["expiration"],
            "strike":             parsed["strike"],
            "option_type":        parsed["option_type"],
            "quantity":           int(p.quantity),
            "quantity_direction": str(p.quantity_direction),
            "avg_open_price":     avg_open,
            "mark":               mark,
            "close_price":        close_px,
            "cost_basis":         cost_basis,
            "market_value":       round(mark * quantity * multiplier, 2),
            "unrealized_pnl":     0.0,
        })

    return {
        "account_number": account.account_number,
        "balances":        balances,
        "positions":       positions,
    }


def _parse_option_symbol(symbol):
    import re
    clean = symbol.lstrip(".").replace(" ", "")

    pattern = r'^([A-Z]+)(\d{6})([CP])(\d{8})$'
    m = re.match(pattern, clean)
    if m:
        ticker   = m.group(1)
        date_str = m.group(2)
        opt_type = "CALL" if m.group(3) == "C" else "PUT"
        strike   = int(m.group(4)) / 1000.0
        try:
            exp_date = datetime.strptime(date_str, "%y%m%d").date()
        except Exception:
            exp_date = None
        return {"ticker": ticker, "expiration": exp_date,
                "option_type": opt_type, "strike": strike}

    pattern2 = r'^([A-Z]+)(\d{6})([CP])(\d+(?:\.\d+)?)$'
    m2 = re.match(pattern2, clean)
    if m2:
        ticker   = m2.group(1)
        date_str = m2.group(2)
        opt_type = "CALL" if m2.group(3) == "C" else "PUT"
        strike   = float(m2.group(4))
        try:
            exp_date = datetime.strptime(date_str, "%y%m%d").date()
        except Exception:
            exp_date = None
        return {"ticker": ticker, "expiration": exp_date,
                "option_type": opt_type, "strike": strike}

    return {"ticker": clean[:20], "expiration": None,
            "option_type": "CALL", "strike": 0.0}


def fetch_tastytrade_data():
    return asyncio.run(_fetch_tastytrade_data())


# ══════════════════════════════════════════════════════════════════════════════
# SPREAD GROUPING
# ══════════════════════════════════════════════════════════════════════════════

def _nombre_estrategia(option_type, long_strike, short_strike):
    """
    El nombre sale de la ESTRUCTURA, no de una suposición.

        CALL, long < short -> Bull Call Spread   (débito)
        CALL, long > short -> Bear Call Spread   (crédito)
        PUT,  long < short -> Bull Put Spread    (crédito)
        PUT,  long > short -> Bear Put Spread    (débito)

    Antes estaba clavado en "Bull Call Spread". Derivarlo significa que un
    spread bajista abierto a mano se etiqueta bien en vez de mentir, y que
    cuando entre lo bajista a paper esto ya lo entiende.
    """
    alcista = float(long_strike) < float(short_strike)
    if str(option_type).upper() == "CALL":
        return "Bull Call Spread" if alcista else "Bear Call Spread"
    return "Bull Put Spread" if alcista else "Bear Put Spread"


def group_spreads(tt_positions):
    """
    Agrupa las patas crudas de Tastytrade en spreads verticales de 2 patas.

    EL BUG QUE ESTO ARREGLA
        La versión anterior sólo miraba CALLS:
            if pos["option_type"] == "CALL":
        Un Bull Put Spread —27 de tus 43 trades, la estrategia mayoritaria—
        nunca se agrupaba: las dos patas caían en `singles` y quedaban sueltas.
        O sea que un BPS abierto en vivo viviría en el broker y jamás en
        `positions`: el monitor no lo vería y no tendría stop loss.

    LA MATEMÁTICA YA SERVÍA
        net_debit = long.avg_open - short.avg_open
        En un BPS el long es el strike BAJO (put barato) y el short el ALTO
        (put caro), así que da NEGATIVO = crédito — exactamente la convención
        del sistema. No hubo que inventar nada: faltaban el filtro y el nombre.
    """
    spreads = []
    singles = []
    used    = set()

    # Se agrupa por (ticker, expiración, tipo). Antes la clave no llevaba el
    # tipo porque sólo entraban calls; ahora sin él un call y un put del mismo
    # ticker y expiración se emparejarían entre sí.
    por_clave = {}
    for i, pos in enumerate(tt_positions):
        tipo = str(pos.get("option_type", "")).upper()
        if tipo not in ("CALL", "PUT"):
            continue
        key = (pos["ticker"], str(pos["expiration"]), tipo)
        por_clave.setdefault(key, []).append((i, pos))

    for (ticker, exp, tipo), legs in por_clave.items():
        if len(legs) != 2:
            continue

        idx_a, leg_a = legs[0]
        idx_b, leg_b = legs[1]

        if leg_a.get("quantity_direction") == "Long":
            long_leg, short_leg = leg_a, leg_b
            long_idx, short_idx = idx_a, idx_b
        elif leg_b.get("quantity_direction") == "Long":
            long_leg, short_leg = leg_b, leg_a
            long_idx, short_idx = idx_b, idx_a
        elif leg_a["strike"] < leg_b["strike"]:
            # Sin dirección: se asume alcista (long = strike bajo), que es lo
            # único que el sistema abre. Un bajista manual caería mal acá.
            long_leg, short_leg = leg_a, leg_b
            long_idx, short_idx = idx_a, idx_b
        else:
            long_leg, short_leg = leg_b, leg_a
            long_idx, short_idx = idx_b, idx_a

        net_debit  = round(long_leg["avg_open_price"] - short_leg["avg_open_price"], 2)
        contracts  = abs(long_leg["quantity"])
        total_cost = round(net_debit * contracts * 100, 2)

        # strike_low/high son NUMÉRICOS, no "el del long / el del short".
        # En un bear call spread el long es el strike ALTO: asumir lo contrario
        # invertía los campos, y position_max_loss y pricing esperan low < high.
        s_long, s_short = float(long_leg["strike"]), float(short_leg["strike"])
        strike_low, strike_high = min(s_long, s_short), max(s_long, s_short)

        spreads.append({
            "type":                    _nombre_estrategia(tipo, s_long, s_short),
            "option_type":             tipo,
            "ticker":                  long_leg["ticker"],
            "expiration":              long_leg["expiration"],
            "strike_low":              strike_low,
            "strike_high":             strike_high,
            "contracts":               contracts,
            "premium_paid":            net_debit,     # >0 débito · <0 crédito
            "total_cost":              total_cost,
            "avg_open_long":           long_leg["avg_open_price"],
            "avg_open_short":          short_leg["avg_open_price"],
            "symbol_long":             long_leg["symbol"],
            "symbol_short":            short_leg["symbol"],
            "tastytrade_symbol":       long_leg["symbol"],
            "tastytrade_symbol_short": short_leg["symbol"],
        })
        used.add(long_idx)
        used.add(short_idx)

    for i, pos in enumerate(tt_positions):
        if i not in used:
            singles.append(pos)

    return spreads, singles


# ══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_db_connection():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def ensure_tables():
    conn = get_db_connection()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id                      SERIAL PRIMARY KEY,
            account_number          VARCHAR(20),
            net_liquidating_value   DECIMAL(12,2),
            equity_buying_power     DECIMAL(12,2),
            derivative_buying_power DECIMAL(12,2),
            cash_balance            DECIMAL(12,2),
            pending_cash            DECIMAL(12,2),
            long_derivative_value   DECIMAL(12,2),
            maintenance_excess      DECIMAL(12,2),
            snapshot_at             TIMESTAMP DEFAULT NOW(),
            created_at              TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        ALTER TABLE positions
        ADD COLUMN IF NOT EXISTS tastytrade_symbol       VARCHAR(50),
        ADD COLUMN IF NOT EXISTS tastytrade_symbol_short VARCHAR(50),
        ADD COLUMN IF NOT EXISTS broker                  VARCHAR(20) DEFAULT 'tastytrade',
        ADD COLUMN IF NOT EXISTS option_type             VARCHAR(10)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS paper_positions (
            id                  SERIAL PRIMARY KEY,
            ticker              VARCHAR(20)     NOT NULL,
            strategy            VARCHAR(50)     NOT NULL,
            strike_low          DECIMAL(10,2),
            strike_high         DECIMAL(10,2),
            contracts           INTEGER         NOT NULL DEFAULT 1,
            expiration          DATE,
            premium_paid        DECIMAL(10,2),
            total_cost          DECIMAL(10,2),
            price_at_open       DECIMAL(10,2),
            opened_at           TIMESTAMP       DEFAULT NOW(),
            current_spread_value DECIMAL(10,2),
            current_value        DECIMAL(10,2),
            gross_pnl            DECIMAL(10,2),
            pnl_pct              DECIMAL(10,4),
            profit_pct_of_max    DECIMAL(10,4),
            last_synced_at       TIMESTAMP,
            premium_received    DECIMAL(10,2),
            total_received      DECIMAL(10,2),
            closed_at           TIMESTAMP,
            price_at_close      DECIMAL(10,2),
            status              VARCHAR(20)     DEFAULT 'OPEN',
            close_reason        VARCHAR(50),
            notes               TEXT,
            created_at          TIMESTAMP       DEFAULT NOW()
        )
    """)

    # trade_context — snapshot of market conditions at entry
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_context (
            id                  SERIAL PRIMARY KEY,

            -- Link to real or paper position (one of the two, not both)
            position_id         INTEGER REFERENCES positions(id),
            paper_position_id   INTEGER REFERENCES paper_positions(id),

            -- Stock criteria at signal moment
            price_at_signal     DECIMAL(10,2),
            trend_25d_pct       DECIMAL(6,2),
            rsi                 DECIMAL(6,2),
            above_sma50         BOOLEAN,
            sma50_rising        BOOLEAN,
            candle_pattern      VARCHAR(30),

            -- Volatility
            iv                  DECIMAL(6,2),
            iv_percentile       DECIMAL(6,2),
            iv_rank             DECIMAL(6,4),
            hv_30d              DECIMAL(6,2),
            beta                DECIMAL(6,2),
            put_call_ratio      DECIMAL(6,3),
            open_interest       INTEGER,

            -- Strategy
            strategy_selected   VARCHAR(30),
            strategy_reason     VARCHAR(20),

            -- Macro context at entry
            vix                 DECIMAL(6,2),
            spy_trend_25d       DECIMAL(6,2),
            macro_verdict       VARCHAR(20),

            -- Claude qualitative analysis
            claude_rationale    TEXT,

            created_at          TIMESTAMP DEFAULT NOW()
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_open_positions_from_db():
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, ticker, strategy, strike_low, strike_high,
               expiration, contracts, total_cost, tastytrade_symbol,
               tastytrade_symbol_short, broker, opened_at
        FROM positions
        WHERE UPPER(status) = 'OPEN'
        ORDER BY opened_at DESC
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def save_account_snapshot(balances):
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO account_snapshots
            (account_number, net_liquidating_value, equity_buying_power,
             derivative_buying_power, cash_balance, pending_cash,
             long_derivative_value, maintenance_excess)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        balances["account_number"],
        balances["net_liquidating_value"],
        balances["equity_buying_power"],
        balances["derivative_buying_power"],
        balances["cash_balance"],
        balances["pending_cash"],
        balances["long_derivative_value"],
        balances["maintenance_excess"],
    ))
    snapshot_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return snapshot_id


def insert_spread(spread, account_number):
    conn = get_db_connection()
    cur  = conn.cursor()
    notes = (
        f"Bull Call Spread ${spread['strike_low']}/${spread['strike_high']} "
        f"exp {spread['expiration']}. "
        f"Long avg: ${spread['avg_open_long']:.2f} | "
        f"Short avg: ${spread['avg_open_short']:.2f} | "
        f"Net debit: ${spread['premium_paid']:.2f}"
    )
    cur.execute("""
        INSERT INTO positions
            (ticker, strategy, broker, is_paper,
             strike_low, strike_high, contracts, expiration,
             premium_paid, total_cost,
             status, opened_at, price_at_open,
             tastytrade_symbol, tastytrade_symbol_short,
             option_type, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'OPEN', NOW(), 0.0, %s, %s, %s, %s)
        RETURNING id
    """, (
        spread["ticker"], spread["type"], "tastytrade", False,
        spread["strike_low"], spread["strike_high"],
        spread["contracts"], spread["expiration"],
        spread["premium_paid"], spread["total_cost"],
        spread["tastytrade_symbol"], spread["tastytrade_symbol_short"],
        "CALL", notes,
    ))
    pos_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return pos_id


def insert_position(tt_pos, account_number):
    conn = get_db_connection()
    cur  = conn.cursor()
    qty = tt_pos["quantity"]
    if tt_pos["option_type"] == "CALL" and qty > 0:
        strategy = "Long Call"
    elif tt_pos["option_type"] == "CALL" and qty < 0:
        strategy = "Short Call"
    elif tt_pos["option_type"] == "PUT" and qty > 0:
        strategy = "Long Put"
    else:
        strategy = "Short Put"

    notes = f"Auto-imported from Tastytrade. Avg open: ${tt_pos['avg_open_price']:.2f}"
    cur.execute("""
        INSERT INTO positions
            (ticker, strategy, broker, is_paper,
             strike_low, strike_high, contracts, expiration,
             premium_paid, total_cost,
             status, opened_at, price_at_open,
             tastytrade_symbol, option_type, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'OPEN', NOW(), 0.0, %s, %s, %s)
        RETURNING id
    """, (
        tt_pos["ticker"], strategy, "tastytrade", False,
        tt_pos["strike"], tt_pos["strike"],
        abs(tt_pos["quantity"]), tt_pos["expiration"],
        tt_pos["avg_open_price"],
        abs(tt_pos["avg_open_price"]) * abs(tt_pos["quantity"]) * 100,
        tt_pos["symbol"], tt_pos["option_type"], notes,
    ))
    pos_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return pos_id


def close_position_in_db(db_pos_id, close_price, close_reason="Closed in Tastytrade"):
    """
    Marca una posición real como cerrada.

    close_price : prima RECIBIDA por acción al cerrar (float), o None si no se
                  conoce. NO se inventa. None -> el P&L queda NULL.
    Devuelve el gross_pnl, o None si no se pudo calcular.

    EL BUG QUE ESTO ARREGLA — 17-jul, con plata real
        La firma anterior recibía `tt_pos` y hacía:
            close_price = tt_pos["mark"] if tt_pos else 0.0
        Pero run_sync SIEMPRE pasa None: detecta el cierre justamente porque la
        posición YA NO ESTÁ en Tastytrade — no hay `mark` que consultar. O sea
        que el `else 0.0` no era un fallback: era el único camino.
        Resultado real: CCL abrió a 0.44, cerró a 0.43 (P&L real -$1, NLV -$3.50
        con comisiones) y la DB registró P&L = -$44.00. La pérdida máxima entera.
        Un error de 44x, sin excepción, sin aviso, con números plausibles.

        Es el mismo `0.0` que ya cazaste en cmd_paper_close y corregiste ahí.
        Esta copia quedó viva porque nadie había cerrado una posición REAL nunca.

    POR QUÉ NULL Y NO 0
        Que el broker no la tenga es un HECHO: se marca CLOSED. Cuánto se ganó
        NO se sabe: se marca NULL. Un 0 y un -44 son los dos mentiras; NULL es
        la verdad. Sin dato real -> None, nunca 0.0 (§10).

    DE DÓNDE SALE EL PRECIO
        Sólo lo sabe quien ejecutó el cierre, en el momento del fill
        (LiveExecutor lo tiene en r.fill_price). run_sync corre después, cuando
        la posición ya desapareció y el dato se perdió. Un cierre sincronizado
        a posteriori queda como CLOSED_PRICE_UNKNOWN, a propósito.
    """
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT total_cost, contracts, premium_paid
        FROM positions WHERE id = %s
    """, (db_pos_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return None

    total_cost, contracts, premium_paid = row
    total_cost = float(total_cost or 0)
    contracts  = int(contracts or 1)

    # ── Sin precio: se cierra, pero NO se inventa el P&L ──────────────────────
    if close_price is None:
        cur.execute("""
            UPDATE positions SET
                status           = 'CLOSED',
                closed_at        = NOW(),
                premium_received = NULL,
                total_received   = NULL,
                gross_pnl        = NULL,
                net_pnl          = NULL,
                pnl_pct          = NULL,
                close_reason     = %s
            WHERE id = %s
        """, ("CLOSED_PRICE_UNKNOWN", db_pos_id))
        conn.commit()
        cur.close()
        conn.close()
        return None

    close_price    = float(close_price)
    total_received = round(close_price * contracts * 100, 2)
    gross_pnl      = round(total_received - total_cost, 2)
    pnl_pct        = round(gross_pnl / total_cost * 100, 2) if total_cost else None

    cur.execute("""
        UPDATE positions SET
            status           = 'CLOSED',
            closed_at        = NOW(),
            premium_received = %s,
            total_received   = %s,
            gross_pnl        = %s,
            net_pnl          = %s,
            pnl_pct          = %s,
            close_reason     = %s
        WHERE id = %s
    """, (close_price, total_received, gross_pnl, gross_pnl, pnl_pct,
          close_reason, db_pos_id))
    conn.commit()
    cur.close()
    conn.close()
    return gross_pnl


# ══════════════════════════════════════════════════════════════════════════════
# TRADE CONTEXT — save market snapshot at entry
# ══════════════════════════════════════════════════════════════════════════════

def save_trade_context(position_id=None, paper_position_id=None,
                       context_json=None, rationale=None):
    """
    Save market conditions snapshot at the moment of trade entry.

    context_json: dict or JSON string with keys:
        price, trend, rsi, above_sma50, sma50_rising, candle,
        iv, ivp, iv_rank, hv, beta, pcr, oi,
        strategy, strategy_reason,
        vix, spy_trend, macro

    rationale: Claude's qualitative analysis (free text paragraph)
    """
    if position_id is None and paper_position_id is None:
        print("  ERROR: must provide position_id or paper_position_id")
        return None

    if context_json is None:
        context_json = {}
    if isinstance(context_json, str):
        try:
            context_json = json.loads(context_json)
        except Exception:
            print("  WARNING: could not parse context JSON")
            context_json = {}

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO trade_context (
            position_id, paper_position_id,
            price_at_signal, trend_25d_pct, rsi, above_sma50, sma50_rising, candle_pattern,
            iv, iv_percentile, iv_rank, hv_30d, beta, put_call_ratio, open_interest,
            strategy_selected, strategy_reason,
            vix, spy_trend_25d, macro_verdict,
            claude_rationale
        ) VALUES (
            %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s,
            %s
        ) RETURNING id
    """, (
        position_id, paper_position_id,
        context_json.get("price"),
        context_json.get("trend"),
        context_json.get("rsi"),
        context_json.get("above_sma50"),
        context_json.get("sma50_rising"),
        context_json.get("candle"),
        context_json.get("iv"),
        context_json.get("ivp"),
        context_json.get("iv_rank"),
        context_json.get("hv"),
        context_json.get("beta"),
        context_json.get("pcr"),
        context_json.get("oi"),
        context_json.get("strategy"),
        context_json.get("strategy_reason"),
        context_json.get("vix"),
        context_json.get("spy_trend"),
        context_json.get("macro"),
        rationale,
    ))
    ctx_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return ctx_id


# ══════════════════════════════════════════════════════════════════════════════
# PAPER TRADING — spread value fetch (delega en pricing.py, fuente única)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_paper_spread_value(ticker, strike_low, strike_high, expiration,
                              option_type="call", retries=3, delay=2):
    """Delegado a pricing.get_spread_value — la lógica vive en un solo lugar."""
    import pricing
    return pricing.get_spread_value(ticker, strike_low, strike_high, expiration,
                                    option_type=option_type, retries=retries, delay=delay)


# ══════════════════════════════════════════════════════════════════════════════
# PAPER TRADING — commands
# ══════════════════════════════════════════════════════════════════════════════

def _read_context_from_reports(ticker):
    """
    Read market context and scanner criteria for a ticker from the
    generated report files (market_context.json + scanner_report.md).

    Returns dict with all available fields, or {} if files not found.
    """
    import re

    base_dir      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ctx_path      = os.path.join(base_dir, "reports", "market_context.json")
    scanner_path  = os.path.join(base_dir, "reports", "scanner_report.md")
    context       = {}

    # ── market_context.json ───────────────────────────────────────────────────
    if os.path.exists(ctx_path):
        try:
            with open(ctx_path) as f:
                mctx = json.load(f)
            vix = mctx.get("vix", {})
            spy = mctx.get("spy", {})
            context["vix"]        = vix.get("current")
            context["spy_trend"]  = spy.get("pct_25d")
            context["macro"]      = mctx.get("verdict")
        except Exception:
            pass

    # ── scanner_report.md ─────────────────────────────────────────────────────
    if os.path.exists(scanner_path):
        try:
            with open(scanner_path, encoding="utf-8") as f:
                content = f.read()

            # Find the section for this ticker
            pattern = rf"### {re.escape(ticker)} — \$([0-9.]+)(.*?)(?=### [A-Z]|\Z)"
            match   = re.search(pattern, content, re.DOTALL)

            if match:
                price_str    = match.group(1)
                section      = match.group(2)
                context["price"] = float(price_str)

                # Trend
                m = re.search(r"Trend 25d:.*?(BULLISH|BEARISH).*?([+-]?\d+\.?\d*)%", section)
                if m:
                    context["trend"] = float(m.group(2)) * (1 if m.group(1) == "BULLISH" else -1)

                # RSI
                m = re.search(r"RSI:\s*([0-9.]+)", section)
                if m:
                    context["rsi"] = float(m.group(1))

                # SMA
                context["above_sma50"]  = "Above" in section and "SMA50" in section
                context["sma50_rising"] = "RISING" in section

                # Candle
                m = re.search(r"Candle:\s*(.+)", section)
                if m:
                    context["candle"] = m.group(1).strip()

                # IV
                m = re.search(r"IV:\s*([0-9.]+)%\s*\(P([0-9.]+)\s*/\s*Rank\s*([0-9.]+)\)", section)
                if m:
                    context["iv"]      = float(m.group(1))
                    context["ivp"]     = float(m.group(2))
                    context["iv_rank"] = float(m.group(3))

                # HV
                m = re.search(r"HV 30d:\s*([0-9.]+)%", section)
                if m:
                    context["hv"] = float(m.group(1))

                # Beta
                m = re.search(r"Beta:\s*([0-9.]+)", section)
                if m:
                    context["beta"] = float(m.group(1))

                # Put/Call
                m = re.search(r"Put/Call:\s*([0-9.]+)", section)
                if m:
                    context["pcr"] = float(m.group(1))

                # OI
                m = re.search(r"OI \(ATM\):\s*([\d,]+)", section)
                if m:
                    context["oi"] = int(m.group(1).replace(",", ""))

        except Exception as e:
            print(f"  WARNING: could not parse scanner report for {ticker}: {e}")

    return context


def cmd_paper_buy(ticker, strike_low, strike_high, expiration_str, debit,
                  notes=None, context_json=None, rationale=None):
    """
    Register a new paper spread position and save trade context.
    Debit > 0 → Bull Call Spread
    Debit < 0 → Bull Put Spread (credit received)
    """
    ensure_tables()

    try:
        expiration = datetime.strptime(expiration_str, "%Y-%m-%d").date()
    except ValueError:
        print(f"  ERROR: invalid expiration format '{expiration_str}' — use YYYY-MM-DD")
        return

    spread_width = strike_high - strike_low

    if debit < 0:
        strategy   = "Bull Put Spread"
        net_credit = abs(debit)
        total_cost = round(-net_credit * 100, 2)
        max_profit = round(net_credit * 100, 2)
        max_loss   = round((spread_width - net_credit) * 100, 2)
        auto_notes = (
            f"Paper trade — Bull Put Spread ${strike_low}/${strike_high} "
            f"exp {expiration}. Crédito: ${net_credit:.2f}. "
            f"Max profit: ${max_profit:.2f}. Max loss: -${max_loss:.2f}."
        )
    else:
        strategy   = "Bull Call Spread"
        total_cost = round(debit * 100, 2)
        max_profit = round((spread_width - debit) * 100, 2)
        auto_notes = (
            f"Paper trade — Bull Call Spread ${strike_low}/${strike_high} "
            f"exp {expiration}. Debit: ${debit:.2f}. Max profit: ${max_profit:.2f}."
        )

    if notes:
        auto_notes += f" | {notes}"

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO paper_positions
            (ticker, strategy, strike_low, strike_high, contracts,
             expiration, premium_paid, total_cost, price_at_open,
             status, opened_at, notes)
        VALUES (%s, %s, %s, %s, 1, %s, %s, %s, 0.0, 'OPEN', NOW(), %s)
        RETURNING id
    """, (
        ticker, strategy,
        strike_low, strike_high,
        expiration, debit, total_cost,
        auto_notes,
    ))
    pos_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    if debit < 0:
        print(f"\n  ✅ Paper position opened ({strategy}):")
        print(f"     {ticker} Bull Put Spread ${strike_low}/${strike_high}")
        print(f"     Exp: {expiration} | Crédito: ${abs(debit):.2f} | Max profit: ${max_profit:.2f} | Max loss: -${max_loss:.2f}")
    else:
        print(f"\n  ✅ Paper position opened ({strategy}):")
        print(f"     {ticker} Bull Call Spread ${strike_low}/${strike_high}")
        print(f"     Exp: {expiration} | Debit: ${debit:.2f} | Cost: ${total_cost:.2f} | Max profit: ${max_profit:.2f}")
    print(f"     DB id: {pos_id}")

    # Auto-read context from reports if not provided
    if context_json is None:
        context_json = _read_context_from_reports(ticker)
        if context_json:
            print(f"     Context auto-loaded from reports ✅")

    # Add strategy info to context
    if isinstance(context_json, dict):
        context_json["strategy"] = strategy
        context_json["strategy_reason"] = "IV_HIGH" if debit < 0 else "IV_LOW"

    # Save trade context
    if context_json or rationale:
        ctx_id = save_trade_context(
            paper_position_id=pos_id,
            context_json=context_json,
            rationale=rationale
        )
        if ctx_id:
            print(f"     Context saved (id: {ctx_id}) ✅")
    print()


def cmd_paper_sync():
    """Update P&L of all open paper positions using real Tastytrade prices."""
    ensure_tables()

    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, ticker, strategy, strike_low, strike_high,
               expiration, contracts, total_cost, premium_paid
        FROM paper_positions
        WHERE UPPER(status) = 'OPEN'
        ORDER BY opened_at
    """)
    cols      = [d[0] for d in cur.description]
    positions = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()

    if not positions:
        print("\n  No open paper positions to sync.\n")
        return

    print(f"\n{'=' * 55}")
    print(f"  PAPER SYNC — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 55}\n")
    print(f"  Open paper positions: {len(positions)}\n")

    for pos in positions:
        ticker      = pos["ticker"]
        strike_low  = float(pos["strike_low"])
        strike_high = float(pos["strike_high"])
        total_cost  = float(pos["total_cost"])
        premium     = float(pos["premium_paid"])
        contracts   = int(pos["contracts"])
        expiration  = pos["expiration"]
        strategy    = pos.get("strategy", "Bull Call Spread")
        is_put      = strategy == "Bull Put Spread"
        dte         = (expiration - date.today()).days

        print(f"  {ticker} ${strike_low}/{strike_high} (DTE: {dte})...", end=" ", flush=True)

        opt_type = "put" if is_put else "call"
        spread_value = fetch_paper_spread_value(ticker, strike_low, strike_high,
                                                 expiration, opt_type)

        if spread_value is None:
            print("no data")
            continue

        spread_width = strike_high - strike_low

        if is_put:
            net_credit    = abs(premium)
            max_profit    = round(net_credit * contracts * 100, 2)
            cost_to_close = round(spread_value * contracts * 100, 2)
            current_value = cost_to_close
            gross_pnl     = round(max_profit - cost_to_close, 2)
            pnl_pct       = round(gross_pnl / max_profit * 100, 2) if max_profit else 0
            profit_pct_max = round(gross_pnl / max_profit, 4) if max_profit else 0
        else:
            max_profit     = round((spread_width - premium) * contracts * 100, 2)
            current_value  = round(spread_value * contracts * 100, 2)
            gross_pnl      = round(current_value - total_cost, 2)
            pnl_pct        = round(gross_pnl / total_cost * 100, 2) if total_cost else 0
            profit_pct_max = round(gross_pnl / max_profit, 4) if max_profit else 0

        print(f"spread=${spread_value:.2f} | P&L ${gross_pnl:+.2f} ({pnl_pct:+.1f}%)")

        # NOTA: el cierre por stop/target/DTE lo hace AHORA el worker
        # (run_paper_monitor, intradía). Aquí solo se actualiza P&L para el
        # reporte del auto_run — para no tener dos procesos cerrando la misma fila.
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE paper_positions SET
                current_spread_value = %s, current_value = %s,
                gross_pnl = %s, pnl_pct = %s, profit_pct_of_max = %s,
                last_synced_at = NOW()
            WHERE id = %s
        """, (spread_value, current_value, gross_pnl, pnl_pct,
              profit_pct_max, pos["id"]))

        conn.commit()
        cur.close()
        conn.close()

    print(f"\n{'=' * 55}")
    print(f"  Paper sync complete.\n")


def cmd_paper_list():
    ensure_tables()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, ticker, strategy, strike_low, strike_high, expiration,
               premium_paid, total_cost, current_spread_value,
               gross_pnl, pnl_pct, profit_pct_of_max, last_synced_at
        FROM paper_positions
        WHERE UPPER(status) = 'OPEN'
        ORDER BY opened_at
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()

    print(f"\n  Open paper positions: {len(rows)}\n")
    if not rows:
        return

    print(f"  {'#':<4} {'Ticker':<6} {'Spread':<14} {'Exp':<12} "
          f"{'Cost':>7} {'P&L':>8} {'%Max':>7} {'Synced':<16}")
    print(f"  {'-'*4} {'-'*6} {'-'*14} {'-'*12} {'-'*7} {'-'*8} {'-'*7} {'-'*16}")

    for r in rows:
        exp     = str(r["expiration"])[:10]
        cost    = float(r["total_cost"] or 0)
        pnl     = float(r["gross_pnl"] or 0) if r["gross_pnl"] else 0
        pnl_pct = float(r["pnl_pct"] or 0) if r["pnl_pct"] else 0
        pmax    = float(r["profit_pct_of_max"] or 0) * 100 if r["profit_pct_of_max"] else 0
        synced  = str(r["last_synced_at"])[:16] if r["last_synced_at"] else "never"
        spread  = f"${r['strike_low']}/{r['strike_high']}"
        sign    = "+" if pnl >= 0 else ""
        print(f"  {r['id']:<4} {r['ticker']:<6} {spread:<14} {exp:<12} "
              f"${cost:>6.0f} {sign}${pnl:>6.0f} {pmax:>6.1f}% {synced}")
    print()


def cmd_paper_history():
    ensure_tables()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, ticker, strike_low, strike_high, expiration,
               premium_paid, total_cost, gross_pnl, pnl_pct,
               profit_pct_of_max, close_reason, closed_at
        FROM paper_positions
        WHERE UPPER(status) = 'CLOSED'
        ORDER BY closed_at DESC
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()

    print(f"\n  Paper trading history: {len(rows)} closed positions\n")
    if not rows:
        return

    wins = sum(1 for r in rows if float(r["gross_pnl"] or 0) > 0)
    losses = len(rows) - wins

    for r in rows:
        pnl     = float(r["gross_pnl"] or 0)
        pnl_pct = float(r["pnl_pct"] or 0)
        pmax    = float(r["profit_pct_of_max"] or 0) * 100 if r["profit_pct_of_max"] else 0
        cost    = float(r["total_cost"] or 0)
        closed  = str(r["closed_at"])[:10] if r["closed_at"] else "N/A"
        reason  = r["close_reason"] or "MANUAL"
        sign    = "✅" if pnl >= 0 else "❌"
        print(f"  {sign} #{r['id']} {r['ticker']} "
              f"${r['strike_low']}/{r['strike_high']} | "
              f"Cost ${cost:.0f} | P&L ${pnl:+.0f} ({pnl_pct:+.1f}%) | "
              f"{pmax:.0f}% of max | {reason} | {closed}")

    print(f"\n  Summary: {wins} wins / {losses} losses / {len(rows)} total")
    if rows:
        avg_pnl = sum(float(r["gross_pnl"] or 0) for r in rows) / len(rows)
        print(f"  Avg P&L per trade: ${avg_pnl:+.0f}")
    print()


def cmd_paper_close(ticker):
    ensure_tables()
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT id, ticker, strategy, strike_low, strike_high, expiration,
               total_cost, premium_paid, contracts
        FROM paper_positions
        WHERE UPPER(status) = 'OPEN' AND ticker = %s
        ORDER BY opened_at DESC LIMIT 1
    """, (ticker.upper(),))
    row = cur.fetchone()

    if not row:
        print(f"\n  No open paper position found for {ticker}\n")
        cur.close()
        conn.close()
        return False

    pos_id, ticker, strategy, sl, sh, exp, total_cost, premium, contracts = row
    is_put = strategy == "Bull Put Spread"
    opt_type = "put" if is_put else "call"

    # Opción C: reintenta fuerte; si no consigue precio real, NO cierra.
    # Nunca inventa $0.00 (eso falseaba el P&L — caso GS del 16-jun).
    spread_value = fetch_paper_spread_value(ticker, float(sl), float(sh), exp, opt_type,
                                            retries=4, delay=3)
    if spread_value is None:
        print(f"\n  ⛔ NO se cerró {ticker}: no se pudo obtener precio real del "
              f"spread tras varios intentos.")
        print(f"     La posición sigue ABIERTA. Reintentá con el mercado abierto, "
              f"o cerrá con un precio conocido.\n")
        cur.close()
        conn.close()
        return False

    total_cost   = float(total_cost)
    premium      = float(premium)
    contracts    = int(contracts)
    spread_width = float(sh) - float(sl)

    if is_put:
        net_credit     = abs(premium)
        max_profit     = round(net_credit * contracts * 100, 2)
        cost_to_close  = round(spread_value * contracts * 100, 2)
        current_value  = cost_to_close
        gross_pnl      = round(max_profit - cost_to_close, 2)
        pnl_pct        = round(gross_pnl / max_profit * 100, 2) if max_profit else 0
        profit_pct_max = round(gross_pnl / max_profit, 4) if max_profit else 0
    else:
        max_profit     = round((spread_width - premium) * contracts * 100, 2)
        current_value  = round(spread_value * contracts * 100, 2)
        gross_pnl      = round(current_value - total_cost, 2)
        pnl_pct        = round(gross_pnl / total_cost * 100, 2) if total_cost else 0
        profit_pct_max = round(gross_pnl / max_profit, 4) if max_profit else 0

    cur.execute("""
        UPDATE paper_positions SET
            status = 'CLOSED', closed_at = NOW(),
            current_spread_value = %s, current_value = %s,
            premium_received = %s, total_received = %s,
            gross_pnl = %s, pnl_pct = %s, profit_pct_of_max = %s,
            close_reason = 'MANUAL', last_synced_at = NOW()
        WHERE id = %s
    """, (spread_value, current_value, spread_value, current_value,
          gross_pnl, pnl_pct, profit_pct_max, pos_id))

    conn.commit()
    cur.close()
    conn.close()

    sign = "✅" if gross_pnl >= 0 else "❌"
    print(f"\n  {sign} Paper position closed:")
    print(f"     {ticker} ({strategy}) ${sl}/{sh} | "
          f"spread=${spread_value:.2f} | P&L ${gross_pnl:+.2f} ({pnl_pct:+.1f}%)\n")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# REAL TRADING — sync logic
# ══════════════════════════════════════════════════════════════════════════════

def run_sync():
    print(f"\n{'=' * 55}")
    print(f"  TRADE SYNC — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 55}\n")

    ensure_tables()

    print("  Fetching from Tastytrade...", end=" ", flush=True)
    try:
        tt_data = fetch_tastytrade_data()
        print("OK")
    except Exception as e:
        print(f"ERROR: {e}")
        return

    account_number = tt_data["account_number"]
    balances       = tt_data["balances"]
    tt_positions   = tt_data["positions"]

    snapshot_id = save_account_snapshot(balances)
    print(f"  Account snapshot saved (id={snapshot_id})")
    print(f"  Net liquidating value:   ${balances['net_liquidating_value']:,.2f}")
    print(f"  Derivative buying power: ${balances['derivative_buying_power']:,.2f}")
    print(f"  Pending cash:            ${balances['pending_cash']:,.2f}")
    if balances['long_derivative_value']:
        print(f"  Options value:           ${balances['long_derivative_value']:,.2f}")

    spreads, singles = group_spreads(tt_positions)
    print(f"\n  Tastytrade positions: {len(tt_positions)} legs "
          f"→ {len(spreads)} spread(s) + {len(singles)} single(s)")

    db_positions = get_open_positions_from_db()
    print(f"  DB open positions:    {len(db_positions)}")

    db_symbols = set()
    for p in db_positions:
        if p.get("tastytrade_symbol"):
            db_symbols.add(p["tastytrade_symbol"])
        if p.get("tastytrade_symbol_short"):
            db_symbols.add(p["tastytrade_symbol_short"])

    new_count = 0

    for spread in spreads:
        if spread["tastytrade_symbol"] not in db_symbols:
            pos_id = insert_spread(spread, account_number)
            print(f"\n  NEW spread imported:")
            print(f"    {spread['ticker']} Bull Call Spread "
                  f"${spread['strike_low']}/${spread['strike_high']} "
                  f"exp {spread['expiration']}")
            print(f"    Contracts: {spread['contracts']} | "
                  f"Net debit: ${spread['premium_paid']:.2f} | "
                  f"Total cost: ${spread['total_cost']:.2f}")
            print(f"    DB id: {pos_id}")
            print(f"  ⚠️  Guarda el contexto:")
            print(f"      python trade.py --save-context --position-id {pos_id} "
                  f"--ticker {spread['ticker']} --rationale \"...\"")
            new_count += 1

    for tt_pos in singles:
        if tt_pos["symbol"] not in db_symbols:
            pos_id = insert_position(tt_pos, account_number)
            print(f"\n  NEW position imported:")
            print(f"    {tt_pos['ticker']} {tt_pos['option_type']} "
                  f"${tt_pos['strike']} exp {tt_pos['expiration']}")
            print(f"    DB id: {pos_id}")
            print(f"  ⚠️  Guarda el contexto:")
            print(f"      python trade.py --save-context --position-id {pos_id} "
                  f"--ticker {tt_pos['ticker']} --rationale \"...\"")
            new_count += 1

    tt_all_symbols = {p["symbol"] for p in tt_positions}
    closed_count   = 0

    for db_pos in db_positions:
        sym_long  = db_pos.get("tastytrade_symbol")
        sym_short = db_pos.get("tastytrade_symbol_short")

        if sym_short:
            both_gone = (sym_long not in tt_all_symbols and
                         sym_short not in tt_all_symbols)
            if both_gone:
                # None a propósito: la posición ya no está en Tastytrade, así que
                # el precio de salida no existe acá. Queda CLOSED_PRICE_UNKNOWN
                # con P&L NULL. Antes esto registraba la pérdida máxima entera.
                pnl = close_position_in_db(db_pos["id"], None, "Closed in Tastytrade")
                print(f"\n  CLOSED spread: {db_pos['ticker']} (DB id={db_pos['id']})")
                if pnl is None:
                    print(f"    P&L: SIN DATO — el precio de salida no lo sabe el sync.")
                    print(f"         Marcada CLOSED_PRICE_UNKNOWN, no se inventa un número.")
                else:
                    print(f"    P&L: ${pnl:.2f}")
                closed_count += 1
        else:
            if sym_long and sym_long not in tt_all_symbols:
                pnl = close_position_in_db(db_pos["id"], None, "Closed in Tastytrade")
                print(f"\n  CLOSED position: {db_pos['ticker']} (DB id={db_pos['id']})")
                if pnl is None:
                    print(f"    P&L: SIN DATO — marcada CLOSED_PRICE_UNKNOWN.")
                else:
                    print(f"    P&L: ${pnl:.2f}")
                closed_count += 1

    print(f"\n{'=' * 55}")
    print(f"  Sync complete:")
    print(f"    New positions imported: {new_count}")
    print(f"    Positions closed:       {closed_count}")
    print(f"    Account snapshot:       saved")
    print(f"{'=' * 55}\n")


# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY COMMANDS — real positions
# ══════════════════════════════════════════════════════════════════════════════

def cmd_list():
    positions = get_open_positions_from_db()
    print(f"\n  Open positions in DB: {len(positions)}\n")
    for p in positions:
        exp    = str(p["expiration"])[:10] if p["expiration"] else "N/A"
        cost   = float(p["total_cost"] or 0)
        broker = p.get("broker", "N/A")
        print(f"  #{p['id']} {p['ticker']} {p['strategy']} "
              f"${p['strike_low']}/${p['strike_high']} | Exp {exp} | "
              f"Cost ${cost:.2f} | {broker}")
    print()


def cmd_account():
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT snapshot_at, net_liquidating_value, derivative_buying_power,
               pending_cash, long_derivative_value
        FROM account_snapshots
        ORDER BY snapshot_at DESC LIMIT 30
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    print(f"\n  Account history (last {len(rows)} snapshots):\n")
    print(f"  {'Date/Time':<22} {'NLV':>10} {'Deriv BP':>10} "
          f"{'Pending':>10} {'Options':>10}")
    print(f"  {'-'*22} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    for row in rows:
        snap_at, nlv, dbp, pending, options = row
        print(f"  {str(snap_at)[:19]:<22} "
              f"${float(nlv or 0):>9,.0f} "
              f"${float(dbp or 0):>9,.0f} "
              f"${float(pending or 0):>9,.0f} "
              f"${float(options or 0):>9,.0f}")
    print()


def cmd_history():
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT ticker, strategy, strike_low, strike_high, expiration,
               total_cost, total_received, net_pnl, pnl_pct,
               close_reason, closed_at
        FROM positions
        WHERE UPPER(status) = 'CLOSED'
        ORDER BY closed_at DESC LIMIT 20
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    print(f"\n  Closed positions history (last {len(rows)}):\n")
    for row in rows:
        ticker, strategy, sl, sh, exp, cost, received, pnl, pnl_pct, reason, closed = row
        pnl     = float(pnl or 0)
        pnl_pct = float(pnl_pct or 0)
        cost    = float(cost or 0)
        emoji   = "+" if pnl >= 0 else "-"
        strikes = f"${sl}/{sh}" if sh and sl != sh else f"${sl}"
        print(f"  {ticker} {strategy} {strikes} | "
              f"Cost ${cost:.0f} | "
              f"P&L {emoji}${abs(pnl):.0f} ({pnl_pct:+.1f}%) | "
              f"{str(closed)[:10] if closed else 'N/A'}")
    print()


def cmd_save_context(position_id, context_json, rationale, ticker=None):
    """Save context for an existing real position."""
    ensure_tables()

    # Auto-read from reports if no context provided and ticker is known
    if context_json is None and ticker:
        context_json = _read_context_from_reports(ticker)
        if context_json:
            print(f"  Context auto-loaded from reports for {ticker} ✅")

    ctx_id = save_trade_context(
        position_id=position_id,
        context_json=context_json,
        rationale=rationale
    )
    if ctx_id:
        print(f"\n  ✅ Context saved for position #{position_id} (context id: {ctx_id})\n")
    else:
        print(f"\n  ❌ Failed to save context\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trade sync with Tastytrade + paper trading")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sync",          action="store_true")
    group.add_argument("--list",          action="store_true")
    group.add_argument("--account",       action="store_true")
    group.add_argument("--history",       action="store_true")
    group.add_argument("--paper-buy",     metavar="TICKER")
    group.add_argument("--paper-sync",    action="store_true")
    group.add_argument("--paper-list",    action="store_true")
    group.add_argument("--paper-history", action="store_true")
    group.add_argument("--paper-close",   metavar="TICKER")
    group.add_argument("--save-context",  action="store_true",
                       help="Save trade context for an existing real position")

    parser.add_argument("--strike-low",   type=float)
    parser.add_argument("--strike-high",  type=float)
    parser.add_argument("--expiration",   type=str, help="YYYY-MM-DD")
    parser.add_argument("--debit",        type=float)
    parser.add_argument("--notes",        type=str, default=None)
    parser.add_argument("--context",      type=str, default=None,
                        help='JSON string with market criteria (optional — auto-read from reports)')
    parser.add_argument("--rationale",    type=str, default=None,
                        help="Claude's qualitative analysis paragraph")
    parser.add_argument("--position-id",  type=int, default=None,
                        help="DB position id (for --save-context)")
    parser.add_argument("--ticker",       type=str, default=None,
                        help="Ticker symbol (for --save-context auto-context)")

    args = parser.parse_args()

    if args.sync:
        run_sync()
    elif args.list:
        cmd_list()
    elif args.account:
        cmd_account()
    elif args.history:
        cmd_history()
    elif args.paper_buy:
        if not all([args.strike_low, args.strike_high, args.expiration, args.debit]):
            print("  ERROR: --paper-buy requires --strike-low --strike-high --expiration --debit")
        else:
            cmd_paper_buy(
                args.paper_buy, args.strike_low, args.strike_high,
                args.expiration, args.debit, args.notes,
                args.context, args.rationale
            )
    elif args.paper_sync:
        cmd_paper_sync()
    elif args.paper_list:
        cmd_paper_list()
    elif args.paper_history:
        cmd_paper_history()
    elif args.paper_close:
        cmd_paper_close(args.paper_close)
    elif args.save_context:
        if not args.position_id:
            print("  ERROR: --save-context requires --position-id")
        else:
            cmd_save_context(args.position_id, args.context, args.rationale, args.ticker)