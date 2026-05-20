"""
trade.py
========
Registro de posiciones de opciones.

Usa los nombres exactos que muestra Thinkorswim para que
no tengas que traducir nada.

Comandos:
    python trade.py --open    → registrar nueva posición
    python trade.py --close   → cerrar posición existente
    python trade.py --list    → ver posiciones abiertas

Al abrir, solo necesitas tener Thinkorswim abierto con la
orden lista para ver los valores.
"""

import argparse
from datetime import datetime, date

from db import open_position, close_position, get_open_positions


# ══════════════════════════════════════════════════════════════════════════════
# INPUT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def ask(label, hint=None, default=None, cast=str, required=True):
    """Prompt with optional hint and default."""
    line = f"  {label}"
    if hint:
        line += f"\n    → {hint}"
    if default is not None:
        line += f" [{default}]"
    line += ": "

    while True:
        raw = input(line).strip()

        if not raw:
            if default is not None:
                return default
            if not required:
                return None
            print("    ⚠️  Requerido.\n")
            continue

        try:
            return cast(raw)
        except (ValueError, TypeError):
            print(f"    ⚠️  Valor inválido — se esperaba {cast.__name__}\n")


def ask_date(label, hint=None):
    """Prompt for a date in YYYY-MM-DD format."""
    line = f"  {label}"
    if hint:
        line += f"\n    → {hint}"
    line += " (YYYY-MM-DD): "

    while True:
        raw = input(line).strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print("    ⚠️  Formato inválido. Ejemplo: 2026-06-18\n")


def confirm(msg):
    return input(f"\n  {msg} (s/n): ").strip().lower() in ("s", "si", "sí")


def sep(char="─", width=52):
    print(f"  {char * width}")


# ══════════════════════════════════════════════════════════════════════════════
# OPEN POSITION
# ══════════════════════════════════════════════════════════════════════════════

