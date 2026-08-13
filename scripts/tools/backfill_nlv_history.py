"""
backfill_nlv_history.py — valida y rellena la serie de NLV en account_snapshots
usando el historico de Tastytrade (get_net_liquidating_value_history).

DOS FASES:
  1. VALIDAR: para cada dia que YA existe en la DB, compara el NLV guardado
     contra el EOD de Tastytrade. Marca diferencias GRANDES (>$100 y >2%) como
     sospechosas. Las chicas son timing intradia normal (la DB guarda a media
     tarde; Tastytrade da el cierre), no errores.
  2. RELLENAR: inserta los dias que Tastytrade tiene y la DB no. Idempotente:
     nunca duplica un dia que ya existe.

Usa `close` (NLV puro EOD) de Tastytrade. Los snapshots rellenados son PARCIALES
(solo net_liquidating_value + snapshot_at); las demas columnas quedan en NULL,
porque Tastytrade no da ese detalle historico. La curva solo usa NLV, asi que
sirve. Del lado TRADING (necesita el SDK y la DB).

Correr con --commit para escribir; sin flag es dry-run (solo reporta).
"""
import os
import sys
import asyncio
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
import psycopg2

COMMIT = "--commit" in sys.argv
UMBRAL_USD = 100.0
UMBRAL_PCT = 2.0


async def _tastytrade_nlv():
    """Devuelve {date: nlv_close} del historico de Tastytrade (1 año)."""
    from tastytrade import Session
    from tastytrade.account import Account
    session = Session(os.getenv("TASTYTRADE_CLIENT_SECRET"),
                      os.getenv("TASTYTRADE_REFRESH_TOKEN"))
    account = (await Account.get(session))[0]
    hist = await account.get_net_liquidating_value_history(session, time_back="1y")
    out = {}
    for r in hist:
        # 'time' viene como '2026-05-27 00:00:00+00'
        d = datetime.fromisoformat(r.time.replace(" ", "T")).date()
        out[d] = float(r.close)
    return out


def _db_nlv_por_dia():
    """
    Devuelve {date: (nlv_ultimo_del_dia, snapshot_at)} de la DB.
    Toma el snapshot mas tardio de cada dia (el mas cercano al cierre).
    """
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (DATE(snapshot_at))
               DATE(snapshot_at) AS dia,
               net_liquidating_value AS nlv,
               snapshot_at
        FROM account_snapshots
        ORDER BY DATE(snapshot_at), snapshot_at DESC
    """)
    out = {}
    for dia, nlv, snap_at in cur.fetchall():
        out[dia] = (float(nlv), snap_at)
    cur.close(); conn.close()
    return out


def _insertar(dia, nlv):
    """Inserta un snapshot parcial (solo NLV) para un dia faltante. EOD 20:00."""
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    snap_at = datetime.combine(dia, datetime.min.time()).replace(hour=20)
    cur.execute("""
        INSERT INTO account_snapshots
            (account_number, net_liquidating_value, snapshot_at, created_at)
        VALUES (%s, %s, %s, NOW())
    """, (os.getenv("TASTYTRADE_ACCOUNT", "5WI77328"), nlv, snap_at))
    conn.commit(); cur.close(); conn.close()


def main():
    print("="*64)
    print(f"  BACKFILL NLV HISTORY {'(COMMIT — escribe)' if COMMIT else '(DRY-RUN)'}")
    print("="*64)

    tt = asyncio.run(_tastytrade_nlv())
    db = _db_nlv_por_dia()
    print(f"\n  Tastytrade: {len(tt)} dias | DB: {len(db)} dias\n")

    # ── FASE 1: VALIDAR los que ya existen ──────────────────────────────────
    print("  --- VALIDACION (dias que ya estan en la DB) ---")
    sospechosos = 0
    for dia in sorted(db.keys()):
        if dia not in tt:
            continue
        db_nlv, snap_at = db[dia]
        tt_nlv = tt[dia]
        diff = db_nlv - tt_nlv
        pct = (abs(diff) / tt_nlv * 100) if tt_nlv else 0
        grande = abs(diff) > UMBRAL_USD and pct > UMBRAL_PCT
        if grande:
            sospechosos += 1
            print(f"  ⚠️  {dia}: DB ${db_nlv:,.0f} vs TT ${tt_nlv:,.0f} "
                  f"(dif ${diff:+,.0f}, {pct:.1f}%) — {snap_at.strftime('%H:%M')} DB")
    if sospechosos == 0:
        print("  ✓ Sin diferencias grandes — las chicas son timing intradia normal.")
    else:
        print(f"  {sospechosos} dia(s) con diferencia grande — revisar.")

    # ── FASE 2: RELLENAR los faltantes ──────────────────────────────────────
    faltantes = sorted(set(tt.keys()) - set(db.keys()))
    print(f"\n  --- RELLENO ---")
    print(f"  Dias en Tastytrade que faltan en la DB: {len(faltantes)}")
    if faltantes:
        print(f"  Rango: {faltantes[0]} a {faltantes[-1]}")
        for dia in faltantes:
            if COMMIT:
                _insertar(dia, tt[dia])
            print(f"    {'INSERTADO' if COMMIT else 'faltaria'}: {dia} -> ${tt[dia]:,.0f}")

    print("\n" + "="*64)
    if not COMMIT and faltantes:
        print("  DRY-RUN. Corre con --commit para insertar los faltantes.")
    elif COMMIT:
        print(f"  LISTO. {len(faltantes)} dias insertados.")
    print("="*64)


if __name__ == "__main__":
    main()