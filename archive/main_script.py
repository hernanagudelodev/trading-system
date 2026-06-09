import yfinance as yf
import pandas as pd
import time

from datetime import datetime


fecha_hora = datetime.now().strftime("%Y-%m-%d %H:%M")
print(f"\n=== ANÁLISIS DEL MERCADO — {fecha_hora} ===\n")

tickers = ["MSFT"]

def calcular_rsi(closes, periodo=14):
    delta = closes.diff()
    ganancias = delta.where(delta > 0, 0)
    perdidas = -delta.where(delta < 0, 0)
    avg_ganancias = ganancias.rolling(window=periodo).mean()
    avg_perdidas = perdidas.rolling(window=periodo).mean()
    rs = avg_ganancias / avg_perdidas
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def calcular_hv(closes, periodo=30):
    retornos = closes.pct_change().dropna()
    hv = retornos.rolling(window=periodo).std().iloc[-1]
    return hv * (252 ** 0.5) * 100

def obtener_dias_earnings(ticker):
    try:
        import datetime
        import sys
        import io
        # Silenciar stderr durante la llamada
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        info = yf.Ticker(ticker)
        calendar = info.calendar
        sys.stderr = old_stderr
        
        if not calendar or 'Earnings Date' not in calendar:
            return None
        fechas = calendar['Earnings Date']
        if not fechas:
            return None
        fecha = fechas[0] if isinstance(fechas, list) else fechas
        hoy = datetime.date.today()
        dias = (fecha - hoy).days
        if dias < 0 or dias > 365:
            return None
        return dias
    except:
        sys.stderr = old_stderr
        return None

def calcular_volumen(data):
    volumenes = data["Volume"].squeeze()
    vol_hoy = volumenes.iloc[-1]
    vol_promedio_20d = volumenes.iloc[-20:].mean()
    ratio = (vol_hoy / vol_promedio_20d) * 100
    
    if ratio > 150:
        status = "MUY ALTO"
        score = 0
    elif ratio >= 80:
        status = "NORMAL"
        score = 1
    else:
        status = "BAJO"
        score = -1
        
    return vol_hoy, ratio, status, score

def obtener_iv(ticker, precio_actual):
    try:
        import sys, io
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        
        tk = yf.Ticker(ticker)
        expiraciones = tk.options
        sys.stderr = old_stderr
        
        if not expiraciones:
            return None, "SIN DATOS"
        
        # Buscar expiración más cercana a 30 días
        import datetime
        hoy = datetime.date.today()
        mejor_exp = None
        menor_diferencia = 999
        
        for exp in expiraciones:
            fecha_exp = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
            dias = (fecha_exp - hoy).days
            if abs(dias - 30) < menor_diferencia:
                menor_diferencia = abs(dias - 30)
                mejor_exp = exp
        
        if not mejor_exp:
            return None, "SIN DATOS"
        
        # Obtener cadena de opciones
        cadena = tk.option_chain(mejor_exp)
        calls = cadena.calls
        
        # Encontrar Call ATM
        calls = calls[calls['strike'] > 0]
        idx_atm = (calls['strike'] - precio_actual).abs().idxmin()
        iv = calls.loc[idx_atm, 'impliedVolatility'] * 100
        
        return iv, mejor_exp
        
    except:
        return None, "ERROR"

def obtener_beta(ticker):
    try:
        import sys, io
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        tk = yf.Ticker(ticker)
        beta = tk.info.get("beta")
        sys.stderr = old_stderr
        
        if beta is None:
            return None, "SIN DATOS", 0
            
        if beta > 1.5:
            status = f"ALTA ({beta:.1f})"
            score = -1
        elif beta >= 0.8:
            status = f"NORMAL ({beta:.1f})"
            score = 1
        else:
            status = f"BAJA ({beta:.1f})"
            score = 0
            
        return beta, status, score
        
    except:
        return None, "ERROR", 0


def tendencia_sma50(closes):
    sma50_serie = closes.rolling(window=50).mean()
    sma50_hoy = sma50_serie.iloc[-1]
    sma50_10d = sma50_serie.iloc[-10]
    
    if sma50_hoy > sma50_10d * 1.001:  # subiendo más de 0.1%
        return "SUBIENDO", 1
    elif sma50_hoy < sma50_10d * 0.999:  # bajando más de 0.1%
        return "BAJANDO", -1
    else:
        return "LATERAL", 0


