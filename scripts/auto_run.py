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


def build_summary(run_started_at, slot="manual", run_time=0, book="paper"):
    """
    Arma el resumen del run leyendo la DB. Texto PLANO (send_push escapa HTML, así
    que nada de <b>): mercado, selección con reparto, aperturas de este run, y el
    estado del libro (P&L abierto, delta neto, balance bidireccional).
    """
    import portfolio
    table = portfolio._BOOK_TABLE.get(book, "paper_positions")
    conn = _conn(); cur = conn.cursor()

    # Mercado: régimen + VIX del último scan
    cur.execute("SELECT regime FROM ticker_study WHERE slot='scan' ORDER BY scan_at DESC LIMIT 1")
    row = cur.fetchone(); regime = row[0] if row else "?"
    cur.execute("""
        SELECT f.criterion, f.value_num, f.value_text
        FROM ticker_study s JOIN study_fact f ON f.study_id = s.id
        WHERE s.ticker = '__MARKET__'
          AND s.scan_at = (SELECT MAX(scan_at) FROM ticker_study WHERE slot='scan')
          AND f.criterion IN ('vix_current', 'vix_level')
    """)
    mkt = {c: (n if n is not None else t) for c, n, t in cur.fetchall()}
    vix = mkt.get("vix_current"); vix_level = mkt.get("vix_level")

    # Selección: reparto de candidatas por dirección
    cur.execute("""
        WITH latest AS (SELECT MAX(scan_at) AS m FROM selection_result)
        SELECT direction, COUNT(*) FROM selection_result
        WHERE status='candidate' AND scan_at=(SELECT m FROM latest)
        GROUP BY direction
    """)
    dc = dict(cur.fetchall())
    n_up = dc.get("UPTREND", 0); n_down = dc.get("DOWNTREND", 0)
    n_cand = n_up + n_down

    # Aperturas de ESTE run
    cur.execute("""
        SELECT ticker, strategy FROM {t}
        WHERE status='OPEN' AND opened_at >= %s ORDER BY opened_at
    """.format(t=table), (run_started_at,))
    opened = cur.fetchall()

    # Libro: posiciones, P&L abierto, balance bidireccional
    cur.execute(f"SELECT strategy, gross_pnl FROM {table} WHERE status='OPEN'")
    book_rows = cur.fetchall()
    n_book    = len(book_rows)
    pnl_total = sum(float(g or 0) for _, g in book_rows)
    bearish   = {"Bear Call Spread", "Bear Put Spread", "Long Put"}
    n_bear    = sum(1 for s, _ in book_rows if s in bearish)
    n_bull    = n_book - n_bear
    cur.close(); conn.close()

    net = None
    try:
        net, _, _ = portfolio.build_book(book)
    except Exception:
        pass

    vix_str  = f"{vix:.1f}" if vix is not None else "?"
    side     = "net long" if (net or 0) > 0 else "net short" if (net or 0) < 0 else "neutral"
    net_str  = f"{net:+.0f} ({side})" if net is not None else "?"

    lines = [
        f"📊 AUTO_RUN · {slot}",
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')} · {run_time:.0f}s",
        "",
        f"📈 Mercado: {regime} · VIX {vix_str} ({vix_level or '?'})",
        f"🎯 Candidatas: {n_cand}  ({n_up} alcistas / {n_down} bajistas)",
        "",
        f"🔓 Abiertas este run: {len(opened)}",
    ]
    for tk, strat in opened:
        lines.append(f"   • {tk} · {strat}")
    lines += [
        "",
        f"📁 Libro {book}: {n_book} posiciones ({n_bull} alcistas / {n_bear} bajistas)",
        f"   P&L abierto: {pnl_total:+.0f}",
        f"   Delta neto: {net_str}",
    ]

    data = {"regime": regime, "n_cand": n_cand, "opened": opened,
            "n_book": n_book, "net_delta": net}
    return "\n".join(lines), data


def _save_run_log(slot, data, run_time, book="paper"):
    """Escribe el run en auto_run_logs (dedup del wrapper + historial para el dashboard)."""
    try:
        conn = _conn(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_run_logs (
                id           SERIAL PRIMARY KEY,
                run_at       TIMESTAMP DEFAULT NOW(),
                slot         VARCHAR(20),
                regime       VARCHAR(10),
                candidates   INTEGER,
                opened       INTEGER DEFAULT 0,
                errors       INTEGER DEFAULT 0,
                net_delta    DECIMAL(10,2),
                summary      TEXT,
                run_time_sec INTEGER,
                mode         VARCHAR(10) NOT NULL DEFAULT 'paper'
            )
        """)
        cur.execute("""
            INSERT INTO auto_run_logs
                (slot, regime, candidates, opened, net_delta, summary, run_time_sec, mode)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            slot, data["regime"], data["n_cand"], len(data["opened"]),
            data["net_delta"],
            "; ".join(f"{tk} {st}" for tk, st in data["opened"]),
            int(run_time), book,
        ))
        log_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        print(f"  log guardado (auto_run_logs id={log_id})")
    except Exception as e:
        print(f"  ⚠️  no se pudo guardar el log: {e}")


def main():
    p = argparse.ArgumentParser(description="auto_run v2 — orquestador del ciclo completo")
    p.add_argument("--max", type=int, default=10, help="máx. posiciones que abre el opener")
    p.add_argument("--skip-scan", action="store_true", dest="skip_scan",
                   help="usar el último scan (no re-escanea) — pruebas")
    p.add_argument("--skip-selection", action="store_true", dest="skip_selection",
                   help="usar la última selección (no re-selecciona) — pruebas")
    p.add_argument("--no-telegram", action="store_true", dest="no_telegram",
                   help="no manda el resumen por Telegram — pruebas")
    p.add_argument("--slot", default="manual",
                   help="nombre del slot (morning/midday/...) — lo pasa el wrapper para el log")
    p.add_argument("--live", action="store_true",
                   help="abre en LIVE (el opener usa LiveExecutor; el interruptor "
                        "LIVE_TRADING_ENABLED decide si llega al broker). Default: paper")
    a = p.parse_args()
    book = "live" if a.live else "paper"

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
    opener_cmd = ["opener.py", "--run", "--commit", "--max", str(a.max)]
    if a.live:
        opener_cmd.append("--live")
    if not run_step(3, total, "Opener (autónomo)", opener_cmd):
        return 1

    # [4/4] Resumen + Telegram
    print(f"\n{'═'*60}\n  [4/{total}] Resumen + Telegram\n{'═'*60}")
    run_time = time.time() - t0
    summary, data = build_summary(started_at, a.slot, run_time, book)
    print("\n" + summary)
    print(f"\n  tiempo total: {run_time:.0f}s")

    _save_run_log(a.slot, data, run_time, book)

    if not a.no_telegram:
        try:
            from notify import send_push
            send_push(title=f"AUTO_RUN · {a.slot}", message=summary)
            print("  ✅ resumen enviado a Telegram")
        except Exception as e:
            print(f"  ⚠️  no se pudo enviar a Telegram: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())