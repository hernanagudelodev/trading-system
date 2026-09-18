"""
scripts/portfolio.py  (v2-bidi)
===============================
CAPA DE CARTERA — el gate final: decide si una candidata "cabe" en el portafolio
dado lo que YA está abierto. No juzga si la opción es buena (eso ya lo hicieron
Estudio y Selección); juzga el encaje. Es determinista y fail-closed.

Tres ejes:
    1. Concentración por sector      (ya existe en def · reúso)          [TODO]
    2. Delta neto direccional        (NUEVO · el eje bidireccional)      <- EN CURSO
    3. Riesgo total vs NLV           (ya existe en def · verificar)      [TODO]

§2 — DELTA NETO DIRECCIONAL
    Mide cuánto apuesta el libro entero a que SUBE vs BAJA. Al volverse el sistema
    bidireccional, hace falta para no quedar todo cargado a un lado sin querer.

    delta de una posición = (delta_pata_larga - delta_pata_corta) × contratos × 100
        · long de 1 pata: la pata corta es 0.
        · los deltas entran con su SIGNO real (calls +, puts -).
    delta neto del libro  = suma sobre todas las posiciones OPEN.

    Convención de signo:
        positivo = libro NET LARGO  (gana si el mercado sube)
        negativo = libro NET CORTO  (gana si el mercado baja)

    Chequeo por estructura (con deltas típicos):
        Bull Call  (long +0.6 / short +0.3) -> +0.3 → largo   ✓ (alcista)
        Bear Call  (long +0.2 / short +0.4) -> -0.2 → corto   ✓ (bajista)
        Bull Put   (long -0.3 / short -0.4) -> +0.1 → largo   ✓ (alcista)
        Bear Put   (long -0.6 / short -0.3) -> -0.3 → corto   ✓ (bajista)
        Long Call  (+0.5 / 0)               -> +0.5 → largo   ✓
        Long Put   (-0.5 / 0)               -> -0.5 → corto   ✓

    El delta se lee FRESCO de Tastytrade (no vive en la tabla). Esa lectura y el
    gate contra un tope por régimen (topes en system_state) son los pasos que siguen.
"""
import os
import sys
import argparse
from pathlib import Path

from dotenv import load_dotenv

_THIS_DIR  = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
sys.path.insert(0, str(_THIS_DIR))

_ENV_NAME  = os.getenv("ENV_FILE", ".env.v2")
_ENV_PATH  = Path(_ENV_NAME)
if not _ENV_PATH.is_absolute():
    _ENV_PATH = _REPO_ROOT / _ENV_PATH
load_dotenv(_ENV_PATH)


def _conn():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


from system_state import get_param_float  # noqa: E402  (path ya seteado arriba)


# ══════════════════════════════════════════════════════════════════════════════
# §2 — DELTA NETO (matemática pura · sin red)
# ══════════════════════════════════════════════════════════════════════════════

def position_delta(delta_long, delta_short=0.0, contracts=1):
    """
    Delta de UNA posición = (delta_long - delta_short) × contratos × 100.
    Long de 1 pata: delta_short = 0. Los deltas entran con su signo real (call +, put -).
    Devuelve None si falta el delta de la pata larga (fail-closed honesto).
    """
    if delta_long is None:
        return None
    short = delta_short if delta_short is not None else 0.0
    return round((delta_long - short) * contracts * 100, 2)


def book_net_delta(positions):
    """
    Delta neto del libro = suma de position_delta sobre las posiciones OPEN.
    positions: lista de dicts {delta_long, delta_short, contracts}.
    Positivo = net largo (gana si sube); negativo = net corto (gana si baja).

    Devuelve (net_delta, incomplete): incomplete es la lista de posiciones cuyo
    delta no se pudo calcular (falta dato) — no se suman, y quien llame decide qué
    hacer (fail-closed: un libro con deltas faltantes no debería autorizar más).
    """
    total = 0.0
    incomplete = []
    for p in positions:
        d = position_delta(p.get("delta_long"), p.get("delta_short", 0.0),
                            p.get("contracts", 1))
        if d is None:
            incomplete.append(p)
        else:
            total += d
    return round(total, 2), incomplete


