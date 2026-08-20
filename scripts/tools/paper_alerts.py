"""
paper_alerts.py — prende/apaga las alertas de NIVEL de las posiciones paper
(WATCH / ACTION / URGENT / take-profit). Flag en system_state['paper_alerts'].

    on   -> las alertas de paper SE ENVIAN (comportamiento normal).
    off  -> las alertas de paper NO se envian (silencio). Las de LIVE nunca se
            tocan — siguen llegando siempre.

Solo afecta send_alert_notification(mode='paper') en monitor.py. Aperturas,
cierres y resumenes de paper van por otra via y no se ven afectados.

USO:
    python paper_alerts.py off    # silencia alertas de paper
    python paper_alerts.py on     # reactiva
    python paper_alerts.py        # muestra el estado actual

Para el bot (/paper_alerts on|off) o a mano. Idempotente (UPSERT).
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()
import psycopg2

FLAG_KEY = "paper_alerts"

def main():
    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else None
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    if arg is None:
        cur.execute("SELECT value, updated_at FROM system_state WHERE key=%s", (FLAG_KEY,))
        row = cur.fetchone()
        if row is None or str(row[0]).strip().lower() != "off":
            estado = row[0] if row else "on (default, sin fila)"
            print(f"ALERTAS PAPER: activas ({estado})")
        else:
            print(f"ALERTAS PAPER: SILENCIADAS (off desde {row[1]})")
        cur.close(); conn.close()
        return 0

    if arg not in ("on", "off"):
        print(f"ERROR: argumento invalido {arg!r}. Uso: paper_alerts.py [on|off]")
        cur.close(); conn.close()
        return 1

    cur.execute("""
        INSERT INTO system_state (key, value, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
    """, (FLAG_KEY, arg))
    conn.commit()

    if arg == "off":
        print("🔇 ALERTAS PAPER SILENCIADAS")
        print("   Las alertas de nivel (WATCH/ACTION/URGENT/take-profit) de paper")
        print("   NO se enviaran. Las de LIVE siguen llegando normal.")
        print("   Reactiva con: /paper_alerts on")
    else:
        print("🔔 ALERTAS PAPER REACTIVADAS")
        print("   Las alertas de paper vuelven a enviarse.")

    cur.close(); conn.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())