def cmd_open():
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║          REGISTRAR NUEVA POSICIÓN               ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print()
    print("  Ten Thinkorswim abierto con la orden lista.")
    print("  Los campos usan los mismos nombres que ves en pantalla.")
    print()

    # ── Ticker ───────────────────────────────────────────────────────────────
    sep()
    ticker = ask(
        "Ticker",
        hint="Ej: HAL, XOM, CAT"
    ).upper()

    # ── Tipo de operación ────────────────────────────────────────────────────
    print()
    print("  Estrategia:")
    strategies = {
        "1": "Bull Call Spread",
        "2": "Bear Put Spread",
        "3": "Long Call",
        "4": "Long Put",
        "5": "Cash Secured Put",
        "6": "Covered Call",
    }
    for k, v in strategies.items():
        print(f"    {k}. {v}")
    strategy_key = ask("  Número", cast=str, default="1")
    strategy = strategies.get(strategy_key, "Bull Call Spread")

    # ── Paper o real ─────────────────────────────────────────────────────────
    print()
    is_paper_input = ask(
        "¿Es paper trading?",
        hint="paperMoney en Thinkorswim = s | dinero real = n",
        default="s"
    ).lower()
    is_paper = is_paper_input in ("s", "si", "sí", "y", "yes")
    broker = "Thinkorswim (paperMoney)" if is_paper else "Schwab / Thinkorswim"

    # ── Datos de la orden (lo que ves en Thinkorswim) ────────────────────────
    sep()
    print()
    print(f"  Datos de la orden en Thinkorswim")
    print(f"  (lo que aparece en la barra inferior: BUY +N VERTICAL...)")
    print()

    contracts = ask(
        "Contratos",
        hint="El número que ves: 'Buy 2 Vertical' → escribe 2",
        cast=int
    )

    strike_low = ask(
        "Strike bajo (long strike)",
        hint="El primer número del spread: '41/44' → escribe 41",
        cast=float
    )

    strike_high = ask(
        "Strike alto (short strike)",
        hint="El segundo número del spread: '41/44' → escribe 44",
        cast=float
    )

    expiration = ask_date(
        "Fecha de vencimiento",
        hint="'Jun 18 (30d)' → escribe 2026-06-18"
    )

    print()
    precio_spread = ask(
        "Precio del spread pagado",
        hint="El valor @X.XX en la orden: 'Buy 2 Vertical @1.13 LIMIT' → escribe 1.13\n"
             "    Este es el COSTO TOTAL POR ACCIÓN del spread (ya descontado el sold)",
        cast=float
    )

    price_at_open = ask(
        "Precio de la acción ahora",
        hint="El precio grande en la esquina superior: ej 42.98",
        cast=float
    )

    # ── Cálculos automáticos ─────────────────────────────────────────────────
    total_cost  = round(precio_spread * contracts * 100, 2)
    max_profit  = round((strike_high - strike_low - precio_spread) * contracts * 100, 2)
    breakeven   = round(strike_low + precio_spread, 2)
    max_loss    = total_cost

    print()
    sep("═")
    print(f"  RESUMEN DE LA POSICIÓN")
    sep("═")
    print(f"  Ticker:          {ticker}")
    print(f"  Estrategia:      {strategy}")
    print(f"  Spread:          ${strike_low} / ${strike_high}")
    print(f"  Contratos:       {contracts}")
    print(f"  Vencimiento:     {expiration}")
    print(f"  Precio acción:   ${price_at_open}")
    print()
    print(f"  ── Calculado automáticamente ──")
    print(f"  Costo total:     ${total_cost:.2f}  ({contracts} contratos × {precio_spread} × 100)")
    print(f"  Ganancia máx:    ${max_profit:.2f}  (si acción ≥ ${strike_high} al vencimiento)")
    print(f"  Pérdida máx:     ${max_loss:.2f}  (si acción ≤ ${strike_low} al vencimiento)")
    print(f"  Breakeven:       ${breakeven:.2f}  (precio mínimo para no perder)")
    print()

    # Targets
    target_50 = round(total_cost + max_profit * 0.50, 2)
    target_70 = round(total_cost + max_profit * 0.70, 2)
    stop_loss = round(total_cost * 0.50, 2)
    print(f"  ── Reglas de salida ──")
    print(f"  Cerrar si el spread vale:  ${target_50:.2f} (50%) → ${target_70:.2f} (70%)")
    print(f"  Stop loss si costo cae a:  ${stop_loss:.2f} (50% del costo)")
    print(f"  Cerrar si DTE ≤ 7:         {(expiration - date.today()).days - 7} días desde hoy")
    print()
    sep("═")

    notas = ask(
        "Notas (opcional)",
        hint="Ej: 'Entrada cerca de soporte, RSI 52, sector Energy PRIORITY'",
        required=False
    )

    if not confirm("¿Confirmar registro de posición?"):
        print("\n  ❌ Cancelado.\n")
        return

    # ── Guardar en DB ─────────────────────────────────────────────────────────
    position_data = {
        "ticker":            ticker,
        "strategy":          strategy,
        "broker":            broker,
        "is_paper":          is_paper,
        "strike_low":        strike_low,
        "strike_high":       strike_high,
        "contracts":         contracts,
        "expiration":        expiration,
        "premium_paid":      precio_spread,
        "total_cost":        total_cost,
        "commission_open":   0.0,
        "opened_at":         datetime.now(),
        "price_at_open":     price_at_open,
        "score_at_open":     None,
        "score_pct_at_open": None,
        "verdict_at_open":   None,
        "notes":             notas or "",
        "analysis_id":       None,
    }

    position_id = open_position(position_data)

    print()
    print(f"  ✅ Posición #{position_id} registrada.")
    print(f"  El monitor en Railway la vigilará automáticamente.")
    print(f"  Para ver el estado: python trade.py --list")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# CLOSE POSITION
# ══════════════════════════════════════════════════════════════════════════════

