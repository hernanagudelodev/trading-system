"""
telegram_bot.py
===============
Bot de lectura + ACCIONES CONTROLADAS. Escucha comandos en Telegram, corre un
script de la whitelist (lectura) o ejecuta una accion con salvaguardas, y
devuelve la salida.

QUÉ CAMBIA ESTO EN EL SISTEMA
    El sistema era solo-salida; esto abre un canal de ENTRADA. El bot es publico:
    cualquiera que descubra su username puede escribirle. Por eso la primera
    linea del handler es el filtro de chat_id: solo el chat autorizado opera.

    Los comandos de LECTURA (whitelist) no modifican nada. Los comandos de
    ACCION (close/pause/resume) SI tocan el sistema, y por eso cada uno tiene su
    salvaguarda: /close y /resume piden /confirm (2 pasos); /pause frena y es
    seguro por definicion (frenar no arriesga).

REGLAS QUE NO SE NEGOCIAN
    1. Whitelist, no interpretación (para lectura). COMANDOS es un dict fijo
       comando->script. Nunca se arma un comando con texto del usuario.
    2. subprocess con LISTA, nunca shell=True. El argumento viaja como un argv
       literal: la inyección de shell es imposible por construcción.
    3. Las acciones son POCAS, EXPLICITAS y con salvaguarda. No hay una via
       generica de escritura: cada accion (close/pause/resume) es una funcion
       dedicada, no un script parametrizable por el usuario.

    ACCIONES ACTUALES
      /close TICKER -> cierra en LIVE (dry-run + /confirm).
      /pause        -> frena aperturas live (kill_live.py off). Directo.
      /resume       -> reactiva aperturas (kill_live.py on). Pide /confirm.
    /pause y /resume escriben el kill-flag system_state['live_kill'], que el
    executor lee antes de cada apertura (live_trading_allowed). NO tocan el
    auto-cierre (eso es MONITOR_AUTO_CLOSE, mecanismo aparte).

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
import threading
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

# Un argumento válido: 'live'/'paper' (libro), dígitos, 'all', fecha, o '--log'.
# Cada token se valida por separado; nada que no matchee esto llega al script.
ARG_OK = re.compile(r"^(live|paper|all|--log|--resumen|--ticker|[a-z]{1,5}|\d{1,3}|\d{4}-\d{2}-\d{2})$")

# ── WHITELIST ────────────────────────────────────────────────────────────────
# comando -> (ruta del script, acepta_argumento, descripción)
COMANDOS = {
    # comando -> (script, acepta_arg, dobla_libro, descripcion)
    # dobla_libro=True: sin 'live'/'paper', el bot corre el script DOS veces
    # (live y paper) y junta la salida. def corre ambos libros -> se ven ambos.
    "/open":    (os.path.join(_TOOLS, "check_open.py"),        True,  True,
                 "abiertas y exposicion - arg: live | paper (default ambos)"),
    "/closed":  (os.path.join(_TOOLS, "check_closed.py"),      True,  True,
                 "cerradas y expectativa - arg: live|paper, fecha, all"),
    "/pnl":     (os.path.join(_TOOLS, "pnl_si_cierro.py"),     True,  False,
                 "P&L si cierro (solo live) - arg: --ticker X"),
    "/runs":    (os.path.join(_TOOLS, "check_runs.py"),        True,  False,
                 "razonamiento de los runs - arg: N | fecha | --log"),
    "/health":  (os.path.join(_TOOLS, "check_operational.py"), True,  False,
                 "salud del auto_run - arg: N"),
    "/equity":  (os.path.join(_TOOLS, "equity_change.py"),      True,  False,
                 "cambio de patrimonio NLV - arg: fecha | N dias (default 30d)"),
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
    for cmd, (_, acepta, _dobla, desc) in COMANDOS.items():
        arg = " <arg>" if acepta else ""
        lineas.append(f"{cmd}{arg}\n    {desc}")
    lineas += ["", "-- ACCION (control del sistema) --",
               "/close TICKER\n    dry-run + pide /confirm. Cierra en LIVE.",
               "/pause\n    frena las aperturas live YA (el auto-cierre sigue).",
               "/resume\n    reactiva aperturas + pide /confirm.",
               "/paper_alerts on|off\n    prende/apaga las alertas de nivel de paper.",
               "/set_risk N\n    cambia el tope de riesgo de cartera + pide /confirm.",
               "/auto_run\n    corre auto_run (escanea y ABRE posiciones) + pide /confirm.",
               "/confirm\n    ejecuta el pendiente (cierre o resume, ventana 60s).",
               "", "/help - esta lista"]
    return "\n".join(lineas)


def _correr(script, args, timeout=None):
    """
    Corre el script con el intérprete actual. LISTA, no string: el argumento es
    un argv literal y la shell nunca lo ve.

    timeout: segundos antes de abortar. None usa SCRIPT_TIMEOUT (comandos de
    lectura). auto_run pasa uno largo (tarda minutos).

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
            timeout=timeout if timeout is not None else SCRIPT_TIMEOUT,
            cwd=_SCRIPTS,
            env=entorno,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        lim = timeout if timeout is not None else SCRIPT_TIMEOUT
        return f"El script no terminó en {lim}s — se abortó."
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


