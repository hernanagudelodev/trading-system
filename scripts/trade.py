"""
trade.py
========
Position management script for Bull Call Spread analysis system.

Handles recording of paper and live trading positions.

Modes:
    --open   → Register a new position interactively
    --close  → Close an existing open position
    --list   → Display all open positions

Usage:
    python trade.py --open
    python trade.py --close
    python trade.py --list

Dependencies:
    db.py   → PostgreSQL read/write
    .env    → DATABASE_URL
"""

import argparse
from datetime import datetime, date
from decimal import Decimal

from db import (
    open_position,
    close_position,
    get_open_positions,
    get_analysis_history
)


# ══════════════════════════════════════════════════════════════════════════════
# INPUT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def ask(prompt, required=True, default=None, cast=str):
    """
    Prompt user for input with optional default and type casting.

    Args:
        prompt   (str)      — question to display
        required (bool)     — if True, keeps asking until answer given
        default  (any)      — value to use if user presses Enter
        cast     (callable) — type to cast the input to

    Returns:
        cast value or default
    """
    display = prompt
    if default is not None:
        display += f" [{default}]"
    display += ": "

    while True:
        raw = input(display).strip()

        if not raw:
            if default is not None:
                return default
            if not required:
                return None
            print("  ⚠️  Este campo es requerido.")
            continue

        try:
            return cast(raw)
        except (ValueError, TypeError):
            print(f"  ⚠️  Valor inválido. Se esperaba {cast.__name__}.")


def ask_date(prompt, required=True, default=None):
    """
    Prompt user for a date in YYYY-MM-DD format.
    """
    display = prompt
    if default:
        display += f" [{default}]"
    display += " (YYYY-MM-DD): "

    while True:
        raw = input(display).strip()

        if not raw and default:
            return default
        if not raw and not required:
            return None

        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print("  ⚠️  Formato inválido. Use YYYY-MM-DD (ej: 2026-06-05)")


def ask_bool(prompt, default=True):
    """
    Prompt user for yes/no answer.
    """
    options = "S/n" if default else "s/N"
    raw = input(f"{prompt} ({options}): ").strip().lower()

    if not raw:
        return default
    return raw in ("s", "si", "sí", "yes", "y")


def confirm(message):
    """Ask for final confirmation before saving."""
    raw = input(f"\n{message} (s/n): ").strip().lower()
    return raw in ("s", "si", "sí", "yes", "y")


# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def print_positions_table(positions, title="POSICIONES ABIERTAS"):
    """Print a formatted table of positions."""
    if not positions:
        print("\n  No hay posiciones abiertas.\n")
        return

    print(f"\n{'═' * 80}")
    print(f"  {title}")
    print(f"{'═' * 80}")
    print(f"{'ID':<4} {'TICKER':<8} {'ESTRATEGIA':<20} {'STRIKES':<12} "
          f"{'EXP':<12} {'COSTO':>8} {'ESTADO':<8} {'PAPER'}")
    print(f"{'─' * 80}")

    for p in positions:
        strikes = f"{p['strike_low']}/{p['strike_high']}" if p['strike_low'] else "—"
        exp = str(p['expiration']) if p['expiration'] else "—"
        cost = f"${p['total_cost']:.2f}" if p['total_cost'] else "—"
        paper = "📝 Sí" if p['is_paper'] else "💵 No"

        print(f"{p['id']:<4} {p['ticker']:<8} {p['strategy']:<20} "
              f"{strikes:<12} {exp:<12} {cost:>8} {p['status']:<8} {paper}")

    print(f"{'═' * 80}\n")


def print_position_detail(p):
    """Print full detail of a single position."""
    print(f"\n{'─' * 50}")
    print(f"  ID: {p['id']} | {p['ticker']} | {p['strategy']}")
    print(f"{'─' * 50}")
    print(f"  Strikes:      {p['strike_low']} / {p['strike_high']}")
    print(f"  Contratos:    {p['contracts']}")
    print(f"  Expiración:   {p['expiration']}")
    print(f"  Premium:      ${p['premium_paid']:.2f}/acción")
    print(f"  Costo total:  ${p['total_cost']:.2f}")
    print(f"  Comisión:     ${p['commission_open']:.2f}")
    print(f"  Precio acción al abrir: ${p['price_at_open']:.2f}")
    print(f"  Abierta:      {p['opened_at']}")
    print(f"  Paper trade:  {'Sí' if p['is_paper'] else 'No'}")
    if p['notes']:
        print(f"  Notas:        {p['notes']}")
    print(f"{'─' * 50}\n")


