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


def main():
    p = argparse.ArgumentParser(description="Portfolio layer v2 (delta, sector, risk gates)")
    p.add_argument("--selftest", action="store_true", help="prueba la matemática del delta neto")
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

    print("  nothing to do — try --selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())