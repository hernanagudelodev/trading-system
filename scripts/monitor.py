"""
monitor.py
==========
Position monitoring script — multi-strategy support.

Strategies supported:
    Bull Call Spread    → spread width P&L
    Bear Put Spread     → spread width P&L
    Long Call           → delta-based P&L with yfinance live delta
    Long Put            → delta-based P&L with yfinance live delta
    Cash Secured Put    → premium decay P&L
    Covered Call        → premium decay P&L
    Long Straddle       → combined call+put P&L

Alert levels:
    NORMAL  → within expected ranges       → console + HTML
    WATCH   → approaching targets          → console + HTML + ntfy
    ACTION  → take profit reached          → console + HTML + ntfy
    URGENT  → stop loss / max profit       → console + HTML + ntfy

Usage:
    python monitor.py              → run once
    python monitor.py --loop       → run in adaptive loop

Dependencies:
    criteria.py  → current market data
    db.py        → positions
    .env         → DATABASE_URL, NTFY_TOPIC
"""

import os
import time
import asyncio
import argparse
import schedule
import yfinance as yf
from datetime import datetime, date

from dotenv import load_dotenv

from notify import send_push

from criteria import get_all_criteria
from db import get_open_positions

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

TAKE_PROFIT_MIN_PCT  = 0.50   # 50% of max profit → ACTION
TAKE_PROFIT_MAX_PCT  = 0.70   # 70% of max profit → URGENT
WATCH_PROFIT_PCT     = 0.30   # 30% of max → WATCH
MIN_DTE              = 7      # days → ACTION
WATCH_DTE            = 10     # days → WATCH

# ── STOP LOSS — measured over REAL MAX LOSS, thresholds per strategy ──────────
#
# THE BUG THIS FIXES (24-jul)
#   pnl_pct = gross_pnl / max_profit, i.e. a % of the CREDIT for a credit spread.
#   A -65% stop on that is ~21% of the real risk for a BPS (credit $120, max loss
#   $380). JNJ would have stopped near -$83 instead of a sane level. The number
#   meant something different depending on the strategy.
#
# THE FIX
#   The denominator is the STRUCTURE's max loss (from spread_pnl), not the credit.
#   pnl_vs_maxloss = -gross_pnl / max_loss  (0.0 at open, 1.0 at total loss).
#   Then the threshold table is per strategy, still tiered by DTE.
#
#   Credit spreads (BPS) get a tighter stop: closing a credit when it costs ~2x
#   the credit is standard, which is ~34% of real risk. Debit spreads keep the
#   historical -65/-55/-50 tier, now correctly over real risk.
STOP_LOSS_WATCH_PCT = 0.30   # 30% loss (of real max loss) → WATCH

STOP_THRESHOLDS = {
    # strategy_type -> (>15 DTE, 8-15 DTE, <8 DTE), as fraction of MAX LOSS
    "debit_spread":  (0.65, 0.55, 0.50),
    "long_option":   (0.65, 0.55, 0.50),
    "credit_spread": (0.35, 0.30, 0.25),
}
_DEFAULT_STOP_TIER = (0.65, 0.55, 0.50)


def _dte_tier_index(dte):
    if dte is None or dte > 15:
        return 0
    if dte >= 8:
        return 1
    return 2


def get_stop_loss_pct(dte, strategy_type="debit_spread"):
    """
    Stop threshold as a fraction of MAX LOSS, by strategy and DTE.

    strategy_type comes from spread_pnl ('debit_spread', 'credit_spread',
    'long_option'). Unknown types fall back to the debit tier, loudly-ish:
    a new strategy must add its own row here on purpose, not inherit silently.
    """
    tier = STOP_THRESHOLDS.get(strategy_type)
    if tier is None:
        print(f"  ⚠️  no stop tier for strategy_type={strategy_type!r} — "
              f"using debit default. Add it to STOP_THRESHOLDS.")
        tier = _DEFAULT_STOP_TIER
    return tier[_dte_tier_index(dte)]

