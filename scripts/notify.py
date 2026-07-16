"""
notify.py
=========
Fuente ÚNICA de notificaciones push. Backend: Telegram.

Por qué Telegram y no ntfy.sh:
    El 16-jul, tras un redeploy, el contenedor de Railway dejó de alcanzar
    159.203.148.75:443 (ntfy.sh). Timeout, no rechazo: los paquetes salen y se
    pierden. Desde el laptop funcionaba. Diagnóstico probable: el redeploy movió
    el contenedor a otra IP de egreso y ntfy.sh —servicio gratis— descarta ese
    rango. NO era código: un socket crudo contra la IP fallaba igual.
    Telegram no bloquea rangos de cloud.

Por qué esta función vive en un solo archivo:
    Antes había DOS copias de send_ntfy (auto_run.py y monitor.py), ya
    divergidas — monitor chequeaba status_code, auto_run no. Misma enfermedad
    que las tres copias de pricing. Cambiar de canal hoy costó UN archivo
    justamente por eso.

Config (.env y Railway):
    TELEGRAM_BOT_TOKEN   token de @BotFather      (secreto)
    TELEGRAM_CHAT_ID     id del chat destino      (no secreto)

Uso:
    from notify import send_push
    ok = send_push("Título", "cuerpo", priority="urgent")
"""
import html
import os
import time

import requests
from dotenv import load_dotenv

# Para que el módulo sirva solo (python -c, scripts sueltos). Idempotente.
load_dotenv()

RETRIES  = 3
TIMEOUT  = 10
API_BASE = "https://api.telegram.org"
MAX_LEN  = 4096          # límite duro de Telegram por mensaje

# Telegram no tiene niveles de prioridad como ntfy. Se mapean así:
#   - los de baja prioridad llegan sin sonido (disable_notification)
#   - los urgentes se marcan en el título; el sonido lo pone el cliente
_PREFIX = {
    "urgent":  "🚨 ",
    "high":    "⚠️ ",
    "default": "",
    "low":     "",
    "min":     "",
}
_SILENT = {"low", "min"}


def _token():
    """
    Se lee AL USAR, no al importar.

    Leerlo a nivel de módulo lo ataba al orden de los imports: monitor.py hace
    el import en la línea 43 y load_dotenv() en la 48 — el valor salía vacío y
    el monitor dejaba de notificar en silencio.
    """
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id():
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _build_text(title, message):
    """
    HTML de Telegram con TODO escapado. El cuerpo trae texto del LLM, que puede
    contener <, > o & — sin escapar, Telegram rechaza el mensaje con 400 y el
    aviso se pierde por un carácter.
    """
    t = f"<b>{html.escape(str(title))}</b>"
    m = html.escape(str(message))
    texto = f"{t}\n\n{m}"
    if len(texto) > MAX_LEN:
        marca  = "\n\n[…recortado]"
        corte  = MAX_LEN - len(marca)
        cuerpo = texto[:corte]
        # Cortar en límite de línea SOLO si el salto está cerca del corte. Si no,
        # un cuerpo sin saltos retrocedía hasta el \n del título y se perdía el
        # mensaje entero: 6000 caracteres entraban y salían 27.
        nl = cuerpo.rfind("\n")
        if nl > corte - 200:
            cuerpo = cuerpo[:nl]
        texto = cuerpo + marca
    return texto


def send_push(title, message, priority="default", tags=None) -> bool:
    """
    Manda un push por Telegram. Devuelve True SOLO si Telegram lo aceptó.

    title    : encabezado (va en negrita)
    message  : cuerpo
    priority : 'min' | 'low' | 'default' | 'high' | 'urgent'
    tags     : compatibilidad con la firma vieja de monitor.py — se ignora.

    Sin token o sin chat_id: avisa y devuelve False. Un canal sin configurar NO
    es "no había nada que notificar": es un error de config que apaga TODAS las
    alertas, incluidos los stop loss.
    """
    token = _token()
    chat  = _chat_id()
    if not token or not chat:
        falta = "TELEGRAM_BOT_TOKEN" if not token else "TELEGRAM_CHAT_ID"
        print(f"  ⛔ {falta} vacía o ausente — no se envió el push.")
        return False

    prefijo = _PREFIX.get(priority, "")
    payload = {
        "chat_id":              chat,
        "text":                 _build_text(f"{prefijo}{title}", message),
        "parse_mode":           "HTML",
        "disable_notification": priority in _SILENT,
        "link_preview_options": {"is_disabled": True},
    }
    url = f"{API_BASE}/bot{token}/sendMessage"

    for intento in range(1, RETRIES + 1):
        resp = None
        try:
            resp = requests.post(url, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            if intento > 1:
                print(f"  telegram OK en el intento {intento}")
            return True
        except Exception as e:
            # El cuerpo de la respuesta dice POR QUÉ (chat_id malo, HTML roto).
            detalle = f" | {resp.text[:200]}" if resp is not None else ""
            print(f"  telegram intento {intento}/{RETRIES}: {e}{detalle}")
            if intento < RETRIES:
                time.sleep(2 ** intento)      # 2s, 4s

    print(f"  ⛔ telegram: los {RETRIES} intentos fallaron — el aviso NO llegó")
    return False