def posicion_52_semanas(closes, precio_actual):
    maximo_52s = closes.tail(252).max()
    minimo_52s = closes.tail(252).min()
    
    rango = maximo_52s - minimo_52s
    posicion = (precio_actual - minimo_52s) / rango * 100
    
    if precio_actual >= maximo_52s * 0.95:
        status = f"CERCA MAXIMO ({posicion:.0f}%)"
        score = -1
    elif precio_actual <= minimo_52s * 1.05:
        status = f"CERCA MINIMO ({posicion:.0f}%)"
        score = 1
    else:
        status = f"ZONA MEDIA ({posicion:.0f}%)"
        score = 1
        
    return maximo_52s, minimo_52s, posicion, status, score

# Función para obtener Open Interest de la Call ATM más cercana a 30 días
def obtener_open_interest(ticker, precio_actual):
    try:
        import sys, io, datetime
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        
        tk = yf.Ticker(ticker)
        expiraciones = tk.options
        sys.stderr = old_stderr
        
        if not expiraciones:
            return None, "SIN DATOS", 0
        
        # Buscar expiración más cercana a 30 días
        hoy = datetime.date.today()
        mejor_exp = None
        menor_diferencia = 999
        
        for exp in expiraciones:
            fecha_exp = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
            dias = (fecha_exp - hoy).days
            if abs(dias - 30) < menor_diferencia:
                menor_diferencia = abs(dias - 30)
                mejor_exp = exp
        
        if not mejor_exp:
            return None, "SIN DATOS", 0
        
        # Obtener cadena de opciones
        cadena = tk.option_chain(mejor_exp)
        calls = cadena.calls
        
        # Encontrar Call ATM
        calls_sorted = calls.reindex(
            (calls['strike'] - precio_actual).abs().sort_values().index
        )
        top5 = calls_sorted.head(5)
        oi = top5['openInterest'].sum()
        
        if oi is None or oi == 0:
            return 0, "SIN DATOS", 0
        elif oi > 10000:
            status = f"ALTO ({oi:,.0f})"
            score = 1
        elif oi >= 1000:
            status = f"NORMAL ({oi:,.0f})"
            score = 0
        else:
            status = f"BAJO ({oi:,.0f})"
            score = -1
            
        return oi, status, score
        
    except:
        return None, "ERROR", 0


# Función para detectar soportes y resistencias basados en pivotes locales

def detectar_soporte_resistencia(closes, precio_actual, ventana=5, zona_pct=0.02):
    precios = closes.values
    pivotes_altos = []
    pivotes_bajos = []
    
    # Detectar pivotes
    for i in range(ventana, len(precios) - ventana):
        es_alto = all(precios[i] >= precios[i-j] and 
                     precios[i] >= precios[i+j] 
                     for j in range(1, ventana+1))
        es_bajo = all(precios[i] <= precios[i-j] and 
                     precios[i] <= precios[i+j] 
                     for j in range(1, ventana+1))
        if es_alto:
            pivotes_altos.append(precios[i])
        if es_bajo:
            pivotes_bajos.append(precios[i])
    
    # Agrupar pivotes en zonas
    def agrupar_zonas(pivotes):
        if not pivotes:
            return []
        zonas = []
        pivotes_ord = sorted(pivotes)
        zona_actual = [pivotes_ord[0]]
        
        for p in pivotes_ord[1:]:
            if (p - zona_actual[0]) / zona_actual[0] <= zona_pct:
                zona_actual.append(p)
            else:
                zonas.append({
                    'nivel': sum(zona_actual) / len(zona_actual),
                    'fuerza': len(zona_actual)
                })
                zona_actual = [p]
        zonas.append({
            'nivel': sum(zona_actual) / len(zona_actual),
            'fuerza': len(zona_actual)
        })
        return zonas
    
    zonas_resistencia = agrupar_zonas(pivotes_altos)
    zonas_soporte = agrupar_zonas(pivotes_bajos)
    
    # Encontrar soporte más cercano por debajo
    soportes_abajo = [z for z in zonas_soporte 
                      if z['nivel'] < precio_actual]
    soporte_cercano = max(soportes_abajo, 
                         key=lambda x: x['nivel']) if soportes_abajo else None
    
    # Encontrar resistencia más cercana por encima
    resistencias_arriba = [z for z in zonas_resistencia 
                           if z['nivel'] > precio_actual]
    resistencia_cercana = min(resistencias_arriba, 
                              key=lambda x: x['nivel']) if resistencias_arriba else None
    
    # Calcular distancias
    dist_soporte = ((precio_actual - soporte_cercano['nivel']) / 
                    precio_actual * 100) if soporte_cercano else None
    dist_resistencia = ((resistencia_cercana['nivel'] - precio_actual) / 
                        precio_actual * 100) if resistencia_cercana else None
    
    # Evaluar posición
    if dist_soporte is not None and dist_resistencia is not None:
        if dist_soporte <= 3:
            status = f"CERCA SOPORTE ({dist_soporte:.1f}%)"
            score = 2
        elif dist_resistencia <= 3:
            status = f"CERCA RESISTENCIA ({dist_resistencia:.1f}%)"
            score = -1
        else:
            status = f"S:{dist_soporte:.1f}% R:{dist_resistencia:.1f}%"
            score = 1
    elif dist_soporte is not None:
        status = f"SOPORTE {dist_soporte:.1f}% abajo"
        score = 1
    elif dist_resistencia is not None:
        status = f"RESISTENCIA {dist_resistencia:.1f}% arriba"
        score = 0
    else:
        status = "SIN DATOS"
        score = 0
        
    return status, score

