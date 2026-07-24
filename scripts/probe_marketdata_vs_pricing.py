"""
probe_marketdata_vs_pricing.py
==============================
Compara DOS formas de preguntar lo mismo, en el mismo instante:

    A) tastytrade.market_data.get_market_data_by_type   (REST, snapshot)
    B) pricing.get_spread_quote                          (DXLink, streaming)

POR QUÉ
    El 23-jul el sistema cerró PAYX $100/$105 pagando 2.02 cuando el libro del
    broker mostraba ~1.18. pricing.py lee por streaming: se suscribe, toma el
    PRIMER evento Quote de cada pata y corta. La doc de tastytrade advierte que
    DXLink publica eventos "as they occur" y que en símbolos de baja liquidez
    puede no haber eventos por minutos u horas. Un put $100 con PAYX en $110 y
    29 DTE es exactamente ese caso.

    El SDK trae un endpoint de consulta puntual que devuelve bid, ask, mid, mark,
    bid_size, ask_size y updated_at en UNA llamada, sin websocket. Esta sonda
    verifica si ese canal y el nuestro dicen lo mismo.

QUÉ MIRAR
    - updated_at : de cuándo es el precio. pricing.py no tiene forma de saberlo.
    - bid_size / ask_size : un precio con size 0 no es un mercado.
    - mark vs mid : en libros anchos el broker publica su valor teórico (mark),
      que es el que usa para mostrar el P&L de tus posiciones. El mid de un
      libro ancho es el promedio de dos precios que nadie va a pagar.
    - la fila FINAL: si REST y pricing difieren, el problema es cómo leemos.

SÍMBOLOS
    Formato OCC, el mismo que ya está en positions.tastytrade_symbol
    (ej. 'PAYX  260821P00100000'). OJO: lleva DOS espacios tras el ticker
    cuando el ticker tiene 4 letras. Copiar tal cual de la DB.

USO
    python probe_marketdata_vs_pricing.py
    python probe_marketdata_vs_pricing.py --long "JNJ   260821P00240000" \\
                                          --short "JNJ   260821P00245000" \\
                                          --ticker JNJ --low 240 --high 245
"""
import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def d(v):
    """Decimal/None -> float/None. No inventa 0.0."""
    return float(v) if v is not None else None


async def leer_rest(long_sym, short_sym):
    from tastytrade import Session
    from tastytrade.market_data import get_market_data_by_type

    cs = os.getenv("TASTYTRADE_CLIENT_SECRET")
    rt = os.getenv("TASTYTRADE_REFRESH_TOKEN")
    if not cs or not rt:
        print("  ERROR: faltan TASTYTRADE_CLIENT_SECRET / TASTYTRADE_REFRESH_TOKEN")
        return None

    session = Session(cs, rt)
    datos = await get_market_data_by_type(session, options=[long_sym, short_sym])
    return {md.symbol: md for md in datos}


