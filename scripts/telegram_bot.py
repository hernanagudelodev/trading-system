"""
telegram_bot.py
===============
Bot de SOLO LECTURA. Escucha comandos en Telegram, corre un script de auditoría
de la whitelist, y devuelve su salida.

QUÉ CAMBIA ESTO EN EL SISTEMA
    Hasta hoy el sistema era solo-salida: hablaba con brokers, DB y Telegram, y
    nada de afuera podía pedirle nada. Esto abre un canal de ENTRADA. El bot es
    público: cualquiera que descubra su username puede escribirle, y Telegram
    acepta mensajes de cualquiera. Por eso la primera línea del handler es el
    filtro de chat_id, y por eso NO hay ningún comando que escriba nada.

TRES REGLAS QUE NO SE NEGOCIAN
    1. Whitelist, no interpretación. COMANDOS es un dict fijo comando->script.
       Nunca se arma un comando con texto del usuario.
    2. subprocess con LISTA, nunca shell=True. El argumento viaja como un argv
       literal: la inyección de shell es imposible por construcción, no por
       validación.
    3. Solo lectura. Ningún comando cierra, abre ni modifica una posición.
       /close sería un control de plata real desde un teléfono que se puede
       perder. Es una decisión distinta a ésta.

UN SOLO DUEÑO DE getUpdates
    Telegram entrega cada update UNA vez. Si dos procesos hacen getUpdates, se
    roban los mensajes entre ellos y los comandos se pierden al azar. Corré este
    bot en UN solo lado: tu laptop o Railway, nunca los dos.

Config (.env / Railway):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL
    (los scripts de la whitelist necesitan además ACCOUNT_NLV,
     MAX_PORTFOLIO_RISK_PCT, TRADING_MODE)

Uso:
    python scripts/telegram_bot.py
"""
import os
import re
import subprocess
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
_TOOLS   = os.path.join(_SCRIPTS, "tools")

sys.path.insert(0, _SCRIPTS)
from notify import send_push          # noqa: E402

API_BASE      = "https://api.telegram.org"
POLL_TIMEOUT  = 30          # long polling: Telegram espera hasta 30s
HTTP_TIMEOUT  = POLL_TIMEOUT + 10
SCRIPT_TIMEOUT = 120        # un script colgado no puede tapar el bot

# Un argumento válido: dígitos, 'all', o fecha YYYY-MM-DD. Nada más.
ARG_OK = re.compile(r"^(all|\d{1,3}|\d{4}-\d{2}-\d{2})$")

# ── WHITELIST ────────────────────────────────────────────────────────────────
# comando -> (ruta del script, acepta_argumento, descripción)
COMANDOS = {
    "/open":        (os.path.join(_TOOLS, "check_open.py"),        False,
                     "posiciones abiertas, exposición y concentración"),
    "/closed":      (os.path.join(_TOOLS, "check_closed.py"),      True,
                     "cerradas y expectativa real · arg: fecha | all"),
    "/runs":        (os.path.join(_TOOLS, "check_runs.py"),        True,
                     "razonamiento de los runs · arg: N | fecha"),
    "/operational": (os.path.join(_TOOLS, "check_operational.py"), True,
                     "salud del auto_run · arg: N"),
}

# NO están en el repo, aunque CONTEXTO_PROYECTO.md los liste en la tabla de
# herramientas de auditoría: check_phantom_closes.py, check_stops.py,
# check_rr_credit.py. Si algún día existen, se agregan acá.


def _token():
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_autorizado():
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _api(metodo, **params):
    url = f"{API_BASE}/bot{_token()}/{metodo}"
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _ayuda():
    lineas = ["Comandos disponibles (solo lectura):", ""]
    for cmd, (_, acepta, desc) in COMANDOS.items():
        arg = " <arg>" if acepta else ""
        lineas.append(f"{cmd}{arg}\n    {desc}")
    lineas += ["", "/help — esta lista"]
    return "\n".join(lineas)