# Función para detectar patrones de velas en las últimas 3 velas
def detectar_patron_velas(data):
    try:
        opens  = data['Open'].squeeze()
        closes = data['Close'].squeeze()
        highs  = data['High'].squeeze()
        lows   = data['Low'].squeeze()

        # Últimas 3 velas
        o1, o2, o3 = opens.iloc[-3], opens.iloc[-2], opens.iloc[-1]
        c1, c2, c3 = closes.iloc[-3], closes.iloc[-2], closes.iloc[-1]
        h1, h2, h3 = highs.iloc[-3], highs.iloc[-2], highs.iloc[-1]
        l1, l2, l3 = lows.iloc[-3], lows.iloc[-2], lows.iloc[-1]

        cuerpo1 = abs(c1 - o1)
        cuerpo2 = abs(c2 - o2)
        cuerpo3 = abs(c3 - o3)
        rango1  = h1 - l1
        rango2  = h2 - l2
        rango3  = h3 - l3

        # Helpers
        es_verde  = lambda o, c: c > o
        es_rojo   = lambda o, c: c < o
        es_grande = lambda cuerpo, rango: cuerpo > rango * 0.6
        es_doji   = lambda cuerpo, rango: rango > 0 and cuerpo < rango * 0.1

        # ── PATRONES DE 3 VELAS ──────────────────────────────────────────
        # Morning Star
        if (es_rojo(o1, c1) and es_grande(cuerpo1, rango1) and
            es_doji(cuerpo2, rango2) and
            es_verde(o3, c3) and es_grande(cuerpo3, rango3)):
            return "MORNING STAR ✅", 2

        # Evening Star
        if (es_verde(o1, c1) and es_grande(cuerpo1, rango1) and
            es_doji(cuerpo2, rango2) and
            es_rojo(o3, c3) and es_grande(cuerpo3, rango3)):
            return "EVENING STAR ⚠️", -2

        # ── PATRONES DE 2 VELAS ──────────────────────────────────────────
        # Engulfing Alcista
        if (es_rojo(o2, c2) and
            es_verde(o3, c3) and
            o3 <= c2 and c3 >= o2):
            return "ENGULFING ALCISTA ✅", 2

        # Engulfing Bajista
        if (es_verde(o2, c2) and
            es_rojo(o3, c3) and
            o3 >= c2 and c3 <= o2):
            return "ENGULFING BAJISTA ⚠️", -2

        # ── PATRONES DE 1 VELA ───────────────────────────────────────────
        mecha_sup3 = h3 - max(o3, c3)
        mecha_inf3 = min(o3, c3) - l3

        # Marubozu Verde
        if (es_verde(o3, c3) and es_grande(cuerpo3, rango3) and
            mecha_sup3 < cuerpo3 * 0.1 and mecha_inf3 < cuerpo3 * 0.1):
            return "MARUBOZU VERDE ✅", 2

        # Marubozu Rojo
        if (es_rojo(o3, c3) and es_grande(cuerpo3, rango3) and
            mecha_sup3 < cuerpo3 * 0.1 and mecha_inf3 < cuerpo3 * 0.1):
            return "MARUBOZU ROJO ⚠️", -2

        # Martillo
        if (mecha_inf3 > cuerpo3 * 2 and
            mecha_sup3 < cuerpo3 * 0.5):
            return "MARTILLO ✅", 1

        # Estrella Fugaz
        if (mecha_sup3 > cuerpo3 * 2 and
            mecha_inf3 < cuerpo3 * 0.5):
            return "ESTRELLA FUGAZ ⚠️", -1

        # Doji
        if es_doji(cuerpo3, rango3):
            return "DOJI —", 0

        # Sin patrón claro
        if es_verde(o3, c3):
            return "VELA VERDE —", 1
        else:
            return "VELA ROJA —", -1

    except:
        return "SIN DATOS", 0

