"""
mark_prerules.py
================
Marca los 5 paper trades abiertos hoy SIN los gates de riesgo
(versión vieja de Railway, día de FOMC) como lote inválido.

NO los borra: hace un UPDATE reversible.
  - status       -> 'CLOSED'  (desaparecen de --paper-list)
  - close_reason -> 'PRE_RULES'  (excluibles de la validación)
  - closed_at    -> NOW()
  - gross_pnl/pnl_pct -> 0  (no inventan P&L; quedan neutros)

NO toca DAL (id 18): ese cumple las reglas nuevas y sigue vivo.
NO toca trade_context (sin filas huérfanas).

Excluir de validación luego con:
    WHERE close_reason != 'PRE_RULES'

Uso:
    python mark_prerules.py            # dry-run: muestra qué tocaría
    python mark_prerules.py --commit   # aplica
"""
import os
import sys
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# IDs de los 5 que violan el tope de riesgo (3% = ~$423).
# DAL (18) queda FUERA a propósito: cabe en las reglas nuevas.
TARGET_IDS = [16, 19, 20, 21, 22]   # IBM, PANW, NTAP, JBHT, APH

COMMIT = "--commit" in sys.argv

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

# Mostrar exactamente lo que se va a tocar — verificar antes de escribir
cur.execute("""
    SELECT id, ticker, strategy, strike_low, strike_high, status, close_reason
    FROM paper_positions
    WHERE id = ANY(%s)
    ORDER BY id
""", (TARGET_IDS,))
rows = cur.fetchall()
cols = [d[0] for d in cur.description]

print(f"\n  Se marcarán como PRE_RULES ({len(rows)} registros):")
print(f"  {'id':<4} {'ticker':<7} {'spread':<18} {'status':<8} {'close_reason'}")
for r in rows:
    d = dict(zip(cols, r))
    spread = f"${d['strike_low']:.0f}/{d['strike_high']:.0f}"
    print(f"  {d['id']:<4} {d['ticker']:<7} {spread:<18} {d['status']:<8} {d['close_reason']}")

# Verificación de seguridad: DAL (18) NO debe estar en la lista
if 18 in TARGET_IDS:
    print("\n  ⛔ DAL (18) está en la lista de objetivos — abortando por seguridad.")
    cur.close(); conn.close(); sys.exit(1)

found_ids = [dict(zip(cols, r))["id"] for r in rows]
if sorted(found_ids) != sorted(TARGET_IDS):
    print(f"\n  ⚠️  Los IDs encontrados {sorted(found_ids)} no coinciden con "
          f"los objetivos {sorted(TARGET_IDS)}.")
    print("  Revisá antes de commitear. Abortando.")
    cur.close(); conn.close(); sys.exit(1)

if not COMMIT:
    print("\n  DRY-RUN — no se escribió nada. Corré con --commit para aplicar.\n")
    cur.close(); conn.close(); sys.exit(0)

cur.execute("""
    UPDATE paper_positions SET
        status       = 'CLOSED',
        close_reason = 'PRE_RULES',
        closed_at    = NOW(),
        gross_pnl    = 0,
        pnl_pct      = 0,
        profit_pct_of_max = 0,
        last_synced_at = NOW()
    WHERE id = ANY(%s)
""", (TARGET_IDS,))

conn.commit()
print(f"\n  ✅ {cur.rowcount} registros marcados como PRE_RULES. "
      f"DAL (18) intacto y abierto.\n")

cur.close()
conn.close()