def cmd_close():
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║             CERRAR POSICIÓN                     ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print()

    positions = get_open_positions()

    if not positions:
        print("  No hay posiciones abiertas.\n")
        return

    # Mostrar tabla simple
    sep("═")
    print(f"  {'ID':<4} {'TICKER':<6} {'SPREAD':<10} {'EXP':<12} {'COSTO':>8} {'PAPER'}")
    sep()
    for p in positions:
        spread = f"{p['strike_low']}/{p['strike_high']}"
        exp    = str(p['expiration'])
        cost   = f"${float(p['total_cost'] or 0):.2f}"
        paper  = "📝" if p['is_paper'] else "💵"
        print(f"  {p['id']:<4} {p['ticker']:<6} {spread:<10} {exp:<12} {cost:>8} {paper}")
    sep("═")
    print()

    valid_ids = [p["id"] for p in positions]
    position_id = ask(
        f"ID de la posición a cerrar",
        hint=f"Opciones disponibles: {valid_ids}",
        cast=int
    )

    if position_id not in valid_ids:
        print(f"\n  ❌ ID {position_id} no válido.\n")
        return

    selected = next(p for p in positions if p["id"] == position_id)
    contracts  = selected["contracts"]
    total_cost = float(selected["total_cost"] or 0)

    print()
    print(f"  Cerrando: {selected['ticker']} "
          f"${selected['strike_low']}/{selected['strike_high']} "
          f"exp {selected['expiration']}")
    print(f"  Costo original: ${total_cost:.2f}")
    print()
    print("  En Thinkorswim, cuando ejecutas el cierre verás:")
    print("  'Sell 2 Vertical @X.XX' — ese X.XX es lo que recibes")
    print()

    precio_cierre = ask(
        "Precio del spread al cerrar",
        hint="El valor @X.XX de la orden de cierre en Thinkorswim",
        cast=float
    )

    price_at_close = ask(
        "Precio de la acción al cerrar",
        hint="El precio de la acción en ese momento",
        cast=float
    )

    print()
    print("  Razón de cierre:")
    reasons = {
        "1": "TARGET_REACHED",
        "2": "STOP_LOSS",
        "3": "MANUAL",
        "4": "EXPIRY",
    }
    print("    1. TARGET_REACHED — alcanzaste el 50-70% de ganancia máxima")
    print("    2. STOP_LOSS — perdiste el 50% del costo")
    print("    3. MANUAL — decidiste cerrar por otra razón")
    print("    4. EXPIRY — expiró")
    reason_key = ask("  Número", default="1")
    close_reason = reasons.get(reason_key, "MANUAL")

    notas = ask("Notas (opcional)", required=False)

    # ── Calcular P&L ──────────────────────────────────────────────────────────
    total_received = round(precio_cierre * contracts * 100, 2)
    gross_pnl      = round(total_received - total_cost, 2)
    pnl_pct        = round(gross_pnl / total_cost * 100, 1) if total_cost else 0
    max_profit = (float(selected["strike_high"]) - float(selected["strike_low"])) * contracts * 100 - total_cost
    pct_of_max     = round(gross_pnl / max_profit * 100, 1) if max_profit else 0

    result_icon = "✅" if gross_pnl >= 0 else "❌"
    print()
    sep("═")
    print(f"  RESULTADO")
    sep("═")
    print(f"  Costo original:   ${total_cost:.2f}")
    print(f"  Recibido:         ${total_received:.2f}")
    print(f"  Ganancia/Pérdida: {result_icon} ${gross_pnl:+.2f} ({pnl_pct:+.1f}%)")
    print(f"  % del máximo:     {pct_of_max:.1f}%")
    print(f"  Razón:            {close_reason}")
    sep("═")
    print()

    if not confirm("¿Confirmar cierre?"):
        print("\n  ❌ Cancelado.\n")
        return

    close_data = {
        "premium_received": precio_cierre,
        "commission_close": 0.0,
        "closed_at":        datetime.now(),
        "price_at_close":   price_at_close,
        "close_reason":     close_reason,
        "notes":            notas or "",
    }

    updated = close_position(position_id, close_data)

    print()
    print(f"  ✅ Posición #{position_id} cerrada.")
    print(f"  Resultado: ${updated['net_pnl']:.2f} ({updated['pnl_pct']:.1f}%)")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# LIST POSITIONS
# ══════════════════════════════════════════════════════════════════════════════

def cmd_list():
    print()
    positions = get_open_positions()

    if not positions:
        print("  No hay posiciones abiertas.\n")
        return

    sep("═")
    print(f"  POSICIONES ABIERTAS ({len(positions)})")
    sep("═")

    for p in positions:
        spread     = f"${p['strike_low']}/{p['strike_high']}"
        exp        = str(p['expiration'])
        cost       = float(p['total_cost'] or 0)
        dte        = (p['expiration'] - date.today()).days if p['expiration'] else "?"
        paper_icon = "📝 paper" if p['is_paper'] else "💵 real"

        max_profit = (float(p['strike_high']) - float(p['strike_low'])) * p['contracts'] * 100 - cost
        target_50  = round(cost + max_profit * 0.50, 2)
        target_70  = round(cost + max_profit * 0.70, 2)
        stop_loss  = round(cost * 0.50, 2)

        print(f"  #{p['id']} {p['ticker']} {spread} | {p['strategy']}")
        print(f"     Vence: {exp} ({dte} días) | Costo: ${cost:.2f} | {paper_icon}")
        print(f"     Cerrar si spread vale: ${target_50:.2f}-${target_70:.2f}")
        print(f"     Stop loss si spread vale menos de: ${stop_loss:.2f}")
        if p.get("notes"):
            print(f"     Notas: {p['notes']}")
        sep()

    total = sum(float(p['total_cost'] or 0) for p in positions)
    print(f"  Total invertido: ${total:.2f}\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Registro de posiciones de opciones")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--open",  action="store_true", help="Registrar nueva posición")
    group.add_argument("--close", action="store_true", help="Cerrar posición existente")
    group.add_argument("--list",  action="store_true", help="Ver posiciones abiertas")
    args = parser.parse_args()

    if args.open:
        cmd_open()
    elif args.close:
        cmd_close()
    elif args.list:
        cmd_list()