# Función para calcular el percentil de la IV actual respecto al HV histórico
def calcular_iv_percentil(closes, iv_actual):
    try:
        if iv_actual is None:
            return None, "SIN DATOS", 0
            
        # Calcular HV rolling de 30 días para cada día del año
        retornos = closes.pct_change().dropna()
        hv_historica = (retornos.rolling(window=30)
                                .std()
                                .dropna() * (252**0.5) * 100)
        
        if len(hv_historica) < 30:
            return None, "SIN DATOS", 0
        
        # Calcular percentil de la IV actual vs HV histórica
        percentil = (hv_historica < iv_actual).sum() / len(hv_historica) * 100
        
        if percentil <= 25:
            status = f"BARATA (P{percentil:.0f})"
            score = 2
        elif percentil <= 50:
            status = f"NORMAL-BAJA (P{percentil:.0f})"
            score = 1
        elif percentil <= 75:
            status = f"NORMAL-ALTA (P{percentil:.0f})"
            score = 0
        else:
            status = f"CARA (P{percentil:.0f})"
            score = -1
            
        return percentil, status, score
        
    except:
        return None, "ERROR", 0

# Función para calcular el Put/Call Ratio de la expiración más cercana a 30 días
def calcular_put_call_ratio(ticker, precio_actual):
    try:
        import sys, io, datetime
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        
        tk = yf.Ticker(ticker)
        expiraciones = tk.options
        sys.stderr = old_stderr
        
        if not expiraciones:
            return None, "SIN DATOS", 0
        
        # Buscar expiración más cercana a 30 días
        hoy = datetime.date.today()
        mejor_exp = None
        menor_diferencia = 999
        
        for exp in expiraciones:
            fecha_exp = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
            dias = (fecha_exp - hoy).days
            if abs(dias - 30) < menor_diferencia:
                menor_diferencia = abs(dias - 30)
                mejor_exp = exp
        
        if not mejor_exp:
            return None, "SIN DATOS", 0
        
        cadena = tk.option_chain(mejor_exp)
        
        vol_calls = cadena.calls['volume'].fillna(0).sum()
        vol_puts  = cadena.puts['volume'].fillna(0).sum()
        
        if vol_calls == 0:
            return None, "SIN DATOS", 0
            
        pcr = vol_puts / vol_calls
        
        if pcr > 1.3:
            status = f"MIEDO ({pcr:.2f}) ✅"
            score = 1
        elif pcr >= 0.7:
            status = f"NEUTRAL ({pcr:.2f}) ✅"
            score = 1
        elif pcr >= 0.5:
            status = f"OPTIMISTA ({pcr:.2f}) ⚠️"
            score = 0
        else:
            status = f"EUFORIA ({pcr:.2f}) ⚠️"
            score = -1
            
        return pcr, status, score
        
    except:
        return None, "ERROR", 0

