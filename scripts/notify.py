"""
notify.py
=========
Fuente ÚNICA de notificaciones ntfy.

Reemplaza las dos copias de send_ntfy que vivían en auto_run.py y monitor.py y
que YA habían divergido:

    monitor.py  -> chequeaba status_code, aceptaba `tags`, mandaba Content-Type
    auto_run.py -> nada de eso: un HTTP 500 pasaba como enviado

Misma enfermedad que las tres copias de pricing: el arreglo se aplicó a una copia
y la otra quedó ciega. Acá la función vive en un solo lugar.

Qué agrega sobre las dos:
  - reintentos con backoff (2s, 4s). El 16-jul un 'Errno 101 Network is
    unreachable' mató el push del run de las 10:00 sin dejar rastro.
  - raise_for_status(): un 4xx/5xx es un fallo, no un éxito silencioso.
  - devuelve bool. Antes ningún llamador podía saber si el aviso llegó.

Lo que NO arregla: un proceso que no alcanza la red no puede avisarte de que no
alcanza la red. El reintento baja la probabilidad; la visibilidad real solo la
da un watcher externo (dead-man's switch). Ver §22.

Uso:
    from notify import send_ntfy
    ok = send_ntfy("Título", "cuerpo", priority="high")
"""
import os
import time

import requests
from dotenv import load_dotenv

# Para que el módulo sirva solo (python -c, scripts sueltos). Los llamadores que
# ya hacen load_dotenv() no se ven afectados: es idempotente.
load_dotenv()

NTFY_RETRIES = 3
NTFY_TIMEOUT = 10


def _topic():
    """
    NTFY_TOPIC se lee AL USAR, no al importar.

    Leerla a nivel de módulo la ataba al orden de los imports: monitor.py hace
    `from notify import send_ntfy` en la línea 43 y `load_dotenv()` en la 48
    — el topic salía vacío y el monitor dejaba de notificar en silencio.
    Mismo defecto que CAPITAL = os.getenv(...) a nivel de módulo.
    """
    return os.getenv("NTFY_TOPIC", "").strip()


def _base_url():
    return os.getenv("NTFY_BASE_URL", "https://ntfy.sh").rstrip("/")


def send_ntfy(title, message, priority="default", tags=None) -> bool:
    """
    Manda un push por ntfy. Devuelve True SOLO si ntfy lo aceptó.

    title    : encabezado (se codifica a utf-8)
    message  : cuerpo
    priority : 'min' | 'low' | 'default' | 'high' | 'urgent'
    tags     : lista de tags de ntfy (opcional) — venía de monitor.py

    Sin NTFY_TOPIC avisa y devuelve False: no se envió nada, y eso NO es un éxito.
    """
    topic = _topic()
    if not topic:
        # Antes esto devolvía False callado. Un topic ausente NO es "no hay nada
        # que notificar": es un error de config que apaga TODAS las alertas.
        print("  ⛔ NTFY_TOPIC vacía o ausente — no se envió el push. "
              "¿Falta load_dotenv() o la env var?")
        return False

    url = f"{_base_url()}/{topic}"
    headers = {
        "Title":        title.encode("utf-8"),
        "Priority":     priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    for intento in range(1, NTFY_RETRIES + 1):
        try:
            resp = requests.post(
                url,
                data=message.encode("utf-8"),
                headers=headers,
                timeout=NTFY_TIMEOUT,
            )
            resp.raise_for_status()
            if intento > 1:
                print(f"  ntfy OK en el intento {intento}")
            return True
        except Exception as e:
            print(f"  ntfy intento {intento}/{NTFY_RETRIES}: {e}")
            if intento < NTFY_RETRIES:
                time.sleep(2 ** intento)      # 2s, 4s

    print(f"  ⛔ ntfy: los {NTFY_RETRIES} intentos fallaron — el aviso NO llegó")
    return False