# CIERRE POR COMANDO — confirmacion de dos pasos (solo LIVE) ────────────────
# /close <TICKER> -> DRY-RUN (close_live commit=False): precia e informa, NO
# cierra; guarda un pendiente. /confirm dentro de la ventana ejecuta el cierre
# REAL (commit=True). Cualquier otro comando entremedio cancela. El estado vive
# EN MEMORIA: si el bot se reinicia, el pendiente se pierde (correcto).

# Pendiente unificado de confirmacion: cierre ("close"+ticker) o reactivacion
# ("resume"). /confirm ejecuta lo que este pendiente. Vive EN MEMORIA.
_PENDIENTE = {"tipo": None, "dato": None, "ts": 0.0}
_CONFIRM_VENTANA_S = 60
TICKER_RE = re.compile(r"^[A-Za-z]{1,5}$")

# auto_run corre en un thread (tarda minutos) para no bloquear el loop del bot.
# Este flag evita lanzar dos a la vez — un segundo /auto_run mientras uno corre
# se rechaza (dos auto_run simultaneos competirian por el broker y la DB).
_AUTORUN_CORRIENDO = threading.Event()
_AUTORUN_TIMEOUT_S = 900   # 15 min: auto_run escanea el universo + LLM + ordenes


def _lanzar_auto_run():
    """
    Corre auto_run.py en un thread y manda la salida al bot cuando termina. No
    bloquea el loop: el bot sigue respondiendo mientras auto_run trabaja. auto_run
    ya respeta el kill switch de live internamente (live_trading_allowed), asi que
    si live esta en pausa, no abre live — el comando no se salta esa proteccion.
    """
    def _worker():
        try:
            salida = _correr(os.path.join(_SCRIPTS, "auto_run.py"), [],
                             timeout=_AUTORUN_TIMEOUT_S)
            # La salida de auto_run puede ser larga; Telegram corta ~4096 chars.
            # Mandamos la cola (lo mas reciente: el resumen de aperturas/cierres).
            if len(salida) > 3500:
                salida = "…(salida truncada)…\n" + salida[-3500:]
            send_push("auto_run terminó", salida, mono=True)
        except Exception as e:
            send_push("auto_run falló", f"Error inesperado: {e}", mono=False)
        finally:
            _AUTORUN_CORRIENDO.clear()

    t = threading.Thread(target=_worker, daemon=True, name="auto_run")
    t.start()


def _cmd_auto_run():
    """Paso 1: pide confirmacion. auto_run ABRE posiciones reales (si live activo)."""
    if _AUTORUN_CORRIENDO.is_set():
        return ("auto_run", "Ya hay un auto_run corriendo. Espera a que termine.", False)
    _PENDIENTE["tipo"] = "auto_run"
    _PENDIENTE["dato"] = None
    _PENDIENTE["ts"]   = time.time()
    return ("auto_run",
            f"Esto corre auto_run: escanea y ABRE posiciones segun encuentre "
            f"(en live si esta activo, y en paper).\n"
            f"Manda /confirm en los proximos {_CONFIRM_VENTANA_S}s para ejecutar.\n"
            f"Cualquier otra cosa lo cancela.", False)