# Función para análisis fundamental básico usando yfinance
def analisis_fundamental(ticker):
    try:
        import sys, io
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        tk = yf.Ticker(ticker)
        info = tk.info
        sys.stderr = old_stderr

        score = 0
        resultados = {}

        # ── P/E RATIO ────────────────────────────────────────────────────
        pe = info.get("trailingPE") or info.get("forwardPE")
        if pe and pe > 0:
            if pe < 15:
                resultados['PE'] = f"BARATO ({pe:.1f})"
                score += 2
            elif pe <= 25:
                resultados['PE'] = f"NORMAL ({pe:.1f})"
                score += 1
            elif pe <= 40:
                resultados['PE'] = f"CARO ({pe:.1f})"
                score += 0
            else:
                resultados['PE'] = f"MUY CARO ({pe:.1f})"
                score += -1
        else:
            resultados['PE'] = "SD"

        # ── EPS GROWTH ───────────────────────────────────────────────────
        eps_actual = info.get("trailingEps")
        eps_forward = info.get("forwardEps")
        if eps_actual and eps_forward and eps_actual != 0:
            crecimiento = ((eps_forward - eps_actual) / abs(eps_actual)) * 100
            if crecimiento > 15:
                resultados['EPS'] = f"CRECIENDO ({crecimiento:.1f}%)"
                score += 2
            elif crecimiento > 0:
                resultados['EPS'] = f"ESTABLE ({crecimiento:.1f}%)"
                score += 1
            elif crecimiento > -10:
                resultados['EPS'] = f"CAYENDO ({crecimiento:.1f}%)"
                score += -1
            else:
                resultados['EPS'] = f"DETERIORO ({crecimiento:.1f}%)"
                score += -2
        else:
            resultados['EPS'] = "SD"

        # ── DEUDA/CAPITAL ────────────────────────────────────────────────
        de = info.get("debtToEquity")
        if de is not None:
            de_ratio = de / 100  # yfinance lo da en porcentaje
            if de_ratio < 1.0:
                resultados['DE'] = f"BAJA ({de_ratio:.1f}x)"
                score += 1
            elif de_ratio <= 2.0:
                resultados['DE'] = f"MODERADA ({de_ratio:.1f}x)"
                score += 0
            else:
                resultados['DE'] = f"ALTA ({de_ratio:.1f}x)"
                score += -1
        else:
            resultados['DE'] = "SD"

        # ── MARGEN DE BENEFICIO ──────────────────────────────────────────
        margen = info.get("profitMargins")
        if margen is not None:
            margen_pct = margen * 100
            if margen_pct > 20:
                resultados['MRG'] = f"ALTO ({margen_pct:.1f}%)"
                score += 2
            elif margen_pct >= 10:
                resultados['MRG'] = f"NORMAL ({margen_pct:.1f}%)"
                score += 1
            elif margen_pct >= 0:
                resultados['MRG'] = f"BAJO ({margen_pct:.1f}%)"
                score += 0
            else:
                resultados['MRG'] = f"NEGATIVO ({margen_pct:.1f}%)"
                score += -2
        else:
            resultados['MRG'] = "SD"

        return resultados, score

    except:
        return {'PE': 'ERROR', 'EPS': 'ERROR',
                'DE': 'ERROR', 'MRG': 'ERROR'}, 0

### Loop principal de análisis