def _correr(script, args):
    """
    Corre el script con el intérprete actual. LISTA, no string: el argumento es
    un argv literal y la shell nunca lo ve.

    PYTHONIOENCODING=utf-8: cuando la salida va a un pipe (que es siempre acá),
    Python en Windows cae al encoding local (cp1252) y revienta con cualquier
    carácter de caja: 'charmap' codec can't encode '─'. En la terminal no pasa
    porque escribe a la consola. auto_run.py se salva por su
    sys.stdout.reconfigure(encoding="utf-8"); los tools/ no lo tienen.
    Se arregla acá, en el que creó el pipe, y no en los seis scripts.
    """
    entorno = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        p = subprocess.run(
            [sys.executable, script, *args],
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            cwd=_SCRIPTS,
            env=entorno,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"El script no terminó en {SCRIPT_TIMEOUT}s — se abortó."
    except Exception as e:
        return f"No se pudo correr el script: {e}"

    salida = (p.stdout or "").strip()
    err    = (p.stderr or "").strip()

    if p.returncode != 0:
        # El script falló: el motivo importa más que la salida parcial.
        return f"[exit {p.returncode}]\n{err or salida or 'sin salida'}"
    if not salida:
        return err or "(sin salida)"
    return salida


def _manejar(texto):
    partes = texto.strip().split()
    if not partes:
        return None

    cmd = partes[0].lower()
    # Telegram manda /open@mi_bot en grupos
    cmd = cmd.split("@", 1)[0]

    if cmd in ("/help", "/start"):
        return ("Trading bot", _ayuda(), False)

    if cmd not in COMANDOS:
        return ("Comando desconocido", f"{cmd} no existe.\n\n{_ayuda()}", False)

    script, acepta, _ = COMANDOS[cmd]
    args = []
    if len(partes) > 1:
        if not acepta:
            return ("Sin argumentos", f"{cmd} no acepta argumentos.", False)
        arg = partes[1]
        if not ARG_OK.match(arg):
            return ("Argumento inválido",
                    f"'{arg}' no es válido. Se acepta: un número, 'all', "
                    f"o una fecha YYYY-MM-DD.", False)
        args = [arg]

    if not os.path.exists(script):
        return ("Script no encontrado", f"No existe {script}", False)

    print(f"  -> corriendo {os.path.basename(script)} {' '.join(args)}")
    return (cmd.lstrip("/"), _correr(script, args), True)


def _drenar_pendientes():
    """
    Al arrancar, descartar lo que quedó en la cola. Si no, un reinicio
    reejecuta comandos viejos y no sabés por qué corrió algo solo.
    """
    try:
        data = _api("getUpdates", timeout=0)
        pend = data.get("result", [])
        if pend:
            offset = pend[-1]["update_id"] + 1
            _api("getUpdates", offset=offset, timeout=0)
            print(f"  {len(pend)} update(s) viejo(s) descartado(s)")
            return offset
    except Exception as e:
        print(f"  no se pudo drenar la cola: {e}")
    return None


def main():
    if not _token() or not _chat_autorizado():
        raise SystemExit(
            "Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID. "
            "Sin chat autorizado el bot no arranca: un bot sin filtro le "
            "responde a cualquiera."
        )

    autorizado = _chat_autorizado()

    try:
        yo = _api("getMe")["result"]
        print(f"\n  Bot: @{yo['username']}")
    except Exception as e:
        raise SystemExit(f"No se pudo hablar con Telegram: {e}")

    print(f"  Chat autorizado: {autorizado}")

    # Validar la whitelist AL ARRANCAR. Un script que no existe se descubría
    # recién al usar el comando: el doc lista tools/ que no están en el repo.
    faltan = [c for c, (ruta, _, _) in COMANDOS.items() if not os.path.exists(ruta)]
    if faltan:
        print(f"  ⛔ comandos con script inexistente: {', '.join(faltan)}")
        print(f"     se van a rechazar. Revisá COMANDOS.")
    vivos = [c for c in COMANDOS if c not in faltan]
    print(f"  Comandos: {', '.join(vivos)}")
    print(f"  Solo lectura. Ctrl-C para salir.\n")

    offset = _drenar_pendientes()
    send_push("Bot arriba", _ayuda())

    while True:
        try:
            data = _api("getUpdates", offset=offset, timeout=POLL_TIMEOUT)
        except Exception as e:
            print(f"  getUpdates falló: {e} — reintento en 5s")
            time.sleep(5)
            continue

        for upd in data.get("result", []):
            offset = upd["update_id"] + 1

            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue

            chat_id = str(msg.get("chat", {}).get("id", ""))

            # ── EL FILTRO ────────────────────────────────────────────────────
            # No es el chat autorizado: se ignora en SILENCIO. Responder algo,
            # aunque sea "no autorizado", le confirma a un desconocido que el
            # bot está vivo y escuchando.
            if chat_id != autorizado:
                quien = msg.get("from", {}).get("username", "?")
                print(f"  ⛔ mensaje de chat no autorizado {chat_id} (@{quien}) — ignorado")
                continue

            texto = msg.get("text", "")
            if not texto:
                continue

            print(f"  <- {texto}")
            r = _manejar(texto)
            if not r:
                continue
            titulo, cuerpo, mono = r
            send_push(titulo, cuerpo, mono=mono)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Bot detenido.\n")