def _cmd_close(partes):
    if len(partes) < 2:
        return ("close", "Uso: /close TICKER (solo live). Ej: /close SCHW", False)
    ticker = partes[1].upper()
    if not TICKER_RE.match(ticker):
        return ("close", f"'{partes[1]}' no parece un ticker valido.", False)
    from close_live_manual import close_live
    ok, detalle = close_live(ticker, commit=False)
    if not ok:
        _PENDIENTE["tipo"] = None
        return ("close", detalle, False)
    _PENDIENTE["tipo"] = "close"
    _PENDIENTE["dato"] = ticker
    _PENDIENTE["ts"]   = time.time()
    return ("close",
            f"{detalle}\n\nEsto CERRARIA {ticker} en LIVE (plata real).\n"
            f"Manda /confirm en los proximos {_CONFIRM_VENTANA_S}s para ejecutar.\n"
            f"Cualquier otra cosa lo cancela.", False)


def _cmd_confirmar():
    tipo = _PENDIENTE["tipo"]
    edad = time.time() - _PENDIENTE["ts"]
    if not tipo:
        return ("confirmar", "No hay nada pendiente. Manda /close TICKER o /resume primero.", False)
    if edad > _CONFIRM_VENTANA_S:
        _PENDIENTE["tipo"] = None
        return ("confirmar", f"El pendiente expiro ({int(edad)}s). Volve a empezar.", False)

    if tipo == "close":
        tk = _PENDIENTE["dato"]
        _PENDIENTE["tipo"] = None
        from close_live_manual import close_live
        ok, detalle = close_live(tk, commit=True)
        estado = "CERRADA" if ok else "NO se pudo cerrar"
        return ("confirmar", f"{estado}: {tk}\n\n{detalle}", ok)

    if tipo == "resume":
        _PENDIENTE["tipo"] = None
        salida = _correr(os.path.join(_TOOLS, "kill_live.py"), ["on"])
        return ("confirmar", salida, False)

    if tipo == "set_risk":
        val = _PENDIENTE["dato"]
        _PENDIENTE["tipo"] = None
        salida = _correr(os.path.join(_TOOLS, "set_risk_pct.py"), [str(val)])
        return ("confirmar", salida, False)

    if tipo == "auto_run":
        _PENDIENTE["tipo"] = None
        if _AUTORUN_CORRIENDO.is_set():
            return ("confirmar", "Ya hay un auto_run corriendo. Espera a que termine.", False)
        _AUTORUN_CORRIENDO.set()
        _lanzar_auto_run()
        return ("confirmar",
                "auto_run arrancó. Corre en segundo plano (varios minutos) — "
                "te aviso acá cuando termine. Podes seguir usando el bot.", False)

    _PENDIENTE["tipo"] = None
    return ("confirmar", "Pendiente de tipo desconocido — cancelado.", False)


def _cmd_pause():
    """Frena las aperturas live YA (sin confirmacion — frenar es seguro)."""
    _PENDIENTE["tipo"] = None   # un pause cancela cualquier pendiente vivo
    salida = _correr(os.path.join(_TOOLS, "kill_live.py"), ["off"])
    return ("pause", salida, False)


def _cmd_resume():
    """Reactivar habilita plata real -> pide /confirm (2 pasos)."""
    _PENDIENTE["tipo"] = "resume"
    _PENDIENTE["dato"] = None
    _PENDIENTE["ts"]   = time.time()
    return ("resume",
            "Esto REACTIVA las aperturas live (el sistema volvera a abrir plata "
            f"real en los proximos slots).\nManda /confirm en los proximos "
            f"{_CONFIRM_VENTANA_S}s para reactivar.\nCualquier otra cosa lo cancela.",
            False)


def _cmd_set_risk(partes):
    """
    Cambia el tope de riesgo de cartera (system_state, fuente unica). Como AUMENTA
    o baja cuanto puede arriesgar el sistema, pide /confirm (2 pasos).
    Sin argumento: muestra el tope actual.
    """
    # Sin argumento -> mostrar el tope actual
    if len(partes) < 2:
        salida = _correr(os.path.join(_TOOLS, "set_risk_pct.py"), [])
        return ("set_risk", salida, False)

    # Validar que sea un numero en rango
    try:
        val = float(partes[1])
    except ValueError:
        return ("set_risk", f"'{partes[1]}' no es un numero. Uso: /set_risk 60", False)
    if not (0 < val <= 100):
        return ("set_risk", f"{val:.0f}% fuera de rango (0-100).", False)

    # Leer el tope actual para mostrar el cambio (de X a Y)
    actual = None
    try:
        sys.path.insert(0, _SCRIPTS)
        from option_selector import portfolio_risk_pct
        actual = portfolio_risk_pct()
    except Exception:
        pass
    desde = f"{actual:.0f}%" if actual is not None else "?"

    _PENDIENTE["tipo"] = "set_risk"
    _PENDIENTE["dato"] = val
    _PENDIENTE["ts"]   = time.time()
    return ("set_risk",
            f"Cambiarias el tope de riesgo de {desde} a {val:.0f}%.\n"
            f"Esto ajusta cuanto capital puede comprometer el sistema en aperturas.\n"
            f"Manda /confirm en los proximos {_CONFIRM_VENTANA_S}s para aplicar.\n"
            f"Cualquier otra cosa lo cancela.",
            False)


