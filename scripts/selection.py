"""
scripts/selection.py  (v2-bidi)
===============================
CAPA DE SELECCIÓN — toma el dossier del Estudio (ticker_study + study_fact) y
decide, de forma casi 100% determinista, qué opciones sirven (bidireccionales).
Un único LLM acotado validará la dirección (paso §4.3); todo lo demás es determinista.

Lee el dossier DESDE LA DB (no lo recibe en memoria): el scanner mide y persiste,
Selección consume. Así se valida que lo persistido alcanza para decidir, y las dos
capas quedan desacopladas (pasan datos por la DB).

ORDEN DE DECISIONES (CAPA_DE_SELECCION.md)
    1. Operabilidad  (¿se puede tradear?)        [TODO]
    2. Pertinencia   (¿vale la pena?)            [TODO]
    3. Dirección     (¿hacia qué lado?)          <- EN CURSO (§4.1 determinista)
    4. Estrategia    (¿qué estructura?)          [TODO]
    5. Builders      (¿qué piernas?)             [TODO]

§4.1 — DIRECCIÓN PROPIA (determinista, regla de CONFLUENCIA)
    UPTREND    : precio > SMA50 y precio > SMA200 y trend_25d positivo
    DOWNTREND  : precio < SMA50 y precio < SMA200 y trend_25d negativo
    LATERAL    : cualquier mezcla (señales en conflicto)
    None       : falta algún dato -> fail-closed (no se opera; sin default alcista)
    Los booleanos above_sma50/above_sma200 YA son "precio > SMA"; trend_25d positivo
    = pct_change > 0. Todo sale de study_fact.

USO (PowerShell, venv trading_env)
    python selection.py --direccion         # reparto de dirección del último scan

VARIABLES DE ENTORNO (en .env.v2): DATABASE_URL
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
_ENV_LOADED = load_dotenv(_ENV_PATH)

MARKET_TICKER = "__MARKET__"

from system_state import get_param_float, get_param_str  # noqa: E402  (path ya seteado arriba)


def _conn():
    import psycopg2
    return psycopg2.connect(os.getenv("DATABASE_URL"))


# ══════════════════════════════════════════════════════════════════════════════
# LECTURA DEL DOSSIER (pivot del formato largo study_fact)
# ══════════════════════════════════════════════════════════════════════════════

def _valor(num, bl, txt, isnull):
    """Reconstruye el valor de un hecho desde las columnas tipadas de study_fact."""
    if isnull:
        return None
    if bl is not None:
        return bl
    if num is not None:
        return num
    return txt


def cargar_dossier(cur, slot="scan"):
    """
    {ticker: {criterion: valor}} del ÚLTIMO scan (max scan_at para ese slot),
    excluyendo la fila de mercado. Pivotea el formato largo a un dict por acción.
    """
    cur.execute("""
        WITH ult AS (SELECT MAX(scan_at) AS m FROM ticker_study WHERE slot = %s)
        SELECT s.ticker, f.criterion, f.value_num, f.value_bool, f.value_text, f.is_null
        FROM ticker_study s
        JOIN study_fact f ON f.study_id = s.id
        WHERE s.slot = %s
          AND s.scan_at = (SELECT m FROM ult)
          AND s.ticker <> %s
    """, (slot, slot, MARKET_TICKER))

    dossier = {}
    for ticker, crit, num, bl, txt, isnull in cur.fetchall():
        dossier.setdefault(ticker, {})[crit] = _valor(num, bl, txt, isnull)
    return dossier


def cargar_regimen(cur, slot="scan"):
    """Régimen del último scan (está en la cabecera ticker_study, igual en todas las filas)."""
    cur.execute("""
        SELECT regime FROM ticker_study
        WHERE slot = %s AND scan_at = (SELECT MAX(scan_at) FROM ticker_study WHERE slot = %s)
        LIMIT 1
    """, (slot, slot))
    row = cur.fetchone()
    return row[0] if row else None


# ══════════════════════════════════════════════════════════════════════════════
# §4.1 — DIRECCIÓN DETERMINISTA POR CONFLUENCIA
# ══════════════════════════════════════════════════════════════════════════════

def direccion(f):
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


def mostrar_direccion():
    conn = _conn(); cur = conn.cursor()
    dossier = cargar_dossier(cur, "scan")
    cur.close(); conn.close()
    if not dossier:
        print("  ⛔ no hay dossier de scan en la DB. Corré el scanner (--scan --commit) primero.")
        return 1

    reparto = {"UPTREND": [], "DOWNTREND": [], "LATERAL": [], None: []}
    for tk, f in dossier.items():
        reparto[direccion(f)].append(tk)

    total = len(dossier)
    print(f"\n  DIRECCIÓN (§4.1) sobre el último scan — {total} acciones:")
    for etq in ("UPTREND", "DOWNTREND", "LATERAL", None):
        tks = reparto[etq]
        nombre = etq if etq else "None (fail-closed)"
        print(f"     {nombre:<20} {len(tks):>3}   ({len(tks)*100//total if total else 0}%)")

    # muestra de DOWNTREND — lo nuevo/bidireccional — para chequeo de cordura
    if reparto["DOWNTREND"]:
        print(f"\n  ejemplos DOWNTREND: {', '.join(reparto['DOWNTREND'][:12])}")
    if reparto[None]:
        print(f"  ejemplos None: {', '.join(reparto[None][:12])}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# §4.2 — DIAL DE RÉGIMEN (modo GATE · determinista)
# ══════════════════════════════════════════════════════════════════════════════

def dial(direc, regime, neutral_factor, mode="GATE"):
    """
    Cruza la dirección propia (§4.1) con el régimen del scan. Devuelve
    (pasa: bool, size_factor: float).

    GATE:
      - régimen a favor de la dirección  -> (True, 1.0)
      - NEUTRAL con UPTREND o DOWNTREND  -> (True, neutral_factor)  [sizing reducido]
      - contra-tendencia                 -> (False, 0.0)  [bloqueado; perfora el LLM §4.4]
      - LATERAL / None (ya filtrados §4.1)-> (False, 0.0)

    Solo GATE está implementado. Otro modo -> falla explícito (no comportamiento raro).
    """
    if mode != "GATE":
        raise NotImplementedError(
            f"dial_mode={mode!r} no implementado — solo GATE por ahora "
            f"(MODULA/INFORMACIONAL se activan con evidencia futura)")

    if direc not in ("UPTREND", "DOWNTREND"):
        return (False, 0.0)                      # LATERAL / None
    if regime == "BULLISH":
        return (True, 1.0) if direc == "UPTREND" else (False, 0.0)
    if regime == "BEARISH":
        return (True, 1.0) if direc == "DOWNTREND" else (False, 0.0)
    if regime == "NEUTRAL":
        return (True, neutral_factor)            # ambas, sizing reducido
    return (False, 0.0)                           # régimen desconocido -> fail-closed


def mostrar_dial():
    mode           = get_param_str("dial_mode", "GATE")
    neutral_factor = get_param_float("neutral_size_factor", 0.5)

    conn = _conn(); cur = conn.cursor()
    dossier = cargar_dossier(cur, "scan")
    regime  = cargar_regimen(cur, "scan")
    cur.close(); conn.close()
    if not dossier:
        print("  ⛔ no hay dossier de scan. Corré el scanner (--scan --commit) primero.")
        return 1

    print(f"\n  DIAL (§4.2) — régimen={regime} · modo={mode} · neutral_size_factor={neutral_factor}")

    pleno, reducido, bloqueada, fuera = [], [], [], []
    try:
        for tk, f in dossier.items():
            direc = direccion(f)
            pasa, factor = dial(direc, regime, neutral_factor, mode)
            if not pasa:
                (bloqueada if direc in ("UPTREND", "DOWNTREND") else fuera).append(tk)
            elif factor >= 1.0:
                pleno.append(tk)
            else:
                reducido.append(tk)
    except NotImplementedError as e:
        print(f"  ⛔ {e}")
        return 1

    total = len(dossier)
    print(f"\n  de {total} acciones:")
    print(f"     pasan a favor del régimen (size 1.0)   {len(pleno):>3}")
    print(f"     pasan en NEUTRAL (size {neutral_factor})           {len(reducido):>3}")
    print(f"     bloqueadas contra-tendencia (GATE)     {len(bloqueada):>3}")
    print(f"     fuera por dirección (LATERAL/None)     {len(fuera):>3}")
    if bloqueada:
        print(f"\n  ejemplos bloqueadas contra-tendencia: {', '.join(bloqueada[:12])}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="Capa de Selección v2")
    p.add_argument("--direccion", action="store_true",
                   help="§4.1: reparto de dirección determinista del último scan")
    p.add_argument("--dial", action="store_true",
                   help="§4.2: aplica el dial de régimen (modo GATE) sobre el último scan")
    a = p.parse_args()

    print(f"\n{'═'*55}")
    print(f"  SELECCIÓN v2")
    print(f"{'═'*55}")
    estado_env = "cargado" if _ENV_LOADED else "NO encontrado — usando variables del sistema"
    print(f"  env: {_ENV_PATH}  ({estado_env})")

    if not os.getenv("DATABASE_URL"):
        print(f"  ⛔ falta DATABASE_URL (revisá {_ENV_PATH})")
        return 1

    if a.direccion:
        return mostrar_direccion()

    if a.dial:
        return mostrar_dial()

    print("  nada que hacer — probá --direccion")
    return 0


if __name__ == "__main__":
    sys.exit(main())