# ══════════════════════════════════════════════════════════════════════════════
# OPEN POSITION
# ══════════════════════════════════════════════════════════════════════════════

def cmd_open():
    """
    Interactively collect position data and save to DB.
    """
    print(f"\n{'═' * 50}")
    print("  REGISTRAR NUEVA POSICIÓN")
    print(f"{'═' * 50}\n")

    # ── Basic info ────────────────────────────────────────────────────────────
    ticker    = ask("Ticker (ej: MSFT)", cast=str).upper()
    strategy  = ask("Estrategia", default="BULL_CALL_SPREAD")
    broker    = ask("Broker", default="Thinkorswim")
    is_paper  = ask_bool("¿Es paper trade?", default=True)

    print()

    # ── Spread structure ──────────────────────────────────────────────────────
    strike_low  = ask("Strike comprado ($)", cast=float)
    strike_high = ask("Strike vendido ($)", cast=float)
    contracts   = ask("Número de contratos", default=1, cast=int)
    expiration  = ask_date("Fecha de expiración", default=None)

    print()

    # ── Entry details ─────────────────────────────────────────────────────────
    premium_paid    = ask("Premium pagado ($ por acción)", cast=float)
    commission_open = ask("Comisión al abrir ($)", default=0.0, cast=float)
    price_at_open   = ask("Precio de la acción al abrir ($)", cast=float)

    print()

    # ── Score context — buscar en DB o ingresar manual ────────────────────────
    print("  Buscando análisis reciente en DB...", end=" ")
    recent = get_analysis_history(ticker=ticker, days=1)

    score_at_open     = None
    score_pct_at_open = None
    verdict_at_open   = None

    if recent:
        latest = recent[0]
        score_at_open     = latest["score"]
        score_pct_at_open = float(latest["score_pct"])
        verdict_at_open   = latest["verdict"]
        print(f"✅ Encontrado: Score {score_at_open} ({score_pct_at_open}%) — {verdict_at_open}")
    else:
        print("No encontrado.")
        use_manual = ask_bool("¿Ingresar score manualmente?", default=False)
        if use_manual:
            score_at_open     = ask("Score al abrir", cast=int, required=False)
            score_pct_at_open = ask("Score % al abrir", cast=float, required=False)
            verdict_at_open   = ask("Veredicto al abrir", required=False)

    print()

    # ── Notes ─────────────────────────────────────────────────────────────────
    notes = ask("Notas (opcional)", required=False)

    # ── Calculate totals ──────────────────────────────────────────────────────
    total_cost = premium_paid * contracts * 100
    breakeven  = strike_low + premium_paid
    max_profit = (strike_high - strike_low - premium_paid) * contracts * 100
    max_loss   = total_cost + commission_open

    # ── Summary before saving ─────────────────────────────────────────────────
    print(f"\n{'─' * 50}")
    print(f"  RESUMEN DE LA POSICIÓN")
    print(f"{'─' * 50}")
    print(f"  Ticker:         {ticker}")
    print(f"  Estrategia:     {strategy}")
    print(f"  Strikes:        ${strike_low} / ${strike_high}")
    print(f"  Contratos:      {contracts}")
    print(f"  Expiración:     {expiration}")
    print(f"  Premium:        ${premium_paid:.2f}/acción")
    print(f"  Costo total:    ${total_cost:.2f}")
    print(f"  Comisión:       ${commission_open:.2f}")
    print(f"  Pérdida máx:    ${max_loss:.2f}")
    print(f"  Breakeven:      ${breakeven:.2f}")
    print(f"  Ganancia máx:   ${max_profit:.2f}")
    print(f"  Paper trade:    {'Sí' if is_paper else 'No'}")
    if score_at_open:
        print(f"  Score entrada:  {score_at_open} ({score_pct_at_open}%) — {verdict_at_open}")
    if notes:
        print(f"  Notas:          {notes}")
    print(f"{'─' * 50}")

    if not confirm("¿Confirmar y guardar esta posición?"):
        print("\n  ❌ Posición cancelada.\n")
        return

    # ── Save to DB ────────────────────────────────────────────────────────────
    position_data = {
        "ticker":           ticker,
        "strategy":         strategy,
        "broker":           broker,
        "is_paper":         is_paper,
        "strike_low":       strike_low,
        "strike_high":      strike_high,
        "contracts":        contracts,
        "expiration":       expiration,
        "premium_paid":     premium_paid,
        "total_cost":       total_cost,
        "commission_open":  commission_open,
        "opened_at":        datetime.now(),
        "price_at_open":    price_at_open,
        "score_at_open":    score_at_open,
        "score_pct_at_open":score_pct_at_open,
        "verdict_at_open":  verdict_at_open,
        "notes":            notes,
        "analysis_id":      recent[0]["id"] if recent else None,
    }

    position_id = open_position(position_data)

    print(f"\n  ✅ Posición guardada con ID: {position_id}")
    print(f"  Recuerda cerrarla con: python trade.py --close\n")


