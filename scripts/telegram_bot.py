"""
scripts/telegram_bot.py  (v2-bidi)
==================================
Bot de Telegram — orquesta las tools de v2. Escucha comandos (long-polling) y corre
la tool correspondiente por SUBPROCESS (aislado: si una tool se cuelga, el timeout la
corta y el bot sigue vivo). Solo responde al chat autorizado (TELEGRAM_CHAT_ID).

Comandos de LECTURA (paper por defecto):
    /open  /book  /runs  /equity  /closed  /health  /candidates

Comandos de ACCIÓN (dos pasos: piden /confirm, ventana 60s):
    /close TICKER   dry-run + /confirm -> cierra en paper
    /auto_run       + /confirm -> corre un ciclo completo (escanea y ABRE)
    /confirm        ejecuta el pendiente

UN SOLO DUEÑO DE getUpdates: Telegram entrega cada update una vez. Este bot debe ser
el único proceso haciendo getUpdates con este token (def está apagado, así que ok).

Start command en Railway (servicio aparte del worker):
    python scripts/telegram_bot.py

Variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL, TASTYTRADE_*
"""
import os
import sys
import time
import subprocess
from pathlib import Path

import requests
from dotenv import load_dotenv

_THIS_DIR = Path(__file__).resolve().parent            # scripts/
_TOOLS    = _THIS_DIR / "tools"
_REPO_ROOT = _THIS_DIR.parent

_ENV_NAME  = os.getenv("ENV_FILE", ".env.v2")
_ENV_PATH  = Path(_ENV_NAME)
if not _ENV_PATH.is_absolute():
    _ENV_PATH = _REPO_ROOT / _ENV_PATH
load_dotenv(_ENV_PATH)

API_BASE = "https://api.telegram.org"

# comando -> script de tools/ (lectura, sin args)
LECTURA = {
    "/open":       "open.py",
    "/book":       "book.py",
    "/runs":       "runs.py",
    "/equity":     "equity.py",
    "/closed":     "closed.py",
    "/health":     "health.py",
    "/candidates": "candidates.py",
}

# Acción pendiente de /confirm: {"kind", "args", "ts"}
_pending = None
CONFIRM_WINDOW = 60   # segundos


def _token():
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def _chat_allowed(chat_id):
    return str(chat_id) == str(os.getenv("TELEGRAM_CHAT_ID", ""))


def _api(method, **params):
    url = f"{API_BASE}/bot{_token()}/{method}"
    try:
        r = requests.post(url, json=params, timeout=40)
        return r.json()
    except Exception as e:
        print(f"  api error ({method}): {e}")
        return {}


def _run(script, args=None, cwd=None, timeout=200):
    """Corre un script por subprocess y devuelve su stdout (o el error).
    Fuerza UTF-8 en el subprocess: PYTHONIOENCODING para que el hijo ESCRIBA utf-8
    y encoding="utf-8" para que el padre lo LEA — si no, en Windows el pipe usa
    cp1252 y los emojis revientan.
    """
    cmd = [sys.executable, script] + (args or [])
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            cwd=str(cwd or _THIS_DIR), env=env, encoding="utf-8")
        out = (r.stdout or "").strip() or (r.stderr or "").strip() or "(sin salida)"
        return out
    except subprocess.TimeoutExpired:
        return f"⏱ {os.path.basename(script)} tardó demasiado (timeout {timeout}s)"
    except Exception as e:
        return f"error corriendo {os.path.basename(script)}: {e}"


def _help_text():
    return ("🤖 Bot v2 — comandos\n\n"
            "LECTURA (agregá 'live' para el book_label real):\n"
            "  /open · /book · /runs · /equity\n"
            "  /closed · /health · /candidates\n"
            "  ej: /open live · /book live\n\n"
            "ACCIÓN (piden /confirm):\n"
            "  /close TICKER — cierra en paper\n"
            "  /close TICKER live — cierra REAL ⚠️\n"
            "  /auto_run — corre un ciclo (abre en paper)\n"
            "  /confirm — ejecuta el pendiente\n\n"
            "  /help — esta ayuda")


def _process(text):
    global _pending
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].lower()
    args = parts[1:]

    # Sufijo de book_label: "/open live" -> live; sin sufijo -> paper (default).
    book_flag = []
    if args and args[-1].lower() == "live":
        book_flag = ["--live"]
        args = args[:-1]

    if cmd in ("/start", "/help"):
        return _help_text()

    # Lectura: correr la tool (paper o live) y devolver su salida
    if cmd in LECTURA:
        return _run(str(_TOOLS / LECTURA[cmd]), book_flag)

    # /close TICKER [live] -> dry-run + deja pendiente el cierre real
    if cmd == "/close":
        if not args:
            return "uso: /close TICKER  (o  /close TICKER live)"
        ticker = args[0].upper()
        book_label  = "LIVE" if book_flag else "paper"
        dry = _run(str(_TOOLS / "close.py"), [ticker] + book_flag)   # sin --confirm
        _pending = {"kind": "close", "args": [ticker] + book_flag, "ts": time.time()}
        warn = " ⚠️ POSICIÓN REAL" if book_flag else ""
        return (f"{dry}\n\n➡️ mandá /confirm para cerrar {ticker} en {book_label}"
                f"{warn} ({CONFIRM_WINDOW}s).")

    # /auto_run -> deja pendiente el ciclo (abre posiciones EN PAPER)
    if cmd == "/auto_run":
        _pending = {"kind": "auto_run", "args": [], "ts": time.time()}
        return ("⚠️ /auto_run corre un ciclo completo: escanea y ABRE posiciones en paper.\n"
                f"➡️ mandá /confirm para ejecutar ({CONFIRM_WINDOW}s).")

    # /confirm -> ejecuta el pendiente si está dentro de la ventana
    if cmd == "/confirm":
        if not _pending:
            return "no hay nada pendiente."
        if time.time() - _pending["ts"] > CONFIRM_WINDOW:
            _pending = None
            return "el pendiente expiró — volvé a mandar el comando."
        p = _pending; _pending = None
        if p["kind"] == "close":
            return _run(str(_TOOLS / "close.py"), p["args"] + ["--confirm"])
        if p["kind"] == "auto_run":
            # BACKGROUND: auto_run tarda minutos y bloquearía el loop de getUpdates
            # (el bot dejaría de responder). Se lanza con Popen sin esperar; auto_run
            # manda su propio resumen a Telegram al terminar.
            subprocess.Popen(
                [sys.executable, str(_THIS_DIR / "auto_run.py"), "--slot", "manual"],
                cwd=str(_THIS_DIR),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            return ("🚀 auto_run lanzado en segundo plano.\n"
                    "El resumen llega a Telegram al terminar (~2-4 min). "
                    "Mientras tanto el bot sigue disponible.")
        return "pendiente desconocido."

    return f"comando no reconocido: {cmd}\nprobá /help"


def main():
    if not _token() or not os.getenv("TELEGRAM_CHAT_ID"):
        print("  faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return 1
    print(f"  bot v2 escuchando (chat {os.getenv('TELEGRAM_CHAT_ID')})…")

    offset = None
    while True:
        try:
            resp = _api("getUpdates", offset=offset, timeout=30)
            for u in resp.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text   = msg.get("text", "")
                if not text:
                    continue
                if not _chat_allowed(chat_id):
                    continue                       # ignora chats no autorizados
                reply = _process(text)
                if reply:
                    _api("sendMessage", chat_id=chat_id, text=reply)
        except Exception as e:
            print(f"  loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())