def auto_close_enabled():
    """
    ¿El monitor puede EJECUTAR cierres, o solo alertar?

    MONITOR_AUTO_CLOSE=true   -> alerta Y cierra (comportamiento historico)
    MONITOR_AUTO_CLOSE=false  -> alerta y NADA MAS. Vos cerras a mano.

    OBLIGATORIA, SIN DEFAULT — a proposito
        Un interruptor de seguridad que se puede quedar mal por un typo
        silencioso no es un interruptor de seguridad. Si falta o el valor no se
        entiende, el proceso MUERE al arrancar: Railway lo muestra caido y el
        healthcheck se pone rojo. Un default habria sido la forma de terminar
        operando en automatico creyendo que no.
        Mismo criterio que EXECUTOR_ENV y MAX_PORTFOLIO_RISK_PCT.

    POR QUE EXISTE — 23-jul
        PAYX $100/$105 se cerro por STOP_LOSS a 2.02 con el spread valiendo
        ~1.18 y PAYX plano en $110 todo el dia. El precio que disparo el stop
        fue tambien el precio limite de la orden: un mid inflado no solo cierra
        de mas, ademas paga de mas. Mientras el pricing no se audite, el monitor
        avisa y el humano decide.
    """
    raw = os.getenv("MONITOR_AUTO_CLOSE", "").strip().lower()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    raise RuntimeError(
        f"MONITOR_AUTO_CLOSE ausente o invalida (recibido: {raw!r}). "
        f"Valores validos: true / false. No tiene default a proposito: "
        f"decidir si el sistema puede gastar plata sola no se adivina."
    )


MARKET_OPEN_HOUR     = 9
MARKET_OPEN_MIN      = 30
MARKET_CLOSE_HOUR    = 16
MARKET_CLOSE_MIN     = 0

INTERVAL_MARKET_OPEN    = 5
INTERVAL_PRE_MARKET     = 10
INTERVAL_MARKET_CLOSED  = 30


# Dead code removed (24-jul): heartbeat globals, REPORT_PATH and the strategy
# sets fed generate_html_report / send_heartbeat / send_market_close_summary,
# all orphaned when run_monitor merged into run_position_monitor. The push
# heartbeat is gone on purpose: healthchecks.io already watches the process and
# an hourly "still alive" push is noise. Liveness lives in healthcheck_ping().


# ══════════════════════════════════════════════════════════════════════════════
# MARKET HOURS
# ══════════════════════════════════════════════════════════════════════════════

def get_market_status():
    """Returns: 'open' | 'pre' | 'closed'. Uses ET timezone."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except ImportError:
        import pytz
        now = datetime.now(pytz.timezone("America/New_York"))

    if now.weekday() >= 5:
        return "closed"

    t = now.hour * 60 + now.minute
    open_t  = MARKET_OPEN_HOUR  * 60 + MARKET_OPEN_MIN
    close_t = MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN
    pre_t   = (MARKET_OPEN_HOUR - 1) * 60 + MARKET_OPEN_MIN

    if open_t <= t < close_t:
        return "open"
    elif pre_t <= t < open_t:
        return "pre"
    return "closed"


def is_market_open():
    return get_market_status() == "open"


def get_interval():
    status = get_market_status()
    if status == "open":
        return INTERVAL_MARKET_OPEN
    elif status == "pre":
        return INTERVAL_PRE_MARKET
    return INTERVAL_MARKET_CLOSED


def get_live_option_price(ticker, strike, expiration, option_type="call"):
    """
    Fetch live mid price for a specific option from yfinance.
    Returns mid price as float, or None if unavailable.
    """
    try:
        tk = yf.Ticker(ticker)

        available = tk.options
        if not available:
            return None

        closest = min(
            available,
            key=lambda e: abs((datetime.strptime(e, "%Y-%m-%d").date() - expiration).days)
        )

        chain = tk.option_chain(closest)
        df = chain.calls if option_type == "call" else chain.puts

        if df is None or df.empty:
            return None

        df = df.copy()
        df["strike_diff"] = (df["strike"] - strike).abs()
        row = df.sort_values("strike_diff").iloc[0]

        bid = row.get("bid", 0) or 0
        ask = row.get("ask", 0) or 0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        last = row.get("lastPrice", None)
        return float(last) if last else None

    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SPREAD VALUE — delega en pricing.py (fuente única)
# ══════════════════════════════════════════════════════════════════════════════

def get_spread_value_tastytrade(ticker, strike_low, strike_high, expiration,
                                option_type="call", session=None):
    """Delegado a pricing.get_spread_value — fuente única de pricing de spreads.
    El parámetro 'session' se ignora (compatibilidad de firma)."""
    import pricing
    return pricing.get_spread_value(ticker, strike_low, strike_high, expiration,
                                    option_type=option_type)


# ══════════════════════════════════════════════════════════════════════════════
# P&L CALCULATION — strategy-aware
# ══════════════════════════════════════════════════════════════════════════════

ALERT_WORSEN_POINTS = 10.0   # pnl_pct points of deterioration within a level
ALERT_REMINDER_HOURS = 1     # re-alert an unchanged level after this many hours


def _should_alert(level, pnl_pct, last_level, last_at, last_pnl_pct):
    """
    Decide whether to send an alert this cycle, given what was last sent.

        URGENT           -> always (a stop/target must not wait)
        level changed    -> yes
        worsened >=N pts  -> yes (pnl_pct dropped ALERT_WORSEN_POINTS since last)
        >=N hours passed  -> yes (periodic reminder it is still in this level)
        otherwise         -> no  (deduped)

    Designed to be callable with None fields (first time this row alerts).
    """
    if level == "URGENT":
        return True
    if last_level != level:
        return True
    # same level as last alert:
    if last_pnl_pct is not None and pnl_pct <= float(last_pnl_pct) - ALERT_WORSEN_POINTS:
        return True
    if last_at is not None:
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            ref = last_at if last_at.tzinfo else last_at.replace(tzinfo=timezone.utc)
            if (now - ref).total_seconds() >= ALERT_REMINDER_HOURS * 3600:
                return True
        except Exception:
            return True   # if we can't compute age, err toward alerting
    else:
        return True       # no timestamp recorded -> alert
    return False


def evaluate_alert_level(pnl_data):
    profit_pct_of_max = pnl_data["profit_pct_of_max"]
    pnl_pct           = pnl_data["pnl_pct"]
    dte               = pnl_data["dte"]
    strategy_type     = pnl_data.get("strategy_type", "debit_spread")
    reasons           = []
    level             = "NORMAL"

    # Stop loss threshold depends on strategy and DTE (over real max loss)
    stop_loss_pct = get_stop_loss_pct(dte, strategy_type)

    # ── URGENT conditions ────────────────────────────────────────────────────
    if profit_pct_of_max >= TAKE_PROFIT_MAX_PCT:
        reasons.append(f"Ganancia {profit_pct_of_max*100:.0f}% del maximo — no dejes escapar")
        level = "URGENT"

    if pnl_pct <= -(stop_loss_pct * 100):
        reasons.append(
            f"Stop loss alcanzado — perdida {pnl_pct:.1f}% "
            f"(umbral {stop_loss_pct*100:.0f}% con {dte}d restantes)"
        )
        level = "URGENT"

    # ── ACTION conditions ────────────────────────────────────────────────────
    if level != "URGENT":
        if TAKE_PROFIT_MIN_PCT <= profit_pct_of_max < TAKE_PROFIT_MAX_PCT:
            reasons.append(f"Take profit alcanzado — {profit_pct_of_max*100:.0f}% del maximo")
            level = "ACTION"

        if dte is not None and dte <= MIN_DTE:
            reasons.append(f"Solo {dte} dias al vencimiento — Theta acelerando")
            level = "ACTION"

    # ── WATCH conditions ─────────────────────────────────────────────────────
    if level == "NORMAL":
        if WATCH_PROFIT_PCT <= profit_pct_of_max < TAKE_PROFIT_MIN_PCT:
            reasons.append(f"Acercandose al objetivo — {profit_pct_of_max*100:.0f}% del maximo")
            level = "WATCH"

        if pnl_pct <= -(STOP_LOSS_WATCH_PCT * 100):
            reasons.append(f"Perdida creciente — {pnl_pct:.1f}%")
            level = "WATCH"

        if dte is not None and MIN_DTE < dte <= WATCH_DTE:
            reasons.append(f"{dte} dias al vencimiento — monitorear de cerca")
            level = "WATCH"

    if not reasons:
        reasons.append("Dentro de rangos normales")

    return level, reasons


def level_icon(level):
    return {"NORMAL": "[OK]", "WATCH": "[!!]", "ACTION": "[ACT]", "URGENT": "[URG]"}.get(level, "---")


def level_icon_emoji(level):
    return {"NORMAL": "verde", "WATCH": "amarillo", "ACTION": "naranja", "URGENT": "rojo"}.get(level, "---")



def _paper_alerts_enabled() -> bool:
    """
    Lee system_state['paper_alerts']. Devuelve False SOLO si el flag == 'off'.
    Fail-safe INVERSO al de live_kill: si la DB no responde o no hay fila, se
    ASUME activo (True) — ante la duda, notificar. Perder una alerta por una DB
    caida es peor que una notificacion de mas.
    """
    import psycopg2
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur  = conn.cursor()
        cur.execute("SELECT value FROM system_state WHERE key = %s", ("paper_alerts",))
        row = cur.fetchone()
        cur.close(); conn.close()
    except Exception:
        return True   # DB ilegible -> ante la duda, notificar
    if row is not None and str(row[0]).strip().lower() == "off":
        return False
    return True


def send_alert_notification(position, pnl_data, alert_level, reasons, mode="paper"):
    # Filtro de alertas paper: si system_state['paper_alerts']=='off', las alertas
    # de nivel de PAPER (WATCH/ACTION/URGENT/take-profit) se silencian. Las de LIVE
    # nunca se tocan. Se controla con /paper_alerts on|off desde el bot.
    if mode == "paper" and not _paper_alerts_enabled():
        return
    ticker   = position["ticker"]
    strategy = position.get("strategy", "")
    pnl      = pnl_data["gross_pnl"]
    pct      = pnl_data["profit_pct_of_max"] * 100
    dte      = pnl_data["dte"]

    # El modo va en el título: con dos workers, una alerta URGENTE de plata real
    # y una de paper no se pueden confundir.
    tag = "🔴 LIVE" if mode == "live" else "📄 PAPER"
    level_titles = {
        "URGENT": f"{tag} · URGENTE — {ticker}",
        "ACTION": f"{tag} · ACCION — {ticker}",
        "WATCH":  f"{tag} · WATCH — {ticker}",
    }
    priorities = {"URGENT": "urgent", "ACTION": "high", "WATCH": "default"}

    title       = level_titles.get(alert_level, ticker)
    reason_text = "\n".join(f"- {r}" for r in reasons)
    message     = (
        f"{strategy} | ${ticker}\n"
        f"P&L: ${pnl:+.0f} ({pct:.0f}% del max) | {dte}d\n"
        f"{reason_text}"
    )

    send_push(title, message, priority=priorities.get(alert_level, "default"))


# ══════════════════════════════════════════════════════════════════════════════
# ENVIRONMENT GUARD
# ══════════════════════════════════════════════════════════════════════════════

def _validate_env():
    """
    Fail loudly at startup if anything the monitor needs is missing.

    WHY
        The monitor is now the ONLY thing that can close a position. It cannot
        run half-configured. Before, a missing TRADING_MODE silently defaulted
        to 'paper' and, on the live service, that means watching the WRONG table
        while reporting success -- the exact silent-failure this project fights.
        Missing Tastytrade creds are worse: pricing returns None, everything
        looks 'no data', nothing alerts or closes, and the process reports fine.

    auto_close_enabled() already raises on its own var; this covers DATABASE_URL
    and the broker credentials so the failure happens HERE, at startup, not deep
    inside a pricing call three functions down.
    """
    missing = [v for v in (
        "DATABASE_URL",
        "TASTYTRADE_CLIENT_SECRET",
        "TASTYTRADE_REFRESH_TOKEN",
    ) if not os.getenv(v)]
    if missing:
        raise RuntimeError(
            f"monitor cannot start: missing env vars {missing}. "
            f"It is the only component that can close a position; running it "
            f"half-configured is how a silent failure looks like success."
        )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN MONITOR RUN
# ══════════════════════════════════════════════════════════════════════════════

def run_position_monitor(table, executor, mode_label):
    """
    Vigila las posiciones abiertas de UN libro, y las CIERRA cuando toca.

    def: recibe la tabla, el executor y la etiqueta como PARAMETROS. Ya no hay
    TRADING_MODE — scheduled_run la llama DOS veces (live y luego paper). Cada
    pasada pricea, evalua y cierra su propio libro. El cierre pasa por el executor
    que se le pasa: PaperExecutor escribe la DB, LiveExecutor manda orden real.

    Antes se llamaba run_paper_monitor y estaba clavada a `paper_positions`.
    Eso significaba que en live NO existía cierre automático de ningún tipo:
    run_monitor() vigila `positions` pero sólo alerta — nunca cerró nada. Los
    stops de -66% de paper los ejecutó esta función, escribiendo una tabla.
    Con plata real no había nada que cerrara una posición sola.

    LO QUE CAMBIA RESPECTO DE LA VERSIÓN ANTERIOR
      - La tabla sale de TRADING_MODE, no está clavada.
      - El cierre NO se hace con UPDATE: pasa por executor.close_position().
        En paper eso llama a cmd_paper_close (mismo resultado que antes); en
        live manda una orden real al broker. La DECISIÓN es idéntica para los
        dos libros; lo único que difiere es la ejecución — que es exactamente
        para lo que existe el Executor.
      - El `reason` viaja hasta la DB: monitor -> executor -> close_reason.

    EL PRECIO DE LA DECISIÓN NO ES EL PRECIO DEL CIERRE
        Acá se pricea para decidir; el executor pricea de nuevo al cerrar. Son
        segundos de diferencia y el segundo es más fresco, así que está bien.
        Pero si ese segundo pricing falla, close_position devuelve False y la
        posición sigue abierta pese a que el stop disparó. No se pierde: el
        worker vuelve en 5 minutos y lo reintenta. Se registra como error, no
        como cierre.
    """
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv()

    _validate_env()                     # dies loudly if anything is missing
    mode       = mode_label             # 'live' | 'paper', lo pasa scheduled_run
    TABLE      = table                  # 'positions' | 'paper_positions'
    auto_close = auto_close_enabled()   # explota si falta: ver el helper
    ex         = executor

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"\n{'=' * 60}")
    print(f"  MONITOR DE POSICIONES [{mode}] — {timestamp}")
    market_status = get_market_status()
    status_label  = {"open": "ABIERTO", "pre": "PRE-MARKET", "closed": "CERRADO"}
    print(f"  Tabla: {TABLE} | Mercado: {status_label.get(market_status, '?')} | "
          f"Intervalo: {get_interval()}min")
    if auto_close:
        print(f"  Auto-cierre: ACTIVO — el monitor puede mandar ordenes")
    else:
        print(f"  Auto-cierre: DESACTIVADO — SOLO ALERTAS, nada se cierra solo")
    print(f"{'=' * 60}\n")

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur  = conn.cursor()
    cur.execute(f"""
        SELECT id, ticker, strategy, strike_low, strike_high,
               expiration, contracts, total_cost, premium_paid,
               current_spread_value, gross_pnl, pnl_pct,
               profit_pct_of_max, opened_at,
               tastytrade_symbol, tastytrade_symbol_short,
               last_alert_level, last_alert_at, last_alert_pnl_pct
        FROM {TABLE}
        WHERE UPPER(status) = 'OPEN'
        ORDER BY opened_at
    """)
    cols      = [d[0] for d in cur.description]
    positions = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    conn.close()

    if not positions:
        print("  No hay paper positions abiertas.\n")
        return

    print(f"  Paper positions abiertas: {len(positions)}\n")

    for pos in positions:
        ticker      = pos["ticker"]
        strike_low  = float(pos["strike_low"])
        strike_high = float(pos["strike_high"])
        total_cost  = float(pos["total_cost"])
        premium     = float(pos["premium_paid"])
        expiration  = pos["expiration"]
        dte         = (expiration - date.today()).days

        strategy = pos.get("strategy", "Bull Call Spread")
        is_long  = strategy in ("Long Call", "Long Put")

        print(f"  Revisando {ticker} [{mode}]...", end=" ", flush=True)

        # PRICING (24-jul): spreads are valued by MARK via the REST endpoint,
        # the same channel the option_selector uses, so the two never disagree
        # on the same instrument. MARK is the broker's own valuation and stays
        # stable when a wide-book mid does not -- the mid is what tripped PAYX.
        # Long options keep the existing per-leg path for now.
        spread_value = None
        long_value   = None
        if is_long:
            opt_type   = "call" if strategy == "Long Call" else "put"
            long_value = get_live_option_price(ticker, strike_low, expiration, opt_type)
        else:
            sym_long  = pos.get("tastytrade_symbol")
            sym_short = pos.get("tastytrade_symbol_short")
            is_credit = premium < 0                      # the sign is the source
            if sym_long and sym_short:
                from pricing import get_spread_mark_by_symbols
                q = get_spread_mark_by_symbols(sym_long, sym_short, is_credit)
                if q is not None and q.get("halted"):
                    print("(trading halted) ", end="")
                    q = None
                spread_value = q["mark"] if q else None
            else:
                # No OCC symbols on the row: cannot price by REST. Do NOT fall
                # back to a different channel silently -- leave it None and let
                # the stale-price path handle it, visibly.
                print("(no OCC symbols) ", end="")
                spread_value = None

        time.sleep(0.3)

        # `precio` es el valor actual: spread_value para spreads, long_value para
        # longs. price_fresh gobierna si se puede CERRAR — nunca con precio viejo.
        precio      = long_value if is_long else spread_value
        price_fresh = True
        if precio is None:
            last_known = float(pos["current_spread_value"] or 0)
            if last_known <= 0:
                print(f"sin datos reales — omitiendo")
                print(f"\n  [--] {ticker} [{mode}] — {strategy} — SIN DATOS")
                print(f"  {'─' * 50}")
                print(f"  Strike(s):     ${strike_low} / ${strike_high}")
                print(f"  Expiracion:    {expiration} ({dte} dias)")
                print(f"  No se pudo obtener precio real.")
                print()
                continue
            precio      = last_known
            price_fresh = False
            print(f"usando último valor conocido: ${precio:.2f} (NO se cerrará con precio viejo)")
        else:
            print(f"{'opción' if is_long else 'spread'}=${precio:.2f}")

        if is_long:
            spread_value = None
            long_value   = precio
        else:
            spread_value = precio

        # P&L: fuente ÚNICA en option_selector.spread_pnl, que ahora maneja las
        # tres estrategias (Bull Call, Bull Put, Long Call). Reemplazó también a
        # calculate_current_pnl, que sólo sabía debit spreads y longs — nunca
        # Bull Put, que es la mitad de la cartera.
        from option_selector import spread_pnl

        contracts     = int(pos.get("contracts") or 1)
        is_put_spread = (not is_long) and premium < 0    # el signo manda

        r = spread_pnl(strike_low, strike_high, premium, contracts, spread_value,
                       strategy=strategy, long_value=long_value)
        max_profit     = r["max_profit"]
        max_loss       = r["max_loss"]
        current_value  = r["current_value"]
        cost_to_close  = r["current_value"]
        gross_pnl      = r["gross_pnl"]
        pnl_pct        = r["pnl_pct"] if r["pnl_pct"] is not None else 0
        profit_pct_max = r["profit_pct_of_max"] if r["profit_pct_of_max"] is not None else 0
        strategy_type  = r["strategy_type"]

        pnl_data = {
            "profit_pct_of_max": profit_pct_max,
            "pnl_pct":           pnl_pct,
            "dte":               dte,
            "strategy_type":     strategy_type,
        }
        alert_level, reasons = evaluate_alert_level(pnl_data)
        icon = level_icon(alert_level)

        print(f"\n  {icon} {ticker} [{mode}] — {strategy} — {alert_level}")
        print(f"  {'─' * 50}")
        print(f"  Strike(s):     ${strike_low} / ${strike_high}")
        print(f"  Expiracion:    {expiration} ({dte} dias)")
        if is_put_spread:
            print(f"  Crédito rec:   ${abs(premium):.2f} (max ganancia ${max_profit:.2f})")
            print(f"  Costo cierre:  ${cost_to_close:.2f}")
        else:
            print(f"  Costo total:   ${total_cost:.2f}")
        print(f"  Valor actual:  ${precio:.2f}")
        print(f"  Ganancia/Perd: ${gross_pnl:+.2f} ({pnl_pct:+.1f}%)")
        print(f"  % del maximo:  {profit_pct_max*100:.1f}%")
        if max_profit is not None:
            print(f"  Ganancia max:  ${max_profit:.2f}")
        print(f"\n  Alertas:")
        for motivo in reasons:
            print(f"    - {motivo}")
        print()

        # ── ALERT with DEDUP (24-jul) ─────────────────────────────────────────
        # Market closed -> silence (can't act on it at night). Stale price ->
        # silence (we don't alert on a number we don't trust). Otherwise decide
        # whether this alert is NEW enough to send:
        #   URGENT              -> always (a stop/target won't wait an hour)
        #   level changed       -> send  (WATCH -> ACTION, NORMAL -> WATCH...)
        #   worsened >=10 pts    -> send  (same level, pnl_pct 10 points worse)
        #   >=1h since last      -> send  (periodic reminder it's still there)
        # NORMAL resets the dedup state so a re-entry to WATCH alerts again.
        alerted_now = False
        if price_fresh and get_market_status() in ("open", "pre"):
            if alert_level in ("WATCH", "ACTION", "URGENT"):
                if _should_alert(alert_level, pnl_pct,
                                 pos.get("last_alert_level"),
                                 pos.get("last_alert_at"),
                                 pos.get("last_alert_pnl_pct")):
                    send_alert_notification(pos, {**pnl_data, "gross_pnl": gross_pnl},
                                            alert_level, reasons, mode)
                    alerted_now = True

        # ── Cierre determinista — el worker es el ÚNICO dueño de stops de paper ─
        # Usa las mismas constantes canónicas que el monitor real (sin inventar
        # un cuarto criterio): stop escalonado por DTE, target 70%, DTE mínimo.
        close_reason = None
        if price_fresh:
            if dte is not None and dte <= MIN_DTE:
                close_reason = "TIME_EXPIRED"
            elif profit_pct_max >= TAKE_PROFIT_MAX_PCT:
                close_reason = "TARGET_REACHED"
            elif pnl_pct <= -(get_stop_loss_pct(dte, strategy_type) * 100):
                close_reason = "STOP_LOSS"

        # ── EL CIERRE PASA POR EL EXECUTOR ────────────────────────────────────
        # Antes esto era un UPDATE directo, y por eso el cierre automático NO
        # existía en live: escribir una tabla no le dice nada al broker.
        # Ahora la decisión es la misma para los dos libros y la ejecución la
        # resuelve el executor — paper escribe la DB, live manda una orden real.
        if close_reason and not auto_close:
            # SOLO ALERTAS. El criterio disparo, pero el monitor no ejecuta.
            # Se avisa fuerte y con los numeros necesarios para cerrar a mano;
            # despues cae al UPDATE de P&L, porque la posicion sigue viva.
            print(f"  → {close_reason} disparo · AUTO-CIERRE DESACTIVADO — "
                  f"{ticker} NO se cierra, hay que hacerlo a mano")
            send_push(
                title=f"CERRAR A MANO [{mode}]: {ticker} ({close_reason})",
                message=(f"{ticker} {strategy} ${strike_low}/{strike_high}\n"
                         f"P&L ${gross_pnl:+.2f} ({pnl_pct:+.1f}%) | DTE {dte}\n"
                         f"Valor del spread: ${precio:.2f}\n\n"
                         f"El auto-cierre esta DESACTIVADO.\n"
                         f"VERIFICA EL PRECIO EN TASTYTRADE antes de cerrar: "
                         f"este valor sale del mismo pricing que esta en revision."),
                priority="urgent",
            )

        elif close_reason:
            print(f"  → AUTO-CIERRE [{mode}]: {close_reason}")
            try:
                cerrada = ex.close_position(ticker, close_reason)
            except Exception as e:
                cerrada = False
                print(f"  ⛔ {ticker}: el cierre reventó: {e}")

            if cerrada:
                send_push(
                    title=f"Auto-cierre [{mode}]: {ticker} ({close_reason})",
                    message=(f"{ticker} {strategy} ${strike_low}/{strike_high}\n"
                             f"P&L ${gross_pnl:+.2f} ({pnl_pct:+.1f}%) | DTE {dte}\n"
                             f"Motivo: {close_reason}"),
                    priority="default",
                )
                continue

            # No cerró. La posición SIGUE ABIERTA aunque el stop haya disparado.
            # No se pierde: el worker vuelve en 5 minutos y reintenta. Pero si es
            # un stop, cada ciclo que pasa es dinero, así que se avisa fuerte.
            print(f"  ⛔ {ticker}: {close_reason} disparó y NO se pudo cerrar — "
                  f"sigue ABIERTA")
            send_push(
                title=f"NO se pudo cerrar {ticker} ({close_reason})",
                message=(f"{ticker} {strategy} ${strike_low}/{strike_high}\n"
                         f"P&L ${gross_pnl:+.2f} ({pnl_pct:+.1f}%) | DTE {dte}\n\n"
                         f"El {close_reason} disparó y la posición SIGUE ABIERTA.\n"
                         f"El worker reintenta en 5 min."),
                priority="urgent" if close_reason == "STOP_LOSS" else "high",
            )
            # Cae al UPDATE de P&L: la posición sigue viva y su estado importa.

        # ── PERSIST P&L (+ dedup state) ───────────────────────────────────────
        # PIECE C: last_synced_at is only stamped NOW() when the price is FRESH.
        # A stale price (pricing failed, using last known) must NOT masquerade as
        # a fresh sync -- that timestamp is read by reports and by auto_run.
        # When stale, last_synced_at is left untouched.
        #
        # Dedup columns are written only when we actually alerted this cycle;
        # when alert_level is NORMAL we CLEAR them so a future re-entry alerts.
        conn2 = psycopg2.connect(os.getenv("DATABASE_URL"))
        cur2  = conn2.cursor()

        synced_clause = "last_synced_at = NOW()," if price_fresh else ""

        if alert_level == "NORMAL":
            # reset dedup state
            cur2.execute(f"""
                UPDATE {TABLE} SET
                    current_spread_value = %s,
                    current_value        = %s,
                    gross_pnl            = %s,
                    pnl_pct              = %s,
                    profit_pct_of_max    = %s,
                    {synced_clause}
                    last_alert_level     = NULL,
                    last_alert_at        = NULL,
                    last_alert_pnl_pct   = NULL
                WHERE id = %s AND UPPER(status) = 'OPEN'
            """, (spread_value, current_value, gross_pnl, pnl_pct,
                  profit_pct_max, pos["id"]))
        elif alerted_now:
            # record what we just alerted, so we can dedup next cycle
            cur2.execute(f"""
                UPDATE {TABLE} SET
                    current_spread_value = %s,
                    current_value        = %s,
                    gross_pnl            = %s,
                    pnl_pct              = %s,
                    profit_pct_of_max    = %s,
                    {synced_clause}
                    last_alert_level     = %s,
                    last_alert_at        = NOW(),
                    last_alert_pnl_pct   = %s
                WHERE id = %s AND UPPER(status) = 'OPEN'
            """, (spread_value, current_value, gross_pnl, pnl_pct,
                  profit_pct_max, alert_level, pnl_pct, pos["id"]))
        else:
            # alert level is WATCH/ACTION/URGENT but we didn't re-alert
            # (deduped): update P&L only, leave dedup state as-is.
            # synced_clause carries its own trailing comma when fresh, so it goes
            # at the FRONT of the SET list -- same pattern as the other branches.
            cur2.execute(f"""
                UPDATE {TABLE} SET
                    {synced_clause}
                    current_spread_value = %s,
                    current_value        = %s,
                    gross_pnl            = %s,
                    pnl_pct              = %s,
                    profit_pct_of_max    = %s
                WHERE id = %s AND UPPER(status) = 'OPEN'
            """, (spread_value, current_value, gross_pnl, pnl_pct,
                  profit_pct_max, pos["id"]))
        conn2.commit()
        cur2.close()
        conn2.close()

    print(f"{'=' * 60}")
    print(f"  Monitor [{mode}] completado — {timestamp}\n")


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULED RUN (Railway)
# ══════════════════════════════════════════════════════════════════════════════

def healthcheck_ping():
    """
    Ping al dead-man's switch (healthchecks.io). Lo llama el LOOP de
    run_monitor.py, no este módulo.

    POR QUÉ ESTO Y NO UN PUSH POR CICLO
        El monitor corre cada 5 minutos: avisar "estoy vivo" serían 78 pushes
        por día, y un canal que se ignora es un canal que no existe — el aviso
        urgente del stop se perdería entre el ruido.
        Y sobre todo: un proceso muerto NO PUEDE avisar que murió. El 17-jul el
        auto_run se saltó medio día y te enteraste porque no sonó el teléfono.
        La ausencia como señal sólo funciona si estás mirando.
        Acá el aviso viene de AFUERA: si el worker deja de pegar, healthchecks
        te avisa. Es el único que funciona con el worker caído.

    RESPONDE UNA SOLA PREGUNTA: ¿el proceso está vivo?
        No "¿el ciclo funcionó?". El loop pinga cada 60s pase lo que pase,
        mientras que scheduled_run corre cada 5min con el mercado abierto y cada
        30 con el mercado cerrado — un check atado al ciclo gritaría todas las
        noches y todos los fines de semana, y un check que da falsa alarma dos
        veces al día se ignora en una semana.

    LO QUE NO CUBRE, Y HAY QUE SABERLO
        Un ciclo que revienta SIEMPRE, con el loop girando, pinga "sano". Esa
        brecha hoy sólo se ve en los logs de Railway. Se intentó cubrir con un
        /fail desde scheduled_run, pero el OK del minuto siguiente lo borraba:
        dos preguntas distintas no entran en un solo check.

    Sin HEALTHCHECK_URL no hace nada y no molesta: es una red de seguridad
    opcional, no una dependencia.
    """
    url = os.getenv("HEALTHCHECK_URL", "").strip()
    if not url:
        return
    try:
        import requests
        requests.get(url.rstrip("/"), timeout=5)
    except Exception as e:
        # Que el ping falle NO puede tumbar el ciclo. Si healthchecks no es
        # alcanzable, va a avisar solo por la ausencia — que es su trabajo.
        print(f"  healthcheck ping falló: {e}")


def scheduled_run():
    # def · opcion B: DOS pasadas, live primero y paper despues. Cada una precia,
    # evalua y cierra su propio libro. Una estructura en ambos libros se precia dos
    # veces (una por pasada); es el precio de la simplicidad — sin agrupar por
    # estructura en el unico componente que cierra plata real.
    #
    # El cierre NO pasa por el interruptor de live (LIVE_TRADING_ENABLED): cerrar
    # siempre esta permitido. El interruptor solo frena APERTURAS (en auto_run).
    from executor import LiveExecutor, PaperExecutor

    for table, ex, label in (
        ("positions",       LiveExecutor(),  "live"),
        ("paper_positions", PaperExecutor(), "paper"),
    ):
        try:
            run_position_monitor(table, ex, label)
        except Exception as e:
            print(f"  monitor [{label}] error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor de posiciones")
    parser.add_argument("--loop",  action="store_true",
                        help="Correr en loop adaptativo (posiciones reales)")
    args = parser.parse_args()

    if args.loop:
        print(f"\n  Monitor en loop adaptativo")
        print(f"  Mercado abierto:  cada {INTERVAL_MARKET_OPEN}min")
        print(f"  Pre-market:       cada {INTERVAL_PRE_MARKET}min")
        print(f"  Mercado cerrado:  cada {INTERVAL_MARKET_CLOSED}min\n")
        scheduled_run()
        current_interval = get_interval()
        schedule.every(current_interval).minutes.do(scheduled_run)

        while True:
            schedule.run_pending()
            new_interval = get_interval()
            if new_interval != current_interval:
                schedule.clear()
                schedule.every(new_interval).minutes.do(scheduled_run)
                current_interval = new_interval
                print(f"  Intervalo ajustado: {current_interval}min")
            time.sleep(60)
    else:
        scheduled_run()   # def: una corrida de las dos pasadas (live + paper)