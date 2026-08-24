"""
set_risk_pct.py — fuente unica del tope de riesgo de cartera (system_state).

El tope vive en system_state['max_portfolio_risk_pct']. TODOS los servicios
(worker, bot, dashboard) lo leen de ahi via portfolio_risk_pct() -> una sola
fuente, nunca mas env vars desincronizadas entre servicios.

USO:
    python set_risk_pct.py           # muestra el tope actual
    python set_risk_pct.py 60        # setea el tope a 60%
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()
import psycopg2

KEY = "max_portfolio_risk_pct"

def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    if len(sys.argv) < 2:
        cur.execute("SELECT value, updated_at FROM system_state WHERE key=%s", (KEY,))
        row = cur.fetchone()
        if row is None:
            print(f"Tope en DB: NO seteado (system_state['{KEY}'] no existe)")
            print(f"  -> portfolio_risk_pct() caeria al fallback de env var")
        else:
            print(f"Tope en DB: {row[0]}%  (actualizado {row[1]})")
        cur.close(); conn.close()
        return 0

    try:
        val = float(sys.argv[1])
    except ValueError:
        print(f"ERROR: '{sys.argv[1]}' no es un numero. Uso: set_risk_pct.py 60")
        cur.close(); conn.close()
        return 1

    if not (0 < val <= 100):
        print(f"ERROR: {val} fuera de rango (0-100).")
        cur.close(); conn.close()
        return 1

    cur.execute("""
        INSERT INTO system_state (key, value, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
    """, (KEY, str(val)))
    conn.commit()
    print(f"✅ Tope de riesgo seteado a {val:.0f}% en system_state.")
    print(f"   TODOS los servicios (worker/bot/dashboard) leeran este valor.")
    print(f"   Podes borrar la env var MAX_PORTFOLIO_RISK_PCT de los servicios.")
    cur.close(); conn.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())