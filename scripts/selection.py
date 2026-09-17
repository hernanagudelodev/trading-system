"""
scripts/selection.py  (v2-bidi)
===============================
CAPA DE SELECCIÓN — toma el dossier del Estudio (ticker_study + study_fact) y
decide, de forma casi 100% determinista, qué opciones sirven (bidireccionales).
Un único LLM acotado valida la dirección (§4.3); todo lo demás es determinista.

Lee el dossier DESDE LA DB (no lo recibe en memoria): el scanner mide y persiste,
Selección consume. Las dos capas quedan desacopladas (pasan datos por la DB).

ORDEN DE DECISIONES (CAPA_DE_SELECCION.md)
    1. Operability   (¿se puede tradear?)        [TODO]
    2. Relevance     (¿vale la pena?)            [TODO]
    3. Direction     (¿hacia qué lado?)          <- EN CURSO
       §4.1 dirección determinista  — LISTO
       §4.2 dial de régimen (GATE)  — LISTO
       §4.3 LLM validador           — LISTO (modo --validate)
       §4.4 perforación del Gate    [TODO]
    4. Strategy      (¿qué estructura?)          [TODO]
    5. Builders      (¿qué piernas?)             [TODO]

§4.1 — DIRECCIÓN por CONFLUENCIA
    UPTREND    : precio > SMA50 y > SMA200 y trend_25d positivo
    DOWNTREND  : precio < SMA50 y < SMA200 y trend_25d negativo
    LATERAL    : mezcla · None : falta dato -> fail-closed (no se opera)

USO (PowerShell, venv trading_env)
    python selection.py --direction        # reparto de dirección del último scan
    python selection.py --dial             # aplica el dial de régimen (GATE)
    python selection.py --validate TICKER  # LLM valida la dirección de un ticker

VARIABLES DE ENTORNO (en .env.v2): DATABASE_URL, ANTHROPIC_API_KEY (para --validate)
"""
import os
import sys
import json
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
_ENV_LOADED = load_dotenv(_ENV_PATH)

MARKET_TICKER = "__MARKET__"
AI_MODEL      = "claude-sonnet-4-6"   # mismo modelo que usa def para el LLM

from system_state import get_param_float, get_param_str, get_param_int  # noqa: E402  (path ya seteado arriba)


def _conn():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


# ══════════════════════════════════════════════════════════════════════════════
# LECTURA DEL DOSSIER (pivot del formato largo study_fact)
# ══════════════════════════════════════════════════════════════════════════════

def _value(num, bl, txt, is_null):
    """Reconstruye el valor de un hecho desde las columnas tipadas de study_fact."""
    if is_null:
        return None
    if bl is not None:
        return bl
    if num is not None:
        return num
    return txt


def load_dossier(cur, slot="scan"):
    """
    {ticker: {criterion: value}} del ÚLTIMO scan (max scan_at para ese slot),
    excluyendo la fila de mercado. Pivotea el formato largo a un dict por acción.
    Agrega price y sector, que viven en la CABECERA (ticker_study), no en study_fact.
    """
    cur.execute("""
        WITH latest AS (SELECT MAX(scan_at) AS m FROM ticker_study WHERE slot = %s)
        SELECT s.id, s.ticker, s.price, s.sector, f.criterion, f.value_num, f.value_bool, f.value_text, f.is_null
        FROM ticker_study s
        JOIN study_fact f ON f.study_id = s.id
        WHERE s.slot = %s
          AND s.scan_at = (SELECT m FROM latest)
          AND s.ticker <> %s
    """, (slot, slot, MARKET_TICKER))

    dossier = {}
    for study_id, ticker, price, sector, crit, num, bl, txt, is_null in cur.fetchall():
        d = dossier.setdefault(ticker, {})
        if "price" not in d:                       # campos de cabecera, una vez por ticker
            d["price"]      = float(price) if price is not None else None
            d["sector"]     = sector
            d["_study_id"]  = study_id             # FK a ticker_study (metadata, no un hecho)
        d[crit] = _value(num, bl, txt, is_null)
    return dossier


def load_regime(cur, slot="scan"):
    """Régimen del último scan (está en la cabecera ticker_study, igual en todas las filas)."""
    cur.execute("""
        SELECT regime FROM ticker_study
        WHERE slot = %s AND scan_at = (SELECT MAX(scan_at) FROM ticker_study WHERE slot = %s)
        LIMIT 1
    """, (slot, slot))
    row = cur.fetchone()
    return row[0] if row else None