# ══════════════════════════════════════════════════════════════════════════════
# CLOSE POSITION
# ══════════════════════════════════════════════════════════════════════════════

def cmd_close():
    """
    Select an open position and record its closing details.
    """
    print(f"\n{'═' * 50}")
    print("  CERRAR POSICIÓN")
    print(f"{'═' * 50}")

    # ── Show open positions ───────────────────────────────────────────────────
    positions = get_open_positions()

    if not positions:
        print("\n  No hay posiciones abiertas para cerrar.\n")
        return

    print_positions_table(positions)

    # ── Select position ───────────────────────────────────────────────────────
    valid_ids = [p["id"] for p in positions]
    position_id = ask(
        f"ID de la posición a cerrar {valid_ids}",
        cast=int
    )

    if position_id not in valid_ids:
        print(f"\n  ❌ ID {position_id} no válido.\n")
        return

    # Show selected position detail
    selected = next(p for p in positions if p["id"] == position_id)
    print_position_detail(selected)

    # ── Closing details ───────────────────────────────────────────────────────
    print("  Datos de cierre:\n")

    premium_received  = ask("Premium recibido al cerrar ($ por acción)", cast=float)
    commission_close  = ask("Comisión al cerrar ($)", default=0.0, cast=float)
    price_at_close    = ask("Precio de la acción al cerrar ($)", cast=float)

    close_reason_options = [
        "TARGET_REACHED",
        "STOP_LOSS",
        "EXPIRY",
        "MANUAL"
    ]
    print(f"\n  Razones de cierre: {', '.join(close_reason_options)}")
    close_reason = ask("Razón de cierre", default="MANUAL").upper()

    notes = ask("Notas (opcional)", required=False)

    # ── Calculate P&L preview ─────────────────────────────────────────────────
    contracts        = selected["contracts"]
    total_received   = premium_received * contracts * 100
    total_cost       = float(selected["total_cost"] or 0)
    commission_open  = float(selected["commission_open"] or 0)
    total_commission = commission_open + commission_close
    gross_pnl        = total_received - total_cost
    net_pnl          = gross_pnl - total_commission
    pnl_pct          = (net_pnl / total_cost * 100) if total_cost != 0 else 0

    print(f"\n{'─' * 50}")
    print(f"  RESULTADO")
    print(f"{'─' * 50}")
    print(f"  Costo total:       ${total_cost:.2f}")
    print(f"  Recibido:          ${total_received:.2f}")
    print(f"  Ganancia bruta:    ${gross_pnl:.2f}")
    print(f"  Comisiones totales:${total_commission:.2f}")
    print(f"  Ganancia neta:     ${net_pnl:.2f} ({pnl_pct:.1f}%)")
    print(f"  Razón de cierre:   {close_reason}")
    result_icon = "✅" if net_pnl >= 0 else "❌"
    print(f"  {result_icon} {'GANANCIA' if net_pnl >= 0 else 'PÉRDIDA'}")
    print(f"{'─' * 50}")

    if not confirm("¿Confirmar cierre de posición?"):
        print("\n  ❌ Cierre cancelado.\n")
        return

    # ── Save to DB ────────────────────────────────────────────────────────────
    close_data = {
        "premium_received":  premium_received,
        "commission_close":  commission_close,
        "closed_at":         datetime.now(),
        "price_at_close":    price_at_close,
        "close_reason":      close_reason,
        "notes":             notes or "",
    }

    updated = close_position(position_id, close_data)

    print(f"\n  ✅ Posición #{position_id} cerrada.")
    print(f"  Ganancia neta: ${updated['net_pnl']:.2f} ({updated['pnl_pct']:.1f}%)\n")


# ══════════════════════════════════════════════════════════════════════════════
# LIST POSITIONS
# ══════════════════════════════════════════════════════════════════════════════

def cmd_list():
    """Display all open positions."""
    positions = get_open_positions()
    print_positions_table(positions, title="POSICIONES ABIERTAS")

    if positions:
        total_cost = sum(float(p["total_cost"] or 0) for p in positions)
        print(f"  Total invertido: ${total_cost:.2f}\n")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Gestión de posiciones Bull Call Spread"
    )

    group = parser.add_mutually_exclusive_group(required=True)
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