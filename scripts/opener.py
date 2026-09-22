"""
scripts/opener.py  (v2-bidi)
============================
OPENER AUTÓNOMO — última pieza del pipeline (paso 3 del espejo de auto_run).
Toma las candidatas de selection_result y las abre según el embudo de dos etapas
(diseño del owner, LOGICA_DEL_OPENER.md):

    1. La SEÑAL rankea  — ordena por fuerza relativa en la dirección propia, desde
       el dossier (barato, sin cotizar cadenas).
    2. La CALIDAD y LIQUIDEZ vetan — al bajar la lista se cotiza SOLO lo que se va a
       abrir; los builders (option_selector) ya vetan R/R, POP y liquidez: si no hay
       pierna válida, la candidata se descarta y se sigue.
    3. Los GATES DE CARTERA recortan — el executor corre gates_for_open (sector +
       riesgo total + no-apilar + delta neto) por libro, y actualiza la cartera tras
       cada apertura (el candidato N se mide contra la cartera después de abrir N-1).
    4. PARADA — al alcanzar max_opens, agotar la lista, o cuando los gates rechazan
       lo que queda.

NO hay score compuesto: la señal rankea, la economía veta, los gates recortan —
cada criterio en su etapa. La diversificación EMERGE de los gates, no del ranking.

Alcance actual: las 4 SPREADS (Bull/Bear Call/Put). Los longs (1 pata) se saltean
con aviso — se suman cuando el executor/gates ramifiquen 1 pata.

USO (PowerShell, venv trading_env, carpeta scripts/)
    python opener.py --run              # dry-run: rankea y muestra qué abriría
    python opener.py --run --commit     # abre en paper hasta el presupuesto
    python opener.py --run --max 5      # limita cuántas abrir esta corrida
"""
import os
import sys
import asyncio
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

from selection import load_dossier  # noqa: E402

# strategy -> (clave en el resultado de _fetch_option_data, familia)
STRATEGY_INFO = {
    "Bull Call Spread": ("spreads",           "debit"),
    "Bear Put Spread":  ("bear_put_spreads",  "debit"),
    "Bull Put Spread":  ("put_spreads",       "credit"),
    "Bear Call Spread": ("bear_call_spreads", "credit"),
    "Long Call":        ("long_calls",        "long"),
    "Long Put":         ("long_puts",         "long"),
}
SPREAD_STRATEGIES = {"Bull Call Spread", "Bear Put Spread", "Bull Put Spread", "Bear Call Spread"}