def load_market(cur, slot="scan"):
    """
    Hechos del bloque de mercado (fila __MARKET__) del último scan: spy_*, vix_*,
    macro_*. El regime NO está acá (vive en la cabecera; usar load_regime).
    """
    cur.execute("""
        WITH latest AS (SELECT MAX(scan_at) AS m FROM ticker_study WHERE slot = %s)
        SELECT f.criterion, f.value_num, f.value_bool, f.value_text, f.is_null
        FROM ticker_study s
        JOIN study_fact f ON f.study_id = s.id
        WHERE s.slot = %s AND s.scan_at = (SELECT m FROM latest) AND s.ticker = %s
    """, (slot, slot, MARKET_TICKER))
    market = {}
    for crit, num, bl, txt, is_null in cur.fetchall():
        market[crit] = _value(num, bl, txt, is_null)
    return market


# ══════════════════════════════════════════════════════════════════════════════
# §2 — OPERABILIDAD (filtro NEUTRAL · nunca por dirección)
# ══════════════════════════════════════════════════════════════════════════════
# Descarta lo inoperable: banda de precio, volumen mínimo, liquidez mínima (proxy
# barato: liquidity_rating de TT). Historial suficiente e higiene (no halted/
# delisting) YA están garantizados: una acción está en el dossier solo si el
# scanner la midió con datos frescos. Es el filtro barato que recorta el universo
# antes de las decisiones caras (cadena de opciones + LLM). Umbrales del owner.

def operability(f, min_price, max_price, min_avg_volume, min_liquidity):
    """Devuelve (operable: bool, reason: str|None). Fail-closed: dato faltante -> no operable."""
    price = f.get("price")
    if price is None or not (min_price <= price <= max_price):
        return (False, f"price {price} out of band [{min_price}, {max_price}]")
    vol = f.get("volume_avg_20d")
    if vol is None or vol < min_avg_volume:
        return (False, f"avg volume {vol} < {min_avg_volume}")
    liq = f.get("liquidity_rating")
    if liq is None or liq < min_liquidity:
        return (False, f"liquidity_rating {liq} < {min_liquidity}")
    return (True, None)