for ticker in tickers:

    try:
        for intento in range(3):  # intenta hasta 3 veces
            try:
                data = yf.download(ticker, period="1y", interval="1d", 
                                progress=False, timeout=20)
                if not data.empty:
                    break
            except:
                if intento < 2:
                    time.sleep(3)  # espera 3 segundos antes de reintentar
                continue
        
        if data.empty or len(data) < 50:
            print(f"{ticker}: SIN DATOS SUFICIENTES — omitiendo")
            continue
            
        closes = data["Close"].squeeze()
    
        precio_actual = closes.iloc[-1]
        precio_25d = closes.iloc[-25]
        tendencia = "ALCISTA" if precio_actual > precio_25d else "BAJISTA"
        
        sma50 = closes.rolling(window=50).mean().iloc[-1]
        sma200 = closes.rolling(window=200).mean().iloc[-1]
        sobre_sma50 = precio_actual > sma50
        sobre_sma200 = precio_actual > sma200

        if sobre_sma50 and sobre_sma200:
            sma_status = "ENCIMA DE AMBAS"
        elif sobre_sma50:
            sma_status = "SOLO SMA50"
        else:
            sma_status = "DEBAJO"

        rsi = calcular_rsi(closes)

        if rsi > 70:
            rsi_status = "SOBRECOMPRADA"
        elif rsi < 30:
            rsi_status = "SOBREVENDIDA"
        elif 40 <= rsi <= 60:
            rsi_status = "NEUTRAL"
        else:
            rsi_status = "PRECAUCION"

        hv = calcular_hv(closes)

        if hv < 20:
            hv_status = "BAJA"
        elif hv <= 35:
            hv_status = "NORMAL"
        else:
            hv_status = "ALTA"

        # Puntuación
        score = 0

        # Tendencia
        score += 1 if tendencia == "ALCISTA" else -1

        # SMA
        if sma_status == "ENCIMA DE AMBAS":
            score += 2
        elif sma_status == "SOLO SMA50":
            score += 1
        else:
            score += -2

        # RSI
        if rsi_status == "NEUTRAL":
            score += 2
        elif rsi_status == "PRECAUCION":
            score += 1
        else:
            score += -1

        # HV
        if hv_status == "BAJA":
            score += 2
        elif hv_status == "NORMAL":
            score += 1
        else:
            score += -1

        # Earnings
        dias_earnings = obtener_dias_earnings(ticker)
        tk_info = yf.Ticker(ticker).info
        es_etf = tk_info.get("quoteType", "") == "ETF"

        if es_etf:
            earnings_status = "ETF"
            # sin penalización ni bonificación
        elif dias_earnings is None:
            earnings_status = "SD"  # sin datos — no penaliza pero tampoco bonifica
        elif dias_earnings > 35:
            earnings_status = f"{dias_earnings}d"
            score += 1
        elif dias_earnings >= 20:
            earnings_status = f"{dias_earnings}d ⚠️"
        elif dias_earnings < 20:
            earnings_status = f"{dias_earnings}d CERCANO"
            score += -2

        # Volumen
        vol_hoy, vol_ratio, vol_status, vol_score = calcular_volumen(data)
        score += vol_score

        # IV
        iv, exp_usada = obtener_iv(ticker, precio_actual)

        if iv is None:
            iv_status = "SIN DATOS"
            iv_score = 0
        elif iv < hv:
            iv_status = f"BARATA ({iv:.1f}% vs HV {hv:.1f}%)"
            iv_score = 2
        elif iv < hv * 1.3:
            iv_status = f"NORMAL ({iv:.1f}% vs HV {hv:.1f}%)"
            iv_score = 1
        else:
            iv_status = f"CARA ({iv:.1f}% vs HV {hv:.1f}%)"
            iv_score = -1

        score += iv_score

        # Beta
        beta, beta_status, beta_score = obtener_beta(ticker)
        score += beta_score

        # Tendencia SMA50
        sma50_tendencia, sma50_t_score = tendencia_sma50(closes)
        score += sma50_t_score

        # Posición en 52 semanas
        max52, min52, pos52, status52, score52 = posicion_52_semanas(closes, precio_actual)
        score += score52

        # Open Interest
        oi, oi_status, oi_score = obtener_open_interest(ticker, precio_actual)
        score += oi_score

        # Soportes y Resistencias
        sr_status, sr_score = detectar_soporte_resistencia(closes, precio_actual)
        score += sr_score

        # Patrones de velas
        vela_patron, vela_score = detectar_patron_velas(data)
        score += vela_score

        # IV Percentil
        iv_pct, iv_pct_status, iv_pct_score = calcular_iv_percentil(closes, iv)
        score += iv_pct_score

        # Put/Call Ratio
        pcr, pcr_status, pcr_score = calcular_put_call_ratio(ticker, precio_actual)
        score += pcr_score

        # Earnings - fundamental analisis
        fund, fund_score = analisis_fundamental(ticker)
        score += fund_score

        fund_str = (f"PE:{fund['PE']} | "
                    f"EPS:{fund['EPS']} | "
                    f"DE:{fund['DE']} | "
                    f"MRG:{fund['MRG']}")

        ### Veredicto final

        SCORE_MAXIMO = 28  
        porcentaje = score / SCORE_MAXIMO

        if porcentaje >= 0.68:    # Se ajusta el umbral para ser menos exigente
            veredicto = "VIABLE ✅"
        elif porcentaje >= 0.35:  # entre 35% y 70%
            veredicto = "PRECAUCION ⚠️"
        else:                      # menos del 35%
            veredicto = "NO OPERAR ❌"
        

        print(f"{fecha_hora} | {ticker}: Score {score:+d} | {veredicto} | "
            f"RSI:{rsi:.1f} | SMA50:{sma50_tendencia} | 52s:{status52} | "
            f"S/R:{sr_status} | Vela:{vela_patron} | IV:{iv_status} | "
            f"IVP:{iv_pct_status} | PCR:{pcr_status} | OI:{oi_status} | "
            f"Beta:{beta_status} | Earnings:{earnings_status} | "
            f"Vol:{vol_ratio:.0f}% ({vol_status}) | {fund_str}")

            
    except Exception as e:
        print(f"{ticker}: ERROR — {e}")
        continue