def _conn():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def load_candidates(cur):
    """Candidatas (status='candidate') del último scan, con strategy y dirección."""
    cur.execute("""
        WITH latest AS (SELECT MAX(scan_at) AS m FROM selection_result)
        SELECT id, ticker, direction, strategy, size_factor
        FROM selection_result
        WHERE status = 'candidate' AND scan_at = (SELECT m FROM latest)
    """)
    return [{"selection_id": r[0], "ticker": r[1], "direction": r[2],
             "strategy": r[3], "size_factor": float(r[4]) if r[4] is not None else None}
            for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════════════════════════
# PASO 1 — RANKING POR SEÑAL (fuerza relativa en la dirección propia, del dossier)
# ══════════════════════════════════════════════════════════════════════════════

def signal_strength(f, direction, rs_cap=None):
    """
    Fuerza de la señal EN LA DIRECCIÓN de la candidata (mayor = más fuerte).
    Primaria: rs_vs_sector (fuerza relativa vs el sector). En UPTREND, rs alto es
    fuerte; en DOWNTREND, rs bajo (más negativo) es fuerte bajista -> se invierte el
    signo para ordenar ambas direcciones en la misma escala.

    Cap de outliers (rs_cap): un rs con magnitud desmesurada (p.ej. +143 cuando el
    resto ronda +30) se TRUNCA al techo, para que un par de nombres extremos no
    dominen el ranking y acaparen el presupuesto antes de que entren otras señales
    fuertes. A partir de cierto punto "muy fuerte" es muy fuerte: distinguir +143 de
    +60 no aporta y sí distorsiona. None = sin cap.
    """
    rs = f.get("rs_vs_sector")
    rs = 0.0 if rs is None else float(rs)
    if rs_cap is not None and rs_cap > 0:
        rs = max(-rs_cap, min(rs, rs_cap))
    return rs if direction == "UPTREND" else -rs


def _tiebreak(f):
    """Desempate: momentum (|pct_change| mayor primero)."""
    pct = f.get("pct_change")
    return abs(float(pct)) if pct is not None else 0.0


def rank_candidates(candidates, dossier):
    """Ordena por señal (primaria) + momentum (desempate), descendente."""
    from system_state import get_param_float
    rs_cap = get_param_float("rs_cap", 50.0)   # techo de fuerza relativa (cap de outliers)
    for c in candidates:
        f = dossier.get(c["ticker"], {})
        c["_strength"] = signal_strength(f, c["direction"], rs_cap)
        c["_tiebreak"] = _tiebreak(f)
    candidates.sort(key=lambda c: (c["_strength"], c["_tiebreak"]), reverse=True)
    return candidates


# ══════════════════════════════════════════════════════════════════════════════
# PUERTAS DE CALIDAD (pass/fail) — se aplican ANTES del ranking, sobre cada
# candidata. NO ponderan: cada una descarta o deja pasar. Son direccionales y
# simétricas (alcista/bajista espejo). Umbrales en system_state, calibrables.
# La primaria (signal_strength) sigue decidiendo el ORDEN de las que pasan.
# ══════════════════════════════════════════════════════════════════════════════

def gate_reason(cand, dossier):
    """
    Devuelve None si la candidata pasa las 3 puertas, o un string con el motivo
    del descarte. Direccional: 'up' = UPTREND (alcista), 'down' = DOWNTREND.
    """
    from system_state import get_param_float
    f = dossier.get(cand["ticker"], {})
    up = cand["direction"] == "UPTREND"

    # Puerta A — Recorrido disponible (52w position_pct): no comprar fuerza que ya
    # llegó al techo, ni shortear debilidad que ya tocó el piso.
    pos_hi = get_param_float("gate_pos_pct_high", 90.0)
    pos_lo = get_param_float("gate_pos_pct_low", 10.0)
    pos = f.get("position_pct")
    if pos is not None:
        if up and pos > pos_hi:
            return f"puerta A: pegada al máximo 52w ({pos:.0f}% > {pos_hi:.0f})"
        if not up and pos < pos_lo:
            return f"puerta A: pegada al mínimo 52w ({pos:.0f}% < {pos_lo:.0f})"

    # Puerta B — RSI extremo: veta solo lo genuinamente tarde (no lo moderado).
    rsi_hi = get_param_float("gate_rsi_high", 78.0)
    rsi_lo = get_param_float("gate_rsi_low", 22.0)
    rsi = f.get("rsi")
    if rsi is not None:
        if up and rsi > rsi_hi:
            return f"puerta B: RSI sobrecompra ({rsi:.0f} > {rsi_hi:.0f})"
        if not up and rsi < rsi_lo:
            return f"puerta B: RSI sobreventa ({rsi:.0f} < {rsi_lo:.0f})"

    # Puerta C — Fuerza relativa vs el MERCADO (rs_vs_spy): descartar el alcista
    # más débil que el mercado, o el bajista más fuerte que el mercado. Umbral 0
    # de arranque (a calibrar); distingue fuerte-en-todo de fuerte-solo-en-sector.
    rs_mkt_up   = get_param_float("gate_rs_spy_up", 0.0)    # alcista: exige rs_vs_spy >= este
    rs_mkt_down = get_param_float("gate_rs_spy_down", 0.0)  # bajista: exige rs_vs_spy <= este
    rs_spy = f.get("rs_vs_spy")
    if rs_spy is not None:
        if up and rs_spy < rs_mkt_up:
            return f"puerta C: más débil que el mercado (rs_vs_spy {rs_spy:.1f} < {rs_mkt_up:.1f})"
        if not up and rs_spy > rs_mkt_down:
            return f"puerta C: más fuerte que el mercado (rs_vs_spy {rs_spy:.1f} > {rs_mkt_down:.1f})"

    return None


# ══════════════════════════════════════════════════════════════════════════════
# PASO 2 — COTIZAR (lazy) la mejor pierna y armar el OpenIntent
# ══════════════════════════════════════════════════════════════════════════════

def _best_leg(result, strategy):
    key, _ = STRATEGY_INFO[strategy]
    legs = result.get(key, [])
    return legs[0] if legs else None


def _to_order(leg, strategy, exp_date):
    _, family = STRATEGY_INFO[strategy]
    if family == "long":
        # 1 pata: strike_high None (señal de long), debit = prima pagada (positiva).
        return {"strike_low": leg["strike"], "strike_high": None,
                "debit": round(leg["mid"], 2), "expiration": exp_date}
    lo = min(leg["long_strike"], leg["short_strike"])
    hi = max(leg["long_strike"], leg["short_strike"])
    debit = leg["net_debit"] if family == "debit" else -leg["net_credit"]
    return {"strike_low": lo, "strike_high": hi, "debit": round(debit, 2),
            "expiration": exp_date}


async def _build_legs(ticker, price, strategy):
    from tastytrade import Session
    import option_selector as osel
    cs = os.getenv("TASTYTRADE_CLIENT_SECRET"); rt = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not cs or not rt:
        return None
    session = Session(cs, rt)
    return await osel._fetch_option_data(session, ticker, price, strategy)


def build_intent(cand, price):
    """
    Cotiza la cadena y arma el OpenIntent de la mejor pierna (spread), o None si no
    hay pierna válida (los builders vetaron por R/R, POP o liquidez).
    """
    from executor import OpenIntent
    strategy = cand["strategy"]
    result = asyncio.run(_build_legs(cand["ticker"], price, strategy))
    if result is None:
        return None
    leg = _best_leg(result, strategy)
    if leg is None:
        return None                        # veto de calidad/liquidez (builder vacío)
    o = _to_order(leg, strategy, result["exp_date"])
    return OpenIntent(
        ticker=cand["ticker"], strike_low=o["strike_low"], strike_high=o["strike_high"],
        expiration=str(o["expiration"]), debit=o["debit"],
        strategy=strategy, selection_id=cand["selection_id"],
        price_at_open=float(price or 0.0),   # spot del subyacente al abrir (del scan)
    )


# ══════════════════════════════════════════════════════════════════════════════
# PASOS 3-4 — bajar la lista, gates (en el executor) + abrir, parar
# ══════════════════════════════════════════════════════════════════════════════

import io                                 # noqa: E402
import contextlib                         # noqa: E402


class _QuietIO(io.StringIO):
    """StringIO que tolera .reconfigure() — trade.py lo llama al importarse."""
    def reconfigure(self, *args, **kwargs):
        return None


def _quiet(fn):
    """Corre fn silenciando su stdout (el detalle del builder/pricing/executor)."""
    with contextlib.redirect_stdout(_QuietIO()):
        return fn()


def _open_quiet(intent, book="paper"):
    """
    Registra la apertura en el libro `book`. Devuelve (ok, reason).

    paper: corre los gates una vez y registra directo con cmd_paper_buy (sin
    re-correr gates -> sin duplicar el fetch de deltas).
    live:  delega en LiveExecutor.open_position, que corre los gates de v2, el
    INTERRUPTOR (LIVE_TRADING_ENABLED + kill-flag) y manda la orden al broker.
    En live NO se silencia: en la apertura real queremos ver el fill y los motivos.
    """
    if book == "live":
        from executor import LiveExecutor, OpenIntent  # noqa: F401
        try:
            ok = LiveExecutor().open_position(intent)   # imprime fill/motivo/interruptor
            return (ok, None if ok else "no abierta en live (ver detalle arriba)")
        except Exception as e:
            return (False, f"live open failed: {e}")

    import portfolio
    allowed, reason = _quiet(lambda: portfolio.gates_for_open(
        "paper", intent.ticker, intent.strategy,
        intent.strike_low, intent.strike_high, intent.debit, intent.expiration))
    if not allowed:
        return (False, reason)

    # Gates OK -> registrar directo (sin re-correr gates).
    import trade as trade_module
    try:
        if intent.strike_high is None:                    # long de 1 pata
            _quiet(lambda: trade_module.cmd_paper_buy_single(
                intent.ticker, intent.strike_low, intent.expiration, intent.debit,
                price_at_open=intent.price_at_open,
                strategy=intent.strategy, selection_id=intent.selection_id))
        else:                                             # spread. Slippage simple: mid+1c.
            debit_fill = round(intent.debit + 0.01, 2)
            _quiet(lambda: trade_module.cmd_paper_buy(
                intent.ticker, intent.strike_low, intent.strike_high, intent.expiration,
                debit_fill, price_at_open=intent.price_at_open,
                strategy=intent.strategy, selection_id=intent.selection_id))
        return (True, None)
    except Exception as e:
        return (False, f"open failed: {e}")


def run_opener(commit, max_opens, book="paper"):
    conn = _conn(); cur = conn.cursor()
    candidates = load_candidates(cur)
    dossier    = load_dossier(cur, "scan")
    cur.close(); conn.close()

    if not candidates:
        print("  no candidates in selection_result. Run the pipeline first.")
        return 1

    # PUERTAS DE CALIDAD (pass/fail) — filtran el conjunto ANTES del ranking.
    kept, dropped = [], 0
    for c in candidates:
        reason = gate_reason(c, dossier)
        if reason is None:
            kept.append(c)
        else:
            dropped += 1
    if dropped:
        print(f"  puertas de calidad: {dropped} descartadas · {len(kept)} pasan")
    candidates = kept
    if not candidates:
        print("  ninguna candidata pasó las puertas de calidad.")
        return 0

    ranked = rank_candidates(candidates, dossier)
    print(f"\n  OPENER — {len(ranked)} candidates ranked by signal "
          f"({'COMMIT' if commit else 'dry-run'}, max {max_opens})")

    from executor import OpenIntent  # noqa: F401  (build_intent lo usa)
    opened    = 0
    evaluated = 0

    for c in ranked:
        # Parada: en commit corta al abrir max_opens; en dry-run corta al evaluar
        # max_opens (si no, cotizaría las ~250 cadenas, lento e inútil).
        if (commit and opened >= max_opens) or (not commit and evaluated >= max_opens):
            print(f"\n  stop: reached max {max_opens} ({'opened' if commit else 'evaluated'}).")
            break
        price = (dossier.get(c["ticker"]) or {}).get("price")
        if price is None:
            continue

        evaluated += 1
        tag = f"{c['ticker']:<6} {c['strategy']:<18} rs={c['_strength']:+.2f}"
        intent = _quiet(lambda: build_intent(c, price))   # silencia el detalle del builder
        if intent is None:
            print(f"     {tag}  — no valid leg (quality/liquidity veto)")
            continue

        if intent.strike_high is None:                    # long de 1 pata
            label = f"${intent.strike_low}"
            econ  = f"premium {intent.debit}"
        else:                                             # spread
            label = f"${intent.strike_low}/{intent.strike_high}"
            econ  = f"debit {intent.debit}" if intent.debit > 0 else f"credit {-intent.debit}"

        if not commit:
            print(f"     {tag}  → {label} {econ}  [would try]")
            continue

        ok, reason = _open_quiet(intent, book)            # gates + registra; (ok, motivo)
        if ok:
            opened += 1
            print(f"     {tag}  → {label} {econ}  ✅ OPENED")
        else:
            print(f"     {tag}  → gate: {reason}")

    print(f"\n  evaluated {evaluated} · opened {opened}"
          f"{' (dry-run: nothing opened)' if not commit else ''}")

    # LIVE: la orden quedó en el broker, no en `positions`. run_sync la baja para
    # que el monitor la vigile. En paper no hace falta (cmd_paper_buy ya escribió).
    if book == "live" and commit and opened > 0:
        from executor import LiveExecutor
        print("\n  [live] bajando del broker a positions (sync)...")
        LiveExecutor().sync_after_opens()

    return 0


def main():
    p = argparse.ArgumentParser(description="Autonomous opener (paso 3 del espejo de auto_run)")
    p.add_argument("--run", action="store_true", required=True, help="run the opener")
    p.add_argument("--commit", action="store_true", help="actually open (default: dry-run)")
    p.add_argument("--max", type=int, default=10, help="max positions to open this run")
    p.add_argument("--live", action="store_true",
                   help="abre en LIVE (usa LiveExecutor; el interruptor LIVE_TRADING_ENABLED "
                        "sigue decidiendo si la orden llega al broker). Default: paper")
    a = p.parse_args()
    book = "live" if a.live else "paper"

    print(f"\n{'═'*55}")
    print(f"  OPENER (Etapa 6) — {book}")
    print(f"{'═'*55}")
    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1
    return run_opener(a.commit, a.max, book)


if __name__ == "__main__":
    sys.exit(main())