def _cmd_paper_alerts(partes):
    """Prende/apaga las alertas de nivel de paper. Directo (silenciar es seguro)."""
    if len(partes) < 2 or partes[1].lower() not in ("on", "off"):
        return ("paper_alerts", "Uso: /paper_alerts on | off", False)
    arg = partes[1].lower()
    salida = _correr(os.path.join(_TOOLS, "paper_alerts.py"), [arg])
    return ("paper_alerts", salida, False)


def _manejar(texto):
    partes = texto.strip().split()
    if not partes:
        return None

    cmd = partes[0].lower()
    # Telegram manda /open@mi_bot en grupos
    cmd = cmd.split("@", 1)[0]

    if cmd in ("/help", "/start"):
        return ("Trading bot", _ayuda(), False)

    if cmd == "/confirm":
        return _cmd_confirmar()
    if cmd == "/close":
        return _cmd_close(partes)
    if cmd == "/pause":
        return _cmd_pause()
    if cmd == "/resume":
        return _cmd_resume()
    if cmd == "/paper_alerts":
        return _cmd_paper_alerts(partes)
    if cmd == "/set_risk":
        return _cmd_set_risk(partes)
    if cmd == "/auto_run":
        return _cmd_auto_run()
    # Cualquier comando que NO sea /confirm cancela un pendiente vivo.
    if _PENDIENTE["tipo"] and cmd != "/confirm":
        _PENDIENTE["tipo"] = None

    if cmd not in COMANDOS:
        return ("Comando desconocido", f"{cmd} no existe.\n\n{_ayuda()}", False)

    script, acepta, dobla_libro, _ = COMANDOS[cmd]
    args = []
    if len(partes) > 1:
        if not acepta:
            return ("Sin argumentos", f"{cmd} no acepta argumentos.", False)
        # Hasta 3 tokens, cada uno validado por separado. El script decide qué
        # hacer con cada uno (libro, fecha, N); el bot solo garantiza que ningún
        # token contenga nada fuera del patrón — la defensa contra inyección.
        for arg in partes[1:4]:
            if not ARG_OK.match(arg.lower()):
                return ("Argumento inválido",
                        f"'{arg}' no es válido. Se acepta: live, paper, un "
                        f"número, 'all', una fecha YYYY-MM-DD, o --log.", False)
            args.append(arg.lower())

    if not os.path.exists(script):
        return ("Script no encontrado", f"No existe {script}", False)

    # RESUMEN: /open y /closed truncan en Telegram (la tabla crece sin limite).
    # El bot pide --resumen -> el script omite la tabla, manda solo metricas.
    # En terminal el script sigue mostrando el detalle completo.
    if cmd in ("/open", "/closed") and "--resumen" not in args:
        args = args + ["--resumen"]

    # DOBLE LIBRO (opcion C): /open y /closed sin libro explicito -> live Y
    # paper, salida juntada. def corre los dos libros; el bot los muestra.
    pidio_libro = any(a in ("live", "paper") for a in args)
    if dobla_libro and not pidio_libro:
        print(f"  -> corriendo {os.path.basename(script)} x2 (live + paper)")
        out_live  = _correr(script, ["live"]  + args)
        out_paper = _correr(script, ["paper"] + args)
        salida = f"=== LIVE ===\n{out_live}\n\n=== PAPER ===\n{out_paper}"
        return (cmd.lstrip("/"), salida, True)

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
    faltan = [c for c, (ruta, _, _, _) in COMANDOS.items() if not os.path.exists(ruta)]
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