# ══════════════════════════════════════════════════════════════════════════════
# LIBRO REAL — lee las posiciones OPEN de un libro y trae sus deltas frescos
# ══════════════════════════════════════════════════════════════════════════════
# Cada gate mide SU libro: paper mide paper_positions, live mide positions. Nunca
# se mezclan — un delta neto que sume ambos no corresponde a ningún libro real.

_BOOK_TABLE = {"paper": "paper_positions", "live": "positions"}

# familia put (lado) y geometría long/short por estructura.
_PUT_STRATEGIES = ("Bull Put Spread", "Bear Put Spread", "Long Put")
_BULL_SPREADS   = ("Bull Call Spread", "Bull Put Spread")   # long = strike bajo
_LONGS          = ("Long Call", "Long Put")


def _legs_of(strategy, strike_low, strike_high):
    """
    Deriva (long_strike, short_strike, option_type) de la estructura.
    - familia put/call por _PUT_STRATEGIES.
    - alcista: long = strike bajo; bajista: long = strike alto.
    - long de 1 pata: short_strike = None.
    """
    option_type = "put" if strategy in _PUT_STRATEGIES else "call"
    if strategy in _LONGS:
        return strike_low, None, option_type          # 1 pata (strike_high viene NULL)
    if strategy in _BULL_SPREADS:
        return strike_low, strike_high, option_type    # alcista: long = bajo
    return strike_high, strike_low, option_type        # bajista: long = alto


