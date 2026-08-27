"""
worker_api.py — API interna del worker (red privada de Railway).

Expone la lógica del sistema (que vive en este repo: trade.py, etc.) para que el
dashboard-backend la consuma SIN duplicar codigo. Corre en un thread daemon
separado del loop del monitor: si la API se cae, el monitor (que cierra
posiciones) sigue intacto.

IMPORTANTE — seguridad:
  - Pensado para escuchar SOLO en la red privada de Railway (sin dominio publico).
    El dashboard-backend le pega por http://<worker>.railway.internal:PORT.
  - Hoy solo expone LECTURA (TWR). Cuando se agreguen ACCIONES (cerrar, pausar,
    auto_run), CADA endpoint de accion necesita su capa de auth — no basta la red
    privada, porque el front (expuesto) es quien las dispara.
  - Un header de servicio (X-Internal-Token) da una barrera minima aun en lectura.
"""
import os
import threading
from datetime import datetime, timedelta

from fastapi import FastAPI, Header, HTTPException
import uvicorn

_INTERNAL_TOKEN = os.getenv("WORKER_API_TOKEN")   # compartido worker <-> dashboard-backend
_PORT = int(os.getenv("WORKER_API_PORT", "8080"))

api = FastAPI(title="worker-api", docs_url=None, redoc_url=None, openapi_url=None)


def _check(tok):
    # Si hay token configurado, exigirlo. Si no hay (transicion), no bloquea.
    if _INTERNAL_TOKEN and tok != _INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")


@api.get("/health")
def health():
    return {"ok": True, "service": "worker-api"}


@api.get("/twr")
def twr(days: int = 90, from_date: str = None, to_date: str = None,
        x_internal_token: str = Header(default=None)):
    """
    Rendimiento Time-Weighted (ajustado por depositos/retiros) del periodo.
    Reusa compute_twr de trade.py — fuente unica, sin duplicar la formula.
    Devuelve la serie de rendimiento acumulado (%) + resumen.
    """
    _check(x_internal_token)
    import trade

    # Rango de fechas (mismo criterio que el dashboard)
    if from_date or to_date:
        start = from_date
        end   = to_date
    else:
        start = (datetime.now() - timedelta(days=int(days))).date().isoformat()
        end   = datetime.now().date().isoformat()

    # Serie de NLV
    conn = trade.get_db_connection()
    cur  = conn.cursor()
    if start and end:
        cur.execute("""SELECT snapshot_at, net_liquidating_value FROM account_snapshots
                       WHERE snapshot_at >= %s AND snapshot_at < (%s::date + INTERVAL '1 day')
                       ORDER BY snapshot_at ASC""", (start, end))
    elif start:
        cur.execute("""SELECT snapshot_at, net_liquidating_value FROM account_snapshots
                       WHERE snapshot_at >= %s ORDER BY snapshot_at ASC""", (start,))
    else:
        cur.execute("""SELECT snapshot_at, net_liquidating_value FROM account_snapshots
                       ORDER BY snapshot_at ASC""")
    rows = cur.fetchall()
    cur.close(); conn.close()

    if not rows or len(rows) < 2:
        return {"twr_pct": 0.0, "series": [], "net_flows": 0.0, "raw_change": 0.0}

    series = [{"t": r[0].isoformat(), "nlv": float(r[1])} for r in rows]

    # Flujos del periodo
    d_start = rows[0][0].date()
    d_end   = rows[-1][0].date()
    flows = trade.get_cash_movements_between(d_start, d_end)

    result = trade.compute_twr(series, flows)
    return {
        "twr_pct":    result["twr_pct"],
        "series":     result["twr_series"],
        "net_flows":  result["net_flows"],
        "raw_change": result["raw_change"],
    }


def start_api_thread():
    """Arranca uvicorn en un thread daemon — no bloquea al que llama.
    (Se conserva por si se quisiera embeber; el uso normal es como servicio
    propio via __main__.)"""
    def _run():
        uvicorn.run(api, host="0.0.0.0", port=_PORT, log_level="warning")
    t = threading.Thread(target=_run, daemon=True, name="worker-api")
    t.start()
    print(f"  worker-api escuchando en :{_PORT} (red privada)")
    return t


if __name__ == "__main__":
    # Entry point del SERVICIO worker-api (Railway: python scripts/worker_api.py).
    # Proceso propio, aislado del monitor. Comparte el repo y la DB, no la memoria.
    print(f"\n{'═' * 55}")
    print(f"  WORKER-API — servicio interno (red privada Railway)")
    print(f"  Puerto: {_PORT}")
    print(f"{'═' * 55}\n")
    uvicorn.run(api, host="0.0.0.0", port=_PORT, log_level="info")