def show_operability():
    min_price     = get_param_float("op_min_price", 5.0)
    max_price     = get_param_float("op_max_price", 2000.0)
    min_avg_vol   = get_param_float("op_min_avg_volume", 300000.0)
    min_liquidity = get_param_int("op_min_liquidity_rating", 2)

    conn = _conn(); cur = conn.cursor()
    dossier = load_dossier(cur, "scan")
    cur.close(); conn.close()
    if not dossier:
        print("  no scan dossier. Run the scanner (--scan --commit) first.")
        return 1

    print(f"\n  OPERABILITY (§2) — price [{min_price}, {max_price}] · "
          f"avg_vol >= {min_avg_vol:,.0f} · liquidity_rating >= {min_liquidity}")

    operable = []
    from collections import Counter
    reasons = Counter()
    for tk, f in dossier.items():
        ok, reason = operability(f, min_price, max_price, min_avg_vol, min_liquidity)
        if ok:
            operable.append(tk)
        else:
            # agrupa por el tipo de motivo (primera palabra clave)
            key = reason.split()[0] if reason else "other"
            reasons[key] += 1

    total = len(dossier)
    print(f"\n  operable: {len(operable)}/{total}  ·  discarded: {total - len(operable)}")
    for key, n in reasons.most_common():
        print(f"     discarded by {key:<12} {n}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# §3 — GATE MACRO × BETA (determinista · aguas arriba del LLM)
# ══════════════════════════════════════════════════════════════════════════════
# Descarta beta alta con un evento macro de alto impacto inminente. Va en CÓDIGO,
# antes del LLM, a propósito: es un riesgo que no se quiere que el LLM racionalice
# (el caso "beta 2,25 en día de CPI/FOMC" que el diseño cita). Umbrales del owner
# en system_state.

def earnings_gate(f, block_days):
    """
    §3: bloquea si hay earnings CONOCIDO dentro de la ventana (0..block_days).
    days_to_earnings None NO bloquea: suele ser ETF sin earnings o fecha no
    confirmada; TT da fechas confiables para el S&P, así que None ~ sin earnings
    inminente. Un earnings ya pasado (dte < 0) tampoco bloquea.
    Devuelve (blocked: bool, reason: str|None).
    """
    dte = f.get("days_to_earnings")
    if dte is None:
        return (False, None)
    if 0 <= dte <= block_days:
        return (True, f"earnings in {dte}d (<= {block_days})")
    return (False, None)


def macro_beta_gate(f, macro_next_high_days, beta_threshold, days_threshold):
    """
    Devuelve (blocked: bool, reason: str|None).
    - Sin evento inminente (macro_days None o > days_threshold) -> el gate NO aplica.
    - Con evento inminente: beta alta -> bloquea; beta desconocida -> bloquea
      (fail-closed, §3); beta baja -> pasa.
    """
    if macro_next_high_days is None or macro_next_high_days > days_threshold:
        return (False, None)                       # sin evento inminente

    beta = f.get("beta")
    if beta is None:
        return (True, f"macro event in {macro_next_high_days}d, beta unknown (fail-closed)")
    if beta > beta_threshold:
        return (True, f"beta {beta} > {beta_threshold} with macro event in {macro_next_high_days}d")
    return (False, None)                           # beta baja -> segura pese al evento


def show_macro_gate():
    beta_th = get_param_float("macro_gate_beta_threshold", 2.0)
    days_th = get_param_int("macro_gate_days_threshold", 2)

    conn = _conn(); cur = conn.cursor()
    dossier = load_dossier(cur, "scan")
    market  = load_market(cur, "scan")
    cur.close(); conn.close()
    if not dossier:
        print("  no scan dossier. Run the scanner (--scan --commit) first.")
        return 1

    macro_days  = market.get("macro_next_high_days")
    macro_event = market.get("macro_next_high_event")
    print(f"\n  MACRO×BETA GATE (§3) — next high-impact event: {macro_event} in {macro_days}d")
    print(f"  thresholds: beta > {beta_th} · within {days_th}d")

    blocked = []
    for tk, f in dossier.items():
        is_blocked, reason = macro_beta_gate(f, macro_days, beta_th, days_th)
        if is_blocked:
            blocked.append((tk, f.get("beta"), reason))

    total = len(dossier)
    print(f"\n  blocked: {len(blocked)}/{total}")
    for tk, beta, reason in sorted(blocked, key=lambda x: -(x[1] or 0))[:15]:
        print(f"     {tk:<6} beta={beta}  — {reason}")
    if not blocked:
        print("     (none — no high-beta names with an imminent macro event)")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# §4.1 — DIRECCIÓN DETERMINISTA POR CONFLUENCIA
# ══════════════════════════════════════════════════════════════════════════════

def direction(f):
    """
    UPTREND / DOWNTREND / LATERAL / None (fail-closed si falta dato).
    above_sma50/above_sma200 ya son precio>SMA; trend positivo = pct_change > 0.
    """
    a50 = f.get("above_sma50")
    a200 = f.get("above_sma200")
    pct = f.get("pct_change")
    if a50 is None or a200 is None or pct is None:
        return None                                  # fail-closed: sin dato, no se opera
    if a50 and a200 and pct > 0:
        return "UPTREND"
    if (not a50) and (not a200) and pct < 0:
        return "DOWNTREND"
    return "LATERAL"


def show_direction():
    conn = _conn(); cur = conn.cursor()
    dossier = load_dossier(cur, "scan")
    cur.close(); conn.close()
    if not dossier:
        print("  no scan dossier in the DB. Run the scanner (--scan --commit) first.")
        return 1

    buckets = {"UPTREND": [], "DOWNTREND": [], "LATERAL": [], None: []}
    for tk, f in dossier.items():
        buckets[direction(f)].append(tk)

    total = len(dossier)
    print(f"\n  DIRECTION (§4.1) on the latest scan — {total} stocks:")
    for label in ("UPTREND", "DOWNTREND", "LATERAL", None):
        tks = buckets[label]
        name = label if label else "None (fail-closed)"
        print(f"     {name:<20} {len(tks):>3}   ({len(tks)*100//total if total else 0}%)")

    if buckets["DOWNTREND"]:
        print(f"\n  DOWNTREND examples: {', '.join(buckets['DOWNTREND'][:12])}")
    if buckets[None]:
        print(f"  None examples: {', '.join(buckets[None][:12])}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# §4.2 — DIAL DE RÉGIMEN (modo GATE · determinista)
# ══════════════════════════════════════════════════════════════════════════════

def dial(stock_dir, regime, neutral_factor, mode="GATE"):
    """
    Cruza la dirección propia (§4.1) con el régimen del scan. Devuelve
    (passes: bool, size_factor: float).

    GATE:
      - régimen a favor de la dirección   -> (True, 1.0)
      - NEUTRAL con UPTREND o DOWNTREND   -> (True, neutral_factor)  [sizing reducido]
      - contra-tendencia                  -> (False, 0.0)  [bloqueado; perfora el LLM §4.4]
      - LATERAL / None (ya filtrados §4.1)-> (False, 0.0)

    Solo GATE está implementado. Otro modo -> falla explícito (no comportamiento raro).
    """
    if mode != "GATE":
        raise NotImplementedError(
            f"dial_mode={mode!r} not implemented — GATE only for now "
            f"(MODULA/INFORMACIONAL activan con evidencia futura)")

    if stock_dir not in ("UPTREND", "DOWNTREND"):
        return (False, 0.0)                      # LATERAL / None
    if regime == "BULLISH":
        return (True, 1.0) if stock_dir == "UPTREND" else (False, 0.0)
    if regime == "BEARISH":
        return (True, 1.0) if stock_dir == "DOWNTREND" else (False, 0.0)
    if regime == "NEUTRAL":
        return (True, neutral_factor)            # ambas, sizing reducido
    return (False, 0.0)                           # régimen desconocido -> fail-closed


def show_dial():
    mode           = get_param_str("dial_mode", "GATE")
    neutral_factor = get_param_float("neutral_size_factor", 0.5)

    conn = _conn(); cur = conn.cursor()
    dossier = load_dossier(cur, "scan")
    regime  = load_regime(cur, "scan")
    cur.close(); conn.close()
    if not dossier:
        print("  no scan dossier. Run the scanner (--scan --commit) first.")
        return 1

    print(f"\n  DIAL (§4.2) — regime={regime} · mode={mode} · neutral_size_factor={neutral_factor}")

    full, reduced, blocked, out = [], [], [], []
    try:
        for tk, f in dossier.items():
            stock_dir = direction(f)
            passes, factor = dial(stock_dir, regime, neutral_factor, mode)
            if not passes:
                (blocked if stock_dir in ("UPTREND", "DOWNTREND") else out).append(tk)
            elif factor >= 1.0:
                full.append(tk)
            else:
                reduced.append(tk)
    except NotImplementedError as e:
        print(f"  {e}")
        return 1

    total = len(dossier)
    print(f"\n  of {total} stocks:")
    print(f"     pass with-regime (size 1.0)        {len(full):>3}")
    print(f"     pass in NEUTRAL (size {neutral_factor})          {len(reduced):>3}")
    print(f"     blocked counter-trend (GATE)       {len(blocked):>3}")
    print(f"     out by direction (LATERAL/None)    {len(out):>3}")
    if blocked:
        print(f"\n  blocked counter-trend examples: {', '.join(blocked[:12])}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# §4.3 — LLM VALIDADOR DE DIRECCIÓN (agente único, contrato rígido)
# ══════════════════════════════════════════════════════════════════════════════
# Un único agente valida la dirección determinista de UNA acción contra evidencia
# EXTERNA (web search). No recibe régimen, no origina dirección, no decide operar.
# Las reglas duras se aplican en CÓDIGO (_validate_contract), no se confía en que el
# LLM las obedezca. Fail-closed: cualquier fallo -> resultado que NO puede perforar.

_VALIDATOR_PROMPT = """ROL: Sos un validador de dirección. Recibís UNA acción y su dirección
determinista ya calculada. Tu única tarea es decir si la evidencia EXTERNA
(noticias, resultados, guidance, M&A, regulación, litigios, shocks sectoriales
o macro específicos de este nombre) apoya o contradice esa dirección.

NO hacés:
- No calculás dirección desde cero (ya viene dada).
- No decidís si operar o no.
- No considerás el régimen de mercado.
- No analizás las señales técnicas (el sistema ya las vio).

ENTRADA:
- ticker: {ticker}
- direccion_deterministica: {direction}
- ventana: {window}

TAREA:
Usá web search para evaluar SOLO evidencia externa concreta y fechada sobre {ticker}
en la ventana dada. Ignorá opinión sin un hecho detrás. Un catalizador vale si es un
evento verificable con fuente.

SALIDA (JSON estricto, nada de texto fuera del JSON):
{{
  "assessment": "CONFIRM" | "VETO",
  "conviction": "LOW" | "MEDIUM" | "HIGH",
  "catalyst": "<evento + fuente>" | "NONE",
  "evidence": "<una línea citando el catalizador; vacío si NONE>"
}}

REGLAS DURAS:
- Sin catalizador externo concreto -> catalyst="NONE" y conviction="LOW".
  No inventes convicción desde las técnicas.
- HIGH solo con un catalizador externo fuerte, concreto y fechado, con fuente.
- VETO solo con evidencia externa que contradiga la dirección dada, no por corazonada.
- Nada fuera del JSON."""


def _fail_closed_result(reason):
    """Resultado que NO puede perforar el Gate (conviction LOW, catalyst NONE)."""
    return {"assessment": None, "conviction": "LOW", "catalyst": "NONE",
            "evidence": "", "error": reason}


def _validate_contract(raw):
    """
    Normaliza la salida del LLM y aplica la REGLA DURA en código: sin catalizador
    externo, conviction se fuerza a LOW (no se confía en la obediencia del LLM).
    """
    if not isinstance(raw, dict):
        return _fail_closed_result("respuesta no es un objeto JSON")

    assessment = raw.get("assessment")
    conviction = raw.get("conviction")
    catalyst   = raw.get("catalyst")
    evidence   = raw.get("evidence", "") or ""

    if assessment not in ("CONFIRM", "VETO"):
        assessment = None                         # contrato inválido -> sin veredicto
    if conviction not in ("LOW", "MEDIUM", "HIGH"):
        conviction = "LOW"
    if not catalyst or str(catalyst).strip().upper() == "NONE":
        catalyst = "NONE"

    # ── REGLA DURA (código): sin catalizador externo -> conviction <= LOW ───────
    if catalyst == "NONE":
        conviction = "LOW"

    return {"assessment": assessment, "conviction": conviction,
            "catalyst": catalyst, "evidence": evidence, "error": None}


def _call_llm(prompt, timeout=120):
    """POST a Anthropic con web search (patrón de def). Devuelve el texto o None."""
    import requests
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": AI_MODEL, "max_tokens": 1500,
                  "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout,
        )
        if resp.status_code != 200:
            print(f"  API error: {resp.status_code} — {resp.text[:160]}")
            return None
        text = "".join(b.get("text", "") for b in resp.json().get("content", [])
                       if b.get("type") == "text").strip()
        return text or None
    except Exception as e:
        print(f"  LLM error: {e}")
        return None


def _extract_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                return None
    return None


def validate_direction(ticker, det_direction, window_days=30):
    """
    §4.3: valida la dirección determinista de UNA acción contra evidencia externa.
    Devuelve dict validado {assessment, conviction, catalyst, evidence, error}.
    Fail-closed ante cualquier problema (nunca habilita una perforación por error).
    """
    if det_direction not in ("UPTREND", "DOWNTREND"):
        return _fail_closed_result(f"dirección no validable: {det_direction}")

    import datetime
    today = datetime.date.today()
    window = f"{today - datetime.timedelta(days=window_days)} a {today}"
    prompt = _VALIDATOR_PROMPT.format(ticker=ticker, direction=det_direction, window=window)

    text = _call_llm(prompt)
    if text is None:
        return _fail_closed_result("sin respuesta del LLM")
    raw = _extract_json(text)
    if raw is None:
        return _fail_closed_result("no se pudo extraer JSON de la respuesta")
    return _validate_contract(raw)


def show_validation(ticker):
    conn = _conn(); cur = conn.cursor()
    dossier = load_dossier(cur, "scan")
    cur.close(); conn.close()
    f = dossier.get(ticker)
    if f is None:
        print(f"  {ticker} is not in the latest scan.")
        return 1
    stock_dir = direction(f)
    print(f"  {ticker}: deterministic direction = {stock_dir}")
    if stock_dir not in ("UPTREND", "DOWNTREND"):
        print("  (LATERAL/None — no direction to validate)")
        return 0

    if not os.getenv("ANTHROPIC_API_KEY"):
        print(f"  missing ANTHROPIC_API_KEY in {_ENV_PATH}")
        return 1

    print("  querying LLM validator (with web search)...")
    r = validate_direction(ticker, stock_dir)
    print(f"\n  assessment : {r['assessment']}")
    print(f"  conviction : {r['conviction']}")
    print(f"  catalyst   : {r['catalyst']}")
    print(f"  evidence   : {r['evidence']}")
    if r.get("error"):
        print(f"  error      : {r['error']}  (fail-closed — cannot perforate)")
    # recordatorio del gate duro
    if r["conviction"] == "HIGH" and r["catalyst"] != "NONE":
        print("\n  -> meets both perforation conditions (§4.4): HIGH + external catalyst")
    else:
        print("\n  -> does NOT perforate the Gate (missing HIGH and/or external catalyst)")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# §4.4 — PERFORACIÓN DEL GATE (regla determinista alimentada por el LLM)
# ══════════════════════════════════════════════════════════════════════════════

def gate_perforation(llm_result, perforation_factor):
    """
    §4.4: regla DETERMINISTA que consume el veredicto del LLM (§4.3). Una
    contra-tendencia bloqueada por el dial (§4.2) perfora el Gate SOLO si se
    cumplen LAS DOS: conviction == HIGH y un catalizador externo citado
    (catalyst != NONE). La regla dura de §4.3 ya garantiza EN CÓDIGO que un HIGH
    sin catalizador es imposible, así que la perforación no se puede falsificar.

    Devuelve (perforates: bool, size_factor: float). Si perfora, entra reducido.
    """
    high     = llm_result.get("conviction") == "HIGH"
    catalyst = llm_result.get("catalyst") not in (None, "NONE", "")
    if high and catalyst:
        return (True, perforation_factor)
    return (False, 0.0)


def show_perforation(ticker):
    perf_factor    = get_param_float("perforation_size_factor", 0.5)
    neutral_factor = get_param_float("neutral_size_factor", 0.5)
    mode           = get_param_str("dial_mode", "GATE")

    conn = _conn(); cur = conn.cursor()
    dossier = load_dossier(cur, "scan")
    regime  = load_regime(cur, "scan")
    cur.close(); conn.close()
    f = dossier.get(ticker)
    if f is None:
        print(f"  {ticker} is not in the latest scan.")
        return 1

    stock_dir = direction(f)
    print(f"  {ticker}: direction={stock_dir} · regime={regime} · perforation_size_factor={perf_factor}")
    if stock_dir not in ("UPTREND", "DOWNTREND"):
        print("  (LATERAL/None — nothing to perforate)")
        return 0

    # Estado del dial, como contexto (la perforación solo importa cuando bloquea).
    passes, _ = dial(stock_dir, regime, neutral_factor, mode)
    print(f"  dial: {'PASSES (with-regime or NEUTRAL)' if passes else 'BLOCKED (counter-trend)'}")

    if not os.getenv("ANTHROPIC_API_KEY"):
        print(f"  missing ANTHROPIC_API_KEY in {_ENV_PATH}")
        return 1

    print("  querying LLM validator (with web search)...")
    r = validate_direction(ticker, stock_dir)
    print(f"\n  assessment : {r['assessment']}")
    print(f"  conviction : {r['conviction']}")
    print(f"  catalyst   : {r['catalyst']}")
    print(f"  evidence   : {r['evidence']}")
    if r.get("error"):
        print(f"  error      : {r['error']}")

    perforates, size = gate_perforation(r, perf_factor)
    if perforates:
        print(f"\n  -> gate_perforation: PERFORATES — enters at size {size} (HIGH + external catalyst)")
    else:
        print(f"\n  -> gate_perforation: does NOT perforate (needs HIGH + external catalyst)")
    if passes and perforates:
        print("     (note: the dial already passes this one; perforation only matters when BLOCKED)")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# §5 — SELECCIÓN DE ESTRATEGIA (volatilidad × dirección · espejo bidireccional)
# ══════════════════════════════════════════════════════════════════════════════
# estructura = f(volatilidad, dirección). El eje de volatilidad es el de def
# (agnóstico, mismos umbrales); lo nuevo es cruzarlo con la dirección para el
# espejo bajista. Fail-closed: sin IV o sin dirección operable -> None (NO el
# default alcista de def).

def select_strategy(f, stock_dir):
    """
    §5: elige la estructura. Espejo:
        IV >= 60 (vender prima)         : Bull Put Spread  / Bear Call Spread
        IV < 30 + momentum fuerte       : Long Call        / Long Put
        IV < 30 sin momentum, o 30-60   : Bull Call Spread / Bear Put Spread
    Momentum fuerte = beta>1.2 y |trend|>10 y rsi ok. RSI espeja: <65 (alcista,
    evita sobrecompra) / >35 (bajista, evita sobreventa).
    Devuelve el nombre de la estructura, o None (fail-closed).
    """
    if stock_dir not in ("UPTREND", "DOWNTREND"):
        return None
    ivp = f.get("iv_percentile")
    if ivp is None:
        return None                                # sin IV no hay estructura (no default de def)

    bull      = (stock_dir == "UPTREND")
    beta      = f.get("beta") or 0
    rsi       = f.get("rsi")
    trend_pct = abs(f.get("pct_change") or 0)

    if ivp >= 60:                                  # IV alta -> vender prima (crédito)
        return "Bull Put Spread" if bull else "Bear Call Spread"

    if ivp < 30:                                   # IV baja -> long direccional si hay momentum
        rsi_ok = (rsi is not None and rsi < 65) if bull else (rsi is not None and rsi > 35)
        if beta > 1.2 and trend_pct > 10 and rsi_ok:
            return "Long Call" if bull else "Long Put"
        return "Bull Call Spread" if bull else "Bear Put Spread"

    return "Bull Call Spread" if bull else "Bear Put Spread"   # IV media -> débito vertical


def show_strategy():
    from collections import Counter
    neutral_factor = get_param_float("neutral_size_factor", 0.5)
    mode           = get_param_str("dial_mode", "GATE")

    conn = _conn(); cur = conn.cursor()
    dossier = load_dossier(cur, "scan")
    regime  = load_regime(cur, "scan")
    cur.close(); conn.close()
    if not dossier:
        print("  no scan dossier. Run the scanner (--scan --commit) first.")
        return 1

    counts = Counter()
    examples = {}
    for tk, f in dossier.items():
        stock_dir = direction(f)
        passes, _ = dial(stock_dir, regime, neutral_factor, mode)
        if not passes:
            continue                               # solo las que pasan el dial tienen estrategia
        strat = select_strategy(f, stock_dir)
        counts[strat] += 1
        examples.setdefault(strat, []).append(tk)

    total = sum(counts.values())
    print(f"\n  STRATEGY (§5) — regime={regime} · {total} candidates that pass the dial")
    bearish = {"Bear Call Spread", "Bear Put Spread", "Long Put"}
    order = ["Bull Put Spread", "Bear Call Spread", "Long Call", "Long Put",
             "Bull Call Spread", "Bear Put Spread", None]
    for strat in order:
        if strat not in counts:
            continue
        tag = "bearish" if strat in bearish else ("fail-closed" if strat is None else "bullish")
        name = strat if strat else "None"
        ex = ", ".join(examples[strat][:5])
        print(f"     {name:<18} {counts[strat]:>3}  [{tag:<11}] {ex}")
    n_bear = sum(counts.get(s, 0) for s in bearish)
    n_bull = total - n_bear - counts.get(None, 0)
    print(f"\n  bidirectional balance: {n_bull} bullish · {n_bear} bearish · {counts.get(None,0)} fail-closed")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE DE SELECCIÓN (ensamblado: §2 -> §3 -> §4.1 -> §4.2 -> §5)
# ══════════════════════════════════════════════════════════════════════════════
# Recorre las 500 del dossier y marca el status de cada una según el PRIMER gate
# que la frena, en el orden del diseño. Persiste TODAS (extensión del scan) en
# selection_result: las 'candidate' son la entrega, el resto documenta por qué no.
# El LLM (§4.3) y los builders (§6) son caros y NO corren acá — actúan después,
# selectivamente, sobre las candidatas persistidas.

def _ensure_selection_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS selection_result (
            id             SERIAL PRIMARY KEY,
            study_id       INTEGER NOT NULL REFERENCES ticker_study(id),
            ticker         VARCHAR(12) NOT NULL,
            scan_at        TIMESTAMPTZ NOT NULL,
            direction      VARCHAR(10),
            operable       BOOLEAN,
            macro_blocked  BOOLEAN,
            dial_passes    BOOLEAN,
            size_factor    DOUBLE PRECISION,
            strategy       VARCHAR(30),
            status         VARCHAR(30) NOT NULL,
            llm_assessment VARCHAR(10),
            llm_conviction VARCHAR(10),
            llm_catalyst   TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (study_id)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_selection_status ON selection_result (status);")


def _scan_at(cur, slot="scan"):
    cur.execute("SELECT MAX(scan_at) FROM ticker_study WHERE slot = %s", (slot,))
    return cur.fetchone()[0]


def run_selection(dossier, market, regime):
    """
    Orquesta las piezas ya validadas. Devuelve la lista de resultados (un dict por
    acción con study_id, direction, strategy, size_factor, status). No toca la DB.
    """
    min_price     = get_param_float("op_min_price", 5.0)
    max_price     = get_param_float("op_max_price", 2000.0)
    min_avg_vol   = get_param_float("op_min_avg_volume", 300000.0)
    min_liquidity = get_param_int("op_min_liquidity_rating", 2)
    beta_th       = get_param_float("macro_gate_beta_threshold", 2.0)
    days_th       = get_param_int("macro_gate_days_threshold", 2)
    earnings_days = get_param_int("earnings_block_days", 21)
    neutral_f     = get_param_float("neutral_size_factor", 0.5)
    mode          = get_param_str("dial_mode", "GATE")
    macro_days    = market.get("macro_next_high_days")

    results = []
    for tk, f in dossier.items():
        r = {"study_id": f.get("_study_id"), "ticker": tk,
             "direction": None, "operable": None, "macro_blocked": None,
             "dial_passes": None, "size_factor": None, "strategy": None, "status": None}

        # §2 operabilidad
        op_ok, _ = operability(f, min_price, max_price, min_avg_vol, min_liquidity)
        r["operable"] = op_ok
        if not op_ok:
            r["status"] = "not_operable"; results.append(r); continue

        # §3a earnings inminente (evento de la acción)
        e_blocked, _ = earnings_gate(f, earnings_days)
        if e_blocked:
            r["status"] = "earnings_blocked"; results.append(r); continue

        # §3b gate macro × beta (evento global)
        blocked, _ = macro_beta_gate(f, macro_days, beta_th, days_th)
        r["macro_blocked"] = blocked
        if blocked:
            r["status"] = "macro_blocked"; results.append(r); continue

        # §4.1 dirección
        d = direction(f)
        r["direction"] = d if d else "NONE"
        if d not in ("UPTREND", "DOWNTREND"):
            r["status"] = "no_direction"; results.append(r); continue

        # §4.2 dial de régimen
        passes, size = dial(d, regime, neutral_f, mode)
        r["dial_passes"] = passes
        r["size_factor"] = size if passes else 0.0
        if not passes:
            r["status"] = "blocked_counter_trend"; results.append(r); continue

        # §5 estrategia
        r["strategy"] = select_strategy(f, d)
        r["status"]   = "candidate"
        results.append(r)

    return results


def _persist_selection(cur, results, scan_at):
    """
    Upsert por study_id (una selección por dossier de acción). Actualiza los campos
    deterministas; NO toca los llm_* -> el enriquecimiento del LLM sobrevive a un re-run.
    """
    from psycopg2.extras import execute_values
    data = [
        (r["study_id"], r["ticker"], scan_at, r["direction"], r["operable"],
         r["macro_blocked"], r["dial_passes"], r["size_factor"], r["strategy"], r["status"])
        for r in results
    ]
    execute_values(cur, """
        INSERT INTO selection_result
            (study_id, ticker, scan_at, direction, operable, macro_blocked,
             dial_passes, size_factor, strategy, status)
        VALUES %s
        ON CONFLICT (study_id) DO UPDATE SET
            scan_at = EXCLUDED.scan_at, direction = EXCLUDED.direction,
            operable = EXCLUDED.operable, macro_blocked = EXCLUDED.macro_blocked,
            dial_passes = EXCLUDED.dial_passes, size_factor = EXCLUDED.size_factor,
            strategy = EXCLUDED.strategy, status = EXCLUDED.status
    """, data, page_size=1000)


def show_selection(commit):
    from collections import Counter
    conn = _conn(); cur = conn.cursor()
    _ensure_selection_table(cur); conn.commit()
    dossier = load_dossier(cur, "scan")
    if not dossier:
        cur.close(); conn.close()
        print("  no scan dossier. Run the scanner (--scan --commit) first.")
        return 1
    market  = load_market(cur, "scan")
    regime  = load_regime(cur, "scan")
    scan_at = _scan_at(cur, "scan")

    results = run_selection(dossier, market, regime)

    status_order = ["candidate", "blocked_counter_trend", "no_direction",
                    "macro_blocked", "earnings_blocked", "not_operable"]
    counts = Counter(r["status"] for r in results)
    total  = len(results)
    print(f"\n  SELECTION PIPELINE — regime={regime} · {total} stocks")
    for st in status_order:
        if st in counts:
            print(f"     {st:<22} {counts[st]:>3}")

    # balance bidireccional de las candidatas (la entrega)
    cands = [r for r in results if r["status"] == "candidate"]
    strat = Counter(r["strategy"] for r in cands)
    bearish = {"Bear Call Spread", "Bear Put Spread", "Long Put"}
    n_bear = sum(v for s, v in strat.items() if s in bearish)
    n_bull = len(cands) - n_bear
    print(f"\n  candidates: {len(cands)}  ·  {n_bull} bullish · {n_bear} bearish")
    for s, v in strat.most_common():
        tag = "bearish" if s in bearish else "bullish"
        print(f"     {s:<18} {v:>3}  [{tag}]")

    if commit:
        _persist_selection(cur, results, scan_at)
        conn.commit()
        print(f"\n  ✅ persisted {total} rows in selection_result (scan_at {scan_at})")
    else:
        print(f"\n  DRY RUN — nothing written. With --commit it persists to selection_result.")
    cur.close(); conn.close()
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Selection layer v2")
    p.add_argument("--direction", action="store_true",
                   help="§4.1: deterministic direction split of the latest scan")
    p.add_argument("--dial", action="store_true",
                   help="§4.2: apply the regime dial (GATE mode) over the latest scan")
    p.add_argument("--validate", metavar="TICKER",
                   help="§4.3: LLM validates a ticker's direction against external evidence")
    p.add_argument("--perforate", metavar="TICKER",
                   help="§4.4: check whether a blocked counter-trend ticker perforates the Gate")
    p.add_argument("--strategy", action="store_true",
                   help="§5: strategy split (bidirectional mirror) over the dial-passing candidates")
    p.add_argument("--macro-gate", action="store_true", dest="macro_gate",
                   help="§3: macro×beta gate — blocks high-beta names with an imminent macro event")
    p.add_argument("--operability", action="store_true",
                   help="§2: operability filter (neutral) — price band, min volume, min liquidity")
    p.add_argument("--select", action="store_true",
                   help="run the full deterministic pipeline (§2->§5) over the dossier")
    p.add_argument("--commit", action="store_true", help="with --select: persist to selection_result")
    a = p.parse_args()

    print(f"\n{'═'*55}")
    print(f"  SELECTION v2")
    print(f"{'═'*55}")
    env_state = "loaded" if _ENV_LOADED else "NOT found — using system env vars"
    print(f"  env: {_ENV_PATH}  ({env_state})")

    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1

    if a.direction:
        return show_direction()
    if a.dial:
        return show_dial()
    if a.validate:
        return show_validation(a.validate.upper())
    if a.perforate:
        return show_perforation(a.perforate.upper())
    if a.strategy:
        return show_strategy()
    if a.macro_gate:
        return show_macro_gate()
    if a.operability:
        return show_operability()
    if a.select:
        return show_selection(a.commit)

    print("  nothing to do — try --direction")
    return 0


if __name__ == "__main__":
    sys.exit(main())