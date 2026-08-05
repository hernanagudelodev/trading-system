"""
system_state.py — tabla clave-valor para estado del sistema en runtime.

Hoy alberga el kill-flag de live ('live_kill'). Generica a proposito: futuros
flags (pausa por evento macro, etc.) reusan la misma tabla sin migrar esquema.

USO (desde scripts/, con el .env cargado):
    python system_state.py --init          # crea la tabla si no existe
    python system_state.py --status        # muestra el estado del kill-flag
    python system_state.py --kill           # FRENA live (setea live_kill='off')
    python system_state.py --resume         # LIBERA el freno (borra la fila)

El interruptor de live (executor.live_trading_allowed) LEE esta tabla. Este script
solo la administra. Recorda: apagar live tambien exige que LIVE_TRADING_ENABLED
sea 'true' para que el kill-flag tenga algo que frenar — si la env ya esta en
'false', live ya esta apagado por la capa 1.
"""
import os
import argparse
from dotenv import load_dotenv
load_dotenv()
import psycopg2

KILL_KEY = "live_kill"


def _conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def init_table():
    conn = _conn(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS system_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.commit(); cur.close(); conn.close()
    print("  system_state lista (creada o ya existia).")


def status():
    conn = _conn(); cur = conn.cursor()
    cur.execute("SELECT value, updated_at FROM system_state WHERE key = %s", (KILL_KEY,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row is None:
        print(f"  kill-flag '{KILL_KEY}': AUSENTE -> live NO esta frenado por capa 2.")
    else:
        estado = "FRENADO (off)" if str(row[0]).strip().lower() == "off" else f"value={row[0]}"
        print(f"  kill-flag '{KILL_KEY}': {estado} · actualizado {row[1]}")


def set_kill():
    conn = _conn(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO system_state (key, value, updated_at)
        VALUES (%s, 'off', NOW())
        ON CONFLICT (key) DO UPDATE SET value = 'off', updated_at = NOW()
    """, (KILL_KEY,))
    conn.commit(); cur.close(); conn.close()
    print(f"  🛑 kill-flag '{KILL_KEY}' = off. Live FRENADO (capa 2).")


def resume():
    conn = _conn(); cur = conn.cursor()
    cur.execute("DELETE FROM system_state WHERE key = %s", (KILL_KEY,))
    n = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    print(f"  ▶️  freno liberado (fila borrada: {n}). Capa 2 ya no frena. "
          f"Live opera si LIVE_TRADING_ENABLED='true'.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Administra system_state (kill-flag de live)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--init",   action="store_true", help="crea la tabla")
    g.add_argument("--status", action="store_true", help="muestra el kill-flag")
    g.add_argument("--kill",   action="store_true", help="FRENA live (value=off)")
    g.add_argument("--resume", action="store_true", help="libera el freno")
    a = p.parse_args()
    if a.init:   init_table()
    elif a.status: status()
    elif a.kill:   set_kill()
    elif a.resume: resume()