def fila_pata(nombre, md):
    if md is None:
        print(f"    {nombre:<8} SIN DATO")
        return None
    bid, ask = d(getattr(md, "bid", None)), d(getattr(md, "ask", None))
    mid, mark = d(getattr(md, "mid", None)), d(getattr(md, "mark", None))
    bs, as_ = d(getattr(md, "bid_size", None)), d(getattr(md, "ask_size", None))
    upd = getattr(md, "updated_at", None)

    def f(x, w=8):
        return f"{x:{w}.4f}" if x is not None else " " * (w - 2) + "--"

    print(f"    {nombre:<8} bid={f(bid)} ask={f(ask)} mid={f(mid)} mark={f(mark)}")
    print(f"    {'':<8} bid_size={bs if bs is not None else '--'} "
          f"ask_size={as_ if as_ is not None else '--'}   updated_at={upd}")
    return {"bid": bid, "ask": ask, "mid": mid, "mark": mark,
            "bid_size": bs, "ask_size": as_}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--long",  default="PAYX  260821P00100000",
                   help="símbolo OCC de la pata LARGA (strike bajo)")
    p.add_argument("--short", default="PAYX  260821P00105000",
                   help="símbolo OCC de la pata CORTA (strike alto)")
    p.add_argument("--ticker", default="PAYX")
    p.add_argument("--low",   type=float, default=100.0)
    p.add_argument("--high",  type=float, default=105.0)
    p.add_argument("--exp",   default="2026-08-21")
    p.add_argument("--type",  default="put", choices=("put", "call"))
    a = p.parse_args()

    print()
    print(f"  {a.ticker} {a.low}/{a.high} {a.exp} ({a.type})")
    print(f"    larga: '{a.long}'")
    print(f"    corta: '{a.short}'")
    print("  " + "=" * 68)

    # ── A · REST snapshot ────────────────────────────────────────────────────
    print("\n  A · get_market_data_by_type  (REST, snapshot)")
    try:
        mds = asyncio.run(leer_rest(a.long, a.short))
    except Exception as e:
        print(f"    ERROR: {e}")
        mds = None

    larga = corta = None
    if mds:
        faltan = [s for s in (a.long, a.short) if s not in mds]
        if faltan:
            print(f"    ⚠️  no volvieron estos símbolos: {faltan}")
            print(f"        devueltos: {list(mds.keys())}")
        larga = fila_pata("larga", mds.get(a.long))
        corta = fila_pata("corta", mds.get(a.short))

    rest_mid = rest_mark = None
    if larga and corta:
        if a.type == "put":       # Bull Put: corta - larga
            if larga["mid"] is not None and corta["mid"] is not None:
                rest_mid = round(corta["mid"] - larga["mid"], 4)
            if larga["mark"] is not None and corta["mark"] is not None:
                rest_mark = round(corta["mark"] - larga["mark"], 4)
        else:                      # Bull Call: larga - corta
            if larga["mid"] is not None and corta["mid"] is not None:
                rest_mid = round(larga["mid"] - corta["mid"], 4)
            if larga["mark"] is not None and corta["mark"] is not None:
                rest_mark = round(larga["mark"] - corta["mark"], 4)

    # ── B · nuestro pricer, por streaming ────────────────────────────────────
    print("\n  B · pricing.get_spread_quote  (DXLink, streaming)")
    try:
        try:
            import pricing
        except ImportError:
            sys.path.insert(0, "scripts")
            import pricing
        nuestro = pricing.get_spread_quote(a.ticker, a.low, a.high, a.exp,
                                           option_type=a.type)
        print(f"    {nuestro}")
    except Exception as e:
        print(f"    ERROR: {e}")
        nuestro = None

    # ── COMPARACIÓN ──────────────────────────────────────────────────────────
    print("\n  " + "=" * 68)
    print(f"\n  {'valor del spread':<34} {'mid':>10}")
    print(f"  {'-'*34} {'-'*10}")
    print(f"  {'REST · de mid de cada pata':<34} "
          f"{rest_mid if rest_mid is not None else 'None':>10}")
    print(f"  {'REST · de mark de cada pata':<34} "
          f"{rest_mark if rest_mark is not None else 'None':>10}")
    print(f"  {'pricing.py (streaming)':<34} "
          f"{nuestro['mid'] if nuestro else 'None':>10}")

    print()
    if rest_mid is not None and nuestro:
        dif = abs(nuestro["mid"] - rest_mid)
        if dif <= 0.03:
            print(f"  → coinciden (dif ${dif:.4f}). El canal no es el problema.")
        else:
            print(f"  → DIFIEREN ${dif:.4f}. Los dos canales leen el mismo mercado")
            print(f"    en el mismo instante y dan valores distintos: el problema")
            print(f"    está en CÓMO leemos, no en el mercado.")
    else:
        print("  → sin comparación posible (falta uno de los dos lados).")
    print()


if __name__ == "__main__":
    main()