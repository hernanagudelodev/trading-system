"""
scripts/auto_run.py  (v2-bidi)
==============================
ORQUESTADOR — el espejo del auto_run de def, con el flujo determinista de v2.
Encadena las capas en un solo comando y manda el resumen por Telegram.

Cuatro pasos (cada uno un subprocess; se comunican por la DB, no por memoria):
    [1/4] Scanner  (Estudio)    — scanner.py --scan --commit
    [2/4] Selection             — selection.py --select --commit
    [3/4] Opener (autónomo)     — opener.py --run --commit --max N
    [4/4] Resumen + Telegram    — lee la DB (lo que el opener dejó) y notifica

No hay LLM monolítico (como el run_claude_analysis de def): el scanner y la
Selección determinista producen las candidatas; el opener las abre con gates.
Si un paso falla (exit != 0), se aborta — no se sigue sobre datos a medias.

USO (PowerShell, venv trading_env, carpeta scripts/)
    python auto_run.py                     # ciclo completo, abre hasta 10, notifica
    python auto_run.py --max 5             # limita cuántas abre el opener
    python auto_run.py --skip-scan         # usa el último scan (no re-escanea) — pruebas
    python auto_run.py --no-telegram       # no manda el resumen a Telegram — pruebas
"""
import os
import sys
import time
import argparse
import subprocess
from datetime import datetime, timezone
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


def run_step(n, total, name, cmd):
    """Corre un paso como subprocess (hereda stdout, imprime en vivo). True si exit 0."""
    print(f"\n{'═'*60}\n  [{n}/{total}] {name}\n{'═'*60}")
    result = subprocess.run([sys.executable] + cmd, cwd=str(_THIS_DIR))
    if result.returncode != 0:
        print(f"\n  ⛔ paso {n} falló (exit {result.returncode}) — se aborta el run.")
        return False
    return True


def build_summary(run_started_at):
    """
    Arma el resumen del run leyendo la DB: régimen, candidatas, lo que abrió el
    opener en ESTE run (opened_at >= inicio), y el delta neto del libro paper.
    """
    import portfolio
    lines = []
    conn = _conn(); cur = conn.cursor()

    # régimen + candidatas del último scan
    cur.execute("SELECT regime FROM ticker_study WHERE slot='scan' ORDER BY scan_at DESC LIMIT 1")
    row = cur.fetchone()
    regime = row[0] if row else "?"

    cur.execute("""
        WITH latest AS (SELECT MAX(scan_at) AS m FROM selection_result)
        SELECT COUNT(*) FILTER (WHERE status='candidate')
        FROM selection_result WHERE scan_at=(SELECT m FROM latest)
    """)
    n_cand = cur.fetchone()[0]

    # abiertas en ESTE run (paper)
    cur.execute("""
        SELECT ticker, strategy FROM paper_positions
        WHERE status='OPEN' AND opened_at >= %s ORDER BY opened_at
    """, (run_started_at,))
    opened = cur.fetchall()

    # tamaño total del libro paper
    cur.execute("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'")
    n_book = cur.fetchone()[0]
    cur.close(); conn.close()

    lines.append(f"<b>auto_run v2</b> — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"régimen: {regime} · candidatas: {n_cand}")
    if opened:
        lines.append(f"abiertas este run: {len(opened)}")
        for tk, strat in opened:
            lines.append(f"  • {tk} {strat}")
    else:
        lines.append("abiertas este run: 0")

    try:
        net, _, _ = portfolio.build_book("paper")
        side = "net long" if net > 0 else "net short" if net < 0 else "neutral"
        lines.append(f"libro paper: {n_book} posiciones · delta neto {net:+.1f} ({side})")
    except Exception:
        lines.append(f"libro paper: {n_book} posiciones")

    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="auto_run v2 — orquestador del ciclo completo")
    p.add_argument("--max", type=int, default=10, help="máx. posiciones que abre el opener")
    p.add_argument("--skip-scan", action="store_true", dest="skip_scan",
                   help="usar el último scan (no re-escanea) — pruebas")
    p.add_argument("--skip-selection", action="store_true", dest="skip_selection",
                   help="usar la última selección (no re-selecciona) — pruebas")
    p.add_argument("--no-telegram", action="store_true", dest="no_telegram",
                   help="no manda el resumen por Telegram — pruebas")
    a = p.parse_args()

    if not os.getenv("DATABASE_URL"):
        print(f"  missing DATABASE_URL (check {_ENV_PATH})")
        return 1

    t0  = time.time()
    started_at = datetime.now(timezone.utc)
    total = 4
    print(f"\n  AUTO_RUN v2 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # [1/4] Scanner
    if a.skip_scan:
        print(f"\n  [1/{total}] Scanner — SKIPPED (usando el último scan)")
    elif not run_step(1, total, "Scanner (Estudio)", ["scanner.py", "--scan", "--commit"]):
        return 1

    # [2/4] Selection
    if a.skip_selection:
        print(f"\n  [2/{total}] Selection — SKIPPED (usando la última selección)")
    elif not run_step(2, total, "Selection", ["selection.py", "--select", "--commit"]):
        return 1

    # [3/4] Opener
    if not run_step(3, total, "Opener (autónomo)",
                    ["opener.py", "--run", "--commit", "--max", str(a.max)]):
        return 1

    # [4/4] Resumen + Telegram
    print(f"\n{'═'*60}\n  [4/{total}] Resumen + Telegram\n{'═'*60}")
    summary = build_summary(started_at)
    print("\n" + summary.replace("<b>", "").replace("</b>", ""))
    print(f"\n  tiempo total: {time.time()-t0:.0f}s")

    if not a.no_telegram:
        try:
            from notify import send_push
            send_push(title="auto_run v2", message=summary)
            print("  ✅ resumen enviado a Telegram")
        except Exception as e:
            print(f"  ⚠️  no se pudo enviar a Telegram: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())