def build_book(book="paper"):
    """
    Lee las posiciones OPEN del libro pedido, trae los deltas frescos de sus patas
    y devuelve (net_delta, positions, incomplete). Cada posición del resultado
    lleva su delta calculado. book: 'paper' | 'live'.
    """
    import pricing
    table = _BOOK_TABLE.get(book)
    if table is None:
        raise ValueError(f"libro desconocido: {book!r} (usar 'paper' o 'live')")

    conn = _conn(); cur = conn.cursor()
    cur.execute(f"""
        SELECT id, ticker, strategy, strike_low, strike_high, expiration, contracts
        FROM {table}
        WHERE UPPER(status) = 'OPEN'
        ORDER BY opened_at
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()

    positions = []
    for pos_id, ticker, strategy, sl, sh, exp, contracts in rows:
        sl = float(sl) if sl is not None else None
        sh = float(sh) if sh is not None else None
        long_strike, short_strike, opt_type = _legs_of(strategy, sl, sh)

        delta_long  = pricing.get_single_delta(ticker, long_strike, exp, opt_type)
        delta_short = 0.0 if short_strike is None else \
                      pricing.get_single_delta(ticker, short_strike, exp, opt_type)

        d = position_delta(delta_long, delta_short, int(contracts))
        positions.append({
            "id": pos_id, "ticker": ticker, "strategy": strategy,
            "contracts": int(contracts), "delta_long": delta_long,
            "delta_short": delta_short, "position_delta": d,
        })

    net, incomplete = book_net_delta(positions)
    return net, positions, incomplete


def show_book(book="paper"):
    net, positions, incomplete = build_book(book)
    print(f"\n  {book.upper()} BOOK — {len(positions)} open position(s)")
    for p in positions:
        d = p["position_delta"]
        d_str = f"{d:+.2f}" if d is not None else "None (missing delta)"
        print(f"     {p['ticker']:<6} {p['strategy']:<18} x{p['contracts']}  "
              f"Δlong={p['delta_long']} Δshort={p['delta_short']}  ->  {d_str}")
    side = "NET LONG (gana si sube)" if net > 0 else \
           "NET SHORT (gana si baja)" if net < 0 else "NEUTRAL"
    print(f"\n  book net delta: {net:+.2f}  ->  {side}")
    if incomplete:
        print(f"  ⚠️  {len(incomplete)} position(s) with missing delta — fail-closed for new opens")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# GATE DE DELTA NETO (asimétrico por régimen · determinista)
# ══════════════════════════════════════════════════════════════════════════════
# Tope direccional: cuánto puede estar el libro NET LARGO y cuánto NET CORTO, según
# el régimen. Coherente con el dial (§4.2): en un régimen fuerte se tolera más carga
# a favor del tape y menos en contra. Topes del owner en system_state.

# Defaults por régimen (delta neto: max_long, max_short). Calibrables.
_DELTA_DEFAULTS = {
    "bullish": (500.0, 150.0),   # tolera cargar largo, limita corto
    "bearish": (150.0, 500.0),   # espejo
    "neutral": (300.0, 300.0),   # parejo y acotado
}


def _delta_limits(regime):
    """(max_long, max_short) para el régimen, desde system_state (o defaults)."""
    r = (regime or "NEUTRAL").lower()
    dl, ds = _DELTA_DEFAULTS.get(r, (300.0, 300.0))
    max_long  = get_param_float(f"delta_max_long_{r}",  dl)
    max_short = get_param_float(f"delta_max_short_{r}", ds)
    return max_long, max_short


def delta_gate(current_net, candidate_delta, regime):
    """
    ¿Abrir la candidata deja el libro dentro de los topes direccionales del régimen?
    max_long acota el delta neto positivo; max_short acota el negativo (|corto|).
    Devuelve (allowed: bool, reason: str|None, resulting_net: float).
    """
    max_long, max_short = _delta_limits(regime)
    resulting = round(current_net + candidate_delta, 2)
    if resulting > max_long:
        return (False, f"net delta {resulting:+.0f} > max long {max_long:.0f} ({regime})", resulting)
    if resulting < -max_short:
        return (False, f"net delta {resulting:+.0f} < max short -{max_short:.0f} ({regime})", resulting)
    return (True, None, resulting)


# ══════════════════════════════════════════════════════════════════════════════
# GATE DE CARTERA (sector + riesgo total + no-apilar · determinista, fail-closed)
# ══════════════════════════════════════════════════════════════════════════════
# Reutiliza la lógica de _cartera_gates de def. Mide SU libro. El sector sale del
# CSV por ticker (get_sp500_sectors), no de una columna. Topes en system_state:
# max_portfolio_risk_pct, max_sector_risk_pct (deben estar seteados, o revienta).

_SECTORS_CACHE = None


def _sectors_map():
    """{ticker: sector} del CSV de constituents, cacheado por corrida."""
    global _SECTORS_CACHE
    if _SECTORS_CACHE is None:
        from universe import get_sp500_sectors
        _SECTORS_CACHE = get_sp500_sectors() or {}
    return _SECTORS_CACHE


def read_book_risk(book="paper"):
    """
    Lee las posiciones OPEN del libro y agrega el riesgo (position_max_loss) total y
    por sector. Devuelve (current_risk, sector_risk, open_tickers) o levanta.
    Un long (strike_high NULL) se valúa con width 0 -> riesgo = prima.
    """
    from option_selector import position_max_loss
    table = _BOOK_TABLE.get(book)
    if table is None:
        raise ValueError(f"libro desconocido: {book!r}")
    sectors = _sectors_map()

    conn = _conn(); cur = conn.cursor()
    cur.execute(f"""
        SELECT UPPER(ticker), strike_low, strike_high, premium_paid, contracts
        FROM {table} WHERE UPPER(status) = 'OPEN'
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()

    current_risk = 0.0
    sector_risk  = {}
    open_tickers = set()
    for tk, sl, sh, prem, contracts in rows:
        open_tickers.add(tk)
        sh_val = float(sh) if sh is not None else float(sl)      # long: width 0
        risk = position_max_loss(float(sl), sh_val, float(prem), int(contracts))
        current_risk += risk
        sec = sectors.get(tk, "Other")
        sector_risk[sec] = sector_risk.get(sec, 0.0) + risk
    return round(current_risk, 2), sector_risk, open_tickers


def cartera_gates(book, ticker, strike_low, strike_high, debit):
    """
    Gate determinista de cartera para abrir `ticker` en `book`. Devuelve
    (allowed: bool, reason: str|None). Fail-closed ante error o dato faltante.
    Chequea, en orden: DB legible, no apilar ticker, riesgo total, riesgo por sector.
    """
    from option_selector import (get_account_nlv, position_max_loss,
                                  portfolio_risk_pct, max_sector_risk_pct)
    ticker  = ticker.upper()
    sectors = _sectors_map()

    # 1. Lectura de cartera (fail-closed si la DB no responde)
    try:
        current_risk, sector_risk, open_tickers = read_book_risk(book)
    except Exception as e:
        return (False, f"no se pudo leer la cartera ({book}): {e} — no se abre")

    # 2. No apilar el mismo ticker
    if ticker in open_tickers:
        return (False, f"{ticker} ya tiene posición abierta — no apilar mismo nombre")

    # Topes desde system_state (revientan si faltan -> fail-closed)
    try:
        capital  = get_account_nlv()
        max_port = capital * portfolio_risk_pct() / 100.0
        max_sect = capital * max_sector_risk_pct() / 100.0
    except Exception as e:
        return (False, f"no se pudieron leer los topes de riesgo: {e} — no se abre")

    # Riesgo de la candidata. Long (strike_high None): la prima pagada (debit>0).
    if strike_high is None:
        new_risk = round(debit * 100, 2)
    else:
        new_risk = position_max_loss(strike_low, strike_high, debit)

    # 3. Riesgo total vs NLV
    if current_risk + new_risk > max_port:
        return (False, f"riesgo total ${current_risk:,.0f} + ${new_risk:,.0f} > "
                       f"${max_port:,.0f} (tope cartera)")

    # 4. Riesgo por sector (sector de la candidata desde el CSV; fail-closed si no resuelve)
    cand_sector = sectors.get(ticker)
    if cand_sector in (None, "Other", ""):
        return (False, f"{ticker}: sector no resuelto ({cand_sector!r}) — fail-closed, no se abre")
    sec_now = sector_risk.get(cand_sector, 0.0)
    if sec_now + new_risk > max_sect:
        return (False, f"sector {cand_sector} ${sec_now:,.0f} + ${new_risk:,.0f} > "
                       f"${max_sect:,.0f} (tope sector)")

    return (True, None)


# ══════════════════════════════════════════════════════════════════════════════
# GATE COMBINADO — todos los ejes de cartera para UNA apertura candidata
# ══════════════════════════════════════════════════════════════════════════════

def intent_delta(ticker, strategy, strike_low, strike_high, expiration, contracts=1):
    """Delta de una posición candidata (aún no abierta), con deltas frescos de sus patas."""
    import pricing
    long_strike, short_strike, opt_type = _legs_of(strategy, strike_low, strike_high)
    delta_long  = pricing.get_single_delta(ticker, long_strike, expiration, opt_type)
    delta_short = 0.0 if short_strike is None else \
                  pricing.get_single_delta(ticker, short_strike, expiration, opt_type)
    return position_delta(delta_long, delta_short, contracts)


def _current_regime(slot="scan"):
    """Régimen del último scan (ticker_study.regime)."""
    try:
        conn = _conn(); cur = conn.cursor()
        cur.execute("SELECT regime FROM ticker_study WHERE slot = %s ORDER BY scan_at DESC LIMIT 1",
                    (slot,))
        row = cur.fetchone(); cur.close(); conn.close()
        return row[0] if row else None
    except Exception:
        return None


def gates_for_open(book, ticker, strategy, strike_low, strike_high, debit, expiration, contracts=1):
    """
    Corre TODOS los gates de cartera para abrir `ticker` en `book`, en orden:
      1. sector + riesgo total + no-apilar (cartera_gates) — barato (DB + CSV).
      2. delta neto direccional (delta_gate) — trae deltas frescos.
    Mide SU libro. Devuelve (allowed: bool, reason: str|None). Fail-closed.
    """
    ok, reason = cartera_gates(book, ticker, strike_low, strike_high, debit)
    if not ok:
        return (False, reason)

    cand_delta = intent_delta(ticker, strategy, strike_low, strike_high, expiration, contracts)
    if cand_delta is None:
        return (False, "delta de la candidata no disponible — fail-closed")

    current_net, _, incomplete = build_book(book)
    if incomplete:
        return (False, f"{len(incomplete)} posicion(es) del libro con delta faltante — fail-closed")

    regime = _current_regime()
    ok, reason, _ = delta_gate(current_net, cand_delta, regime)
    if not ok:
        return (False, reason)
    return (True, None)


def show_risk(book="paper"):
    from option_selector import get_account_nlv, portfolio_risk_pct, max_sector_risk_pct
    try:
        current_risk, sector_risk, open_tickers = read_book_risk(book)
    except Exception as e:
        print(f"  ⛔ {e}")
        return 1
    try:
        capital  = get_account_nlv()
        max_port = capital * portfolio_risk_pct() / 100.0
        max_sect = capital * max_sector_risk_pct() / 100.0
    except Exception as e:
        print(f"  ⛔ topes no configurados en system_state: {e}")
        print(f"     setear max_portfolio_risk_pct y max_sector_risk_pct.")
        return 1

    print(f"\n  {book.upper()} BOOK RISK — NLV ${capital:,.0f}")
    print(f"  total risk ${current_risk:,.0f} / ${max_port:,.0f} (portfolio cap)")
    print(f"  open tickers: {', '.join(sorted(open_tickers)) if open_tickers else '(none)'}")
    if sector_risk:
        print(f"\n  by sector (cap ${max_sect:,.0f}):")
        for sec, risk in sorted(sector_risk.items(), key=lambda x: -x[1]):
            print(f"     {sec:<24} ${risk:,.0f}")
    return 0


def main():
    p = argparse.ArgumentParser(description="Portfolio layer v2 (delta, sector, risk gates)")
    p.add_argument("--selftest", action="store_true", help="prueba la matemática del delta neto")
    p.add_argument("--book", choices=["paper", "live"],
                   help="delta neto real del libro (paper o live), con deltas frescos de TT")
    p.add_argument("--delta-gate", dest="delta_gate", action="store_true",
                   help="prueba el gate de delta neto con casos sintéticos")
    p.add_argument("--risk", choices=["paper", "live"],
                   help="estado de riesgo del libro (total + por sector) vs topes")
    a = p.parse_args()

    if a.selftest:
        book = [
            {"strategy": "Bull Call Spread", "delta_long": 0.60, "delta_short": 0.30, "contracts": 1},
            {"strategy": "Bear Call Spread", "delta_long": 0.20, "delta_short": 0.40, "contracts": 1},
            {"strategy": "Long Put",         "delta_long": -0.50, "delta_short": 0.0, "contracts": 2},
        ]
        print("\n  per-position delta:")
        for pos in book:
            d = position_delta(pos["delta_long"], pos["delta_short"], pos["contracts"])
            print(f"     {pos['strategy']:<18} {d:+.2f}")
        net, incomplete = book_net_delta(book)
        side = "NET LONG (gana si sube)" if net > 0 else "NET SHORT (gana si baja)" if net < 0 else "NEUTRAL"
        print(f"\n  book net delta: {net:+.2f}  ->  {side}")
        if incomplete:
            print(f"  incomplete (missing delta): {len(incomplete)}")
        return 0

    if a.book:
        if not os.getenv("DATABASE_URL"):
            print(f"  missing DATABASE_URL (check {_ENV_PATH})")
            return 1
        return show_book(a.book)

    if a.delta_gate:
        cases = [
            ("BULLISH · add long, ok",        400.0,   50.0, "BULLISH"),
            ("BULLISH · add long, over",      480.0,   50.0, "BULLISH"),
            ("BULLISH · add short, tight",   -100.0,  -60.0, "BULLISH"),
            ("BEARISH · add short, ok",      -400.0,  -50.0, "BEARISH"),
            ("NEUTRAL · within",              200.0,   50.0, "NEUTRAL"),
            ("NEUTRAL · over",                280.0,   50.0, "NEUTRAL"),
        ]
        print("\n  DELTA GATE — asymmetric limits by regime")
        for desc, cur_net, cand, regime in cases:
            allowed, reason, resulting = delta_gate(cur_net, cand, regime)
            tag = "PASS" if allowed else "BLOCK"
            extra = "" if allowed else f"  ({reason})"
            print(f"     {desc:<28} net {cur_net:+.0f} + {cand:+.0f} = {resulting:+.0f}  -> {tag}{extra}")
        return 0

    if a.risk:
        if not os.getenv("DATABASE_URL"):
            print(f"  missing DATABASE_URL (check {_ENV_PATH})")
            return 1
        return show_risk(a.risk)

    print("  nothing to do — try --selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())