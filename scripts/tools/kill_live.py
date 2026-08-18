"""
kill_live.py — freno en caliente de las APERTURAS live (kill-flag en DB).

Escribe system_state[key='live_kill']:
    off  -> FRENA las aperturas live (LiveExecutor.open_position devuelve False).
    on   -> REACTIVA (quita el freno; la capa 1 / env vuelve a mandar).

El monitor/executor lee este flag FRESCO en cada apertura via live_trading_allowed().
Esto NO frena el auto-cierre (eso es MONITOR_AUTO_CLOSE / otro flag) — solo aperturas.

USO:
    python kill_live.py off    # frena aperturas
    python kill_live.py on     # reactiva
    python kill_live.py        # muestra el estado actual

Pensado para correrse por el bot de Telegram (/pause -> off, /resume -> on)
o a mano desde PowerShell. Idempotente (UPSERT).
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()
import psycopg2

KILL_KEY = "live_kill"

def estado_actual(cur):
    cur.execute("SELECT value, updated_at FROM system_state WHERE key=%s", (KILL_KEY,))
    row = cur.fetchone()
    if row is None:
        return None, None
    return str(row[0]).strip().lower(), row[1]

def main():
    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else None

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    # Sin argumento: mostrar estado actual y salir.
    if arg is None:
        val, ts = estado_actual(cur)
        if val is None:
            print("APERTURAS LIVE: habilitadas (no hay kill-flag)")
        elif val == "off":
            print(f"APERTURAS LIVE: FRENADAS (live_kill=off desde {ts})")
        else:
            print(f"APERTURAS LIVE: habilitadas (live_kill={val} desde {ts})")
        cur.close(); conn.close()
        return 0

    if arg not in ("on", "off"):
        print(f"ERROR: argumento invalido {arg!r}. Uso: kill_live.py [on|off]")
        cur.close(); conn.close()
        return 1

    # UPSERT del flag (crea la fila si no existe, actualiza si existe).
    cur.execute("""
        INSERT INTO system_state (key, value, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
    """, (KILL_KEY, arg))
    conn.commit()

    if arg == "off":
        print("🔴 APERTURAS LIVE FRENADAS")
        print("   El sistema NO abrira posiciones nuevas en live.")
        print("   (El auto-cierre sigue activo — las posiciones abiertas se")
        print("    gestionan normal.) Reactiva con: /resume")
    else:
        print("🟢 APERTURAS LIVE REACTIVADAS")
        print("   El sistema vuelve a abrir posiciones en los proximos slots.")

    cur.close(); conn.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())