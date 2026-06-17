"""
Registro automático de asistencia en Buk.
Detecta la hora para saber si marcar entrada (mañana) o salida (tarde).
Usa un perfil de Chrome persistente para no tener que loguearse cada vez.
Si Buk pide código de validación, lo lee automáticamente desde Gmail.
"""

import os
import re
import time
import random
import imaplib
import holidays
import email
import email.header
import email.utils
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

# ── Configuración ──────────────────────────────────────────────────────────────

load_dotenv()

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "asistencia.log"

BUK_URL           = "https://globalconexus.buk.cl/"
BUK_LOGIN_URL     = "https://globalconexus.buk.cl/users/sign_in"
ASISTENCIA_URL    = "https://globalconexus.buk.cl/static_pages/portal"
XPATH_ENTRADA     = '//*[@id="web-marking-form"]/div[2]/button[1]/span'
XPATH_SALIDA      = '//*[@id="web-marking-form"]/div[2]/button[2]/span'

EMAIL          = os.getenv("BUK_EMAIL", "")
PASSWORD       = os.getenv("BUK_PASSWORD", "")
CHROME_VISIBLE = os.getenv("CHROME_VISIBLE", "true").lower() == "true"

# Gmail IMAP
GMAIL_USER         = EMAIL                            # mismo correo que Buk
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_IMAP_HOST    = "imap.gmail.com"
BUK_REMITENTE      = "noreply@buk.cl"
BUK_ASUNTO         = "BUK | Código de acceso a BUK"
CODIGO_2FA_TIMEOUT = 15 * 60  # esperar máximo 15 minutos
CODIGO_2FA_POLL    = 10       # revisar Gmail cada 10 segundos

# Hora límite: antes de las 12:00 → entrada; después → salida
HORA_LIMITE = 12

# Tiempos de espera Selenium (segundos)
WAIT_TIMEOUT = 20
SHORT_WAIT   = 5

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Driver ─────────────────────────────────────────────────────────────────────

def crear_driver(visible: bool = False) -> webdriver.Chrome:
    """Crea un driver Chrome limpio (sin perfil persistente)."""
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    if not visible:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")

    service = Service(ChromeDriverManager().install())
    driver  = webdriver.Chrome(service=service, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


# ── Gmail — leer código 2FA ────────────────────────────────────────────────────

def _extraer_codigo(texto: str) -> str | None:
    """
    Busca el código de 6 dígitos en el cuerpo del correo.
    Estrategia 1: buscar el número cerca de palabras clave (más fiable).
    Estrategia 2: buscar un número solo en su propia línea (fallback).
    """
    # Estrategia 1 — número de 6 dígitos cerca de palabras clave del email de Buk
    patrones_contexto = [
        r"(?:código|codigo|code|clave|acceso)[^\d]{0,40}(\d{6})",
        r"(\d{6})[^\d]{0,40}(?:código|codigo|code|validez|minutos)",
    ]
    for patron in patrones_contexto:
        match = re.search(patron, texto, re.IGNORECASE)
        if match:
            return match.group(1)

    # Estrategia 2 — número de 6 dígitos solo en su línea (como aparece en el email)
    for linea in texto.splitlines():
        linea = linea.strip()
        if re.fullmatch(r"\d{6}", linea):
            return linea

    return None


def _decodificar_payload(msg) -> str:
    """
    Extrae el texto del cuerpo del correo.
    Prioriza text/plain; si no existe, usa text/html (stripea los tags).
    """
    texto_plain = None
    texto_html  = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            charset = part.get_content_charset() or "utf-8"
            if ct == "text/plain" and texto_plain is None:
                texto_plain = part.get_payload(decode=True).decode(charset, errors="replace")
            elif ct == "text/html" and texto_html is None:
                texto_html = part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        charset = msg.get_content_charset() or "utf-8"
        raw = msg.get_payload(decode=True).decode(charset, errors="replace")
        if msg.get_content_type() == "text/html":
            texto_html = raw
        else:
            texto_plain = raw

    if texto_plain:
        return texto_plain

    if texto_html:
        # Stripear tags HTML para quedarse solo con el texto
        return re.sub(r"<[^>]+>", " ", texto_html)

    return ""


def obtener_codigo_gmail(desde: datetime) -> str | None:
    """
    Se conecta a Gmail vía IMAP y busca el código de validación de Buk.
    Solo considera correos recibidos a partir de `desde` (hora de inicio del script).
    Reintenta cada CODIGO_2FA_POLL segundos hasta CODIGO_2FA_TIMEOUT segundos.
    Retorna el código de 6 dígitos o None si no llega a tiempo.
    """
    if not GMAIL_APP_PASSWORD:
        log.error("GMAIL_APP_PASSWORD no configurado en .env")
        return None

    # Formato de fecha para búsqueda IMAP (sin hora, solo día)
    fecha_imap = desde.strftime("%d-%b-%Y")
    deadline   = time.time() + CODIGO_2FA_TIMEOUT

    log.info(f"Esperando código de Buk en Gmail (máx. {CODIGO_2FA_TIMEOUT // 60} min)...")

    while time.time() < deadline:
        try:
            with imaplib.IMAP4_SSL(GMAIL_IMAP_HOST) as imap:
                imap.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                imap.select("INBOX")

                # Buscar por remitente y fecha (sin asunto — tiene tildes, IMAP solo acepta ASCII)
                # El filtro de asunto se hace en Python más abajo
                _, ids = imap.search(
                    None,
                    f'(FROM "{BUK_REMITENTE}" SINCE "{fecha_imap}")'
                )

                correo_ids = ids[0].split()
                if not correo_ids:
                    log.info("Código aún no llegó, reintentando...")
                    time.sleep(CODIGO_2FA_POLL)
                    continue

                # Revisar los correos del más reciente al más antiguo
                for uid in reversed(correo_ids):
                    _, data = imap.fetch(uid, "(RFC822)")
                    msg = email.message_from_bytes(data[0][1])

                    # Filtrar por asunto — decodificar primero (puede venir en base64)
                    asunto_raw = msg.get("Subject", "")
                    asunto = " ".join(
                        parte.decode(enc or "utf-8") if isinstance(parte, bytes) else parte
                        for parte, enc in email.header.decode_header(asunto_raw)
                    )
                    if "BUK" not in asunto.upper() and "acceso" not in asunto.lower():
                        continue

                    # Verificar que el correo llegó DESPUÉS de que Buk pidió el código.
                    # Ambos se normalizan a UTC para comparar correctamente
                    # (el correo viene en UTC, desde_codigo está en hora Santiago).
                    fecha_str = msg.get("Date", "")
                    try:
                        fecha_msg = email.utils.parsedate_to_datetime(fecha_str)
                        if fecha_msg.tzinfo is None:
                            fecha_msg = fecha_msg.replace(tzinfo=timezone.utc)
                        # Convertir desde (hora local) a UTC para comparar
                        # Restar 30 segundos de margen por diferencia entre
                        # el momento en que Buk envía el correo y cuando capturamos desde_codigo
                        desde_utc = desde.astimezone(timezone.utc) - timedelta(seconds=10)
                        if fecha_msg < desde_utc:
                            log.info(f"Correo descartado: llegó {fecha_msg} antes de {desde_utc}")
                            continue
                    except Exception as e:
                        log.warning(f"No se pudo parsear fecha del correo ({e}), se procesa igual.")

                    cuerpo = _decodificar_payload(msg)
                    codigo = _extraer_codigo(cuerpo)
                    if codigo:
                        log.info(f"✅ Código recibido: {codigo}")
                        return codigo

        except imaplib.IMAP4.error as e:
            log.error(f"Error IMAP: {e}")

        time.sleep(CODIGO_2FA_POLL)

    log.error(f"No llegó el código en {CODIGO_2FA_TIMEOUT // 60} minutos.")
    return None


# ── Login ──────────────────────────────────────────────────────────────────────

def esta_logueado(driver: webdriver.Chrome) -> bool:
    """Comprueba si ya hay sesión activa."""
    driver.get(BUK_URL)
    time.sleep(2)
    return "sign_in" not in driver.current_url and "login" not in driver.current_url


def hacer_login(driver: webdriver.Chrome, inicio: datetime) -> bool:
    """
    Inicia sesión en Buk con email/contraseña.
    Soporta login en 1 paso (email+pass juntos) y en 2 pasos (email → continuar → pass).
    Si Buk pide código de validación, lo obtiene automáticamente desde Gmail.
    `inicio` es la hora en que arrancó el script (para filtrar correos viejos).
    Retorna True si el login fue exitoso.
    """
    log.info("Iniciando sesión en Buk...")
    driver.get(BUK_LOGIN_URL)
    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    try:
        # Paso 1 — campo email (siempre presente)
        campo_email = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "input[type='email'], input[name='user[email]'], #user_email")
        ))
        campo_email.clear()
        campo_email.send_keys(EMAIL)

        # Verificar si el campo de contraseña ya está visible (login en 1 paso)
        # o hay que hacer clic en "Continuar" primero (login en 2 pasos)
        try:
            campo_pass = driver.find_element(
                By.CSS_SELECTOR, "input[type='password'], input[name='user[password]'], #user_password"
            )
            log.info("Formulario de 1 paso detectado.")
        except NoSuchElementException:
            # Login en 2 pasos: hacer clic en el botón para avanzar al paso de contraseña
            log.info("Formulario de 2 pasos detectado, avanzando al paso de contraseña...")
            boton_siguiente = driver.find_element(
                By.CSS_SELECTOR, "input[type='submit'], button[type='submit'], button[type='button']"
            )
            boton_siguiente.click()

            # Esperar a que aparezca el campo de contraseña
            campo_pass = wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input[type='password'], input[name='user[password]'], #user_password")
            ))

        campo_pass.clear()
        campo_pass.send_keys(PASSWORD)

        boton = driver.find_element(
            By.CSS_SELECTOR, "input[type='submit'], button[type='submit']"
        )
        boton.click()

    except (TimeoutException, NoSuchElementException) as e:
        log.error(f"No se encontró el formulario de login: {e}")
        screenshot = BASE_DIR / f"debug_login_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        driver.save_screenshot(str(screenshot))
        log.info(f"Captura guardada: {screenshot.name}")
        return False

    time.sleep(3)

    # Guardar captura para diagnóstico (se borra si todo va bien)
    screenshot_post_login = BASE_DIR / f"debug_postlogin_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    driver.save_screenshot(str(screenshot_post_login))
    log.info(f"URL tras login: {driver.current_url}")
    log.info(f"Captura guardada: {screenshot_post_login.name} (se puede borrar si el login fue ok)")

    # Detectar si Buk pide código — primero por DOM (más fiable que la URL,
    # ya que Buk puede mostrar el formulario de código en la misma URL de sign_in)
    pide_codigo = False
    try:
        driver.find_element(By.CSS_SELECTOR,
            "input[type='number'], input[name*='code'], input[name*='otp'], "
            "input[id*='code'], input[id*='otp'], input[placeholder*='digo'], "
            "input[placeholder*='code']"
        )
        pide_codigo = True
        log.info("Campo de código detectado en el DOM.")
    except NoSuchElementException:
        pass

    # También revisar la URL por si el DOM no coincide
    if not pide_codigo:
        url = driver.current_url
        pide_codigo = any(k in url for k in ("challenge", "confirmation", "otp", "two_factor", "unlock"))
        if pide_codigo:
            log.info(f"Página de código detectada por URL: {url}")

    if pide_codigo:
        log.info("Buk solicita código de validación. Leyendo Gmail...")
        # Capturar tiempo AHORA (Buk acaba de pedir el código) y esperar
        # 15 seg para dar tiempo a que el correo llegue antes del primer poll
        desde_codigo = datetime.now().astimezone()
        log.info("Esperando 5 segundos para que llegue el correo...")
        time.sleep(5)
        codigo = obtener_codigo_gmail(desde_codigo)

        if not codigo:
            log.error("No se pudo obtener el código de validación.")
            return False

        # Ingresar el código en el formulario
        try:
            campo_codigo = WebDriverWait(driver, SHORT_WAIT).until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    "input[type='number'], input[type='text'][name*='code'], "
                    "input[type='text'][name*='otp'], input[id*='code'], input[id*='otp']"
                ))
            )
            campo_codigo.clear()
            campo_codigo.send_keys(codigo)

            # Confirmar
            boton_confirmar = driver.find_element(
                By.CSS_SELECTOR, "input[type='submit'], button[type='submit']"
            )
            boton_confirmar.click()
            time.sleep(3)
            log.info("Código ingresado y confirmado.")

        except (TimeoutException, NoSuchElementException) as e:
            log.error(f"No se encontró el campo para ingresar el código: {e}")
            screenshot = BASE_DIR / f"debug_2fa_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            driver.save_screenshot(str(screenshot))
            log.info(f"Captura guardada: {screenshot.name}")
            return False

    url_final = driver.current_url
    log.info(f"URL final: {url_final}")

    if "sign_in" in url_final or "login" in url_final:
        # Puede ser contraseña incorrecta O que Buk mostró el código en la misma URL
        # Si hay campo de código visible, no es un error real
        try:
            driver.find_element(By.CSS_SELECTOR,
                "input[type='number'], input[name*='code'], input[name*='otp'], "
                "input[id*='code'], input[id*='otp']"
            )
            log.info("Página de código detectada (misma URL de sign_in). Continuando...")
            desde_codigo = datetime.now().astimezone()
            log.info("Esperando 15 segundos para que llegue el correo...")
            time.sleep(15)
            codigo = obtener_codigo_gmail(desde_codigo)
            if not codigo:
                return False
            campo_codigo = WebDriverWait(driver, SHORT_WAIT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR,
                    "input[type='number'], input[name*='code'], input[name*='otp'], "
                    "input[id*='code'], input[id*='otp']"
                ))
            )
            campo_codigo.clear()
            campo_codigo.send_keys(codigo)
            driver.find_element(By.CSS_SELECTOR, "input[type='submit'], button[type='submit']").click()
            time.sleep(3)
        except NoSuchElementException:
            log.error("Login fallido. Verifica credenciales en .env")
            return False

    if "sign_in" in driver.current_url or "login" in driver.current_url:
        log.error("Login fallido incluso después del código.")
        return False

    log.info("✅ Sesión iniciada correctamente.")
    # Borrar captura de diagnóstico si el login fue exitoso
    try:
        for f in BASE_DIR.glob("debug_postlogin_*.png"):
            f.unlink(missing_ok=True)
    except Exception:
        pass
    return True


# ── Asistencia ─────────────────────────────────────────────────────────────────

def marcar_asistencia(driver: webdriver.Chrome) -> bool:
    """
    Navega al portal de Buk y hace clic en Entrada o Salida según la hora actual.
    """
    hora_actual = datetime.now().hour
    es_entrada  = hora_actual < HORA_LIMITE
    accion      = "Entrada" if es_entrada else "Salida"
    xpath       = XPATH_ENTRADA if es_entrada else XPATH_SALIDA
    log.info(f"Hora actual: {hora_actual}:xx → marcando {accion}")

    driver.get(ASISTENCIA_URL)

    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    try:
        boton = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
        boton.click()
        log.info(f"Botón '{accion}' presionado.")
    except TimeoutException:
        screenshot = BASE_DIR / f"debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        driver.save_screenshot(str(screenshot))
        log.error(f"No se encontró el botón de {accion}. Captura: {screenshot.name}")
        return False

    time.sleep(2)

    # Confirmar modal si aparece
    try:
        confirmar = WebDriverWait(driver, SHORT_WAIT).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(., 'Confirmar') or contains(., 'Aceptar') or contains(., 'OK')]")
            )
        )
        confirmar.click()
        log.info("Modal de confirmación aceptado.")
        time.sleep(2)
    except TimeoutException:
        pass

    log.info(f"✅ {accion} registrada exitosamente el {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    return True


# ── Main ───────────────────────────────────────────────────────────────────────

def es_dia_habil() -> bool:
    """Retorna False si hoy es fin de semana o feriado en Chile."""
    hoy = datetime.now().date()

    if hoy.weekday() >= 5:  # 5=sábado, 6=domingo
        log.info(f"Hoy es {'sábado' if hoy.weekday() == 5 else 'domingo'}, no se marca asistencia.")
        return False

    feriados_cl = holidays.Chile(years=hoy.year)
    if hoy in feriados_cl:
        log.info(f"Hoy es feriado en Chile: {feriados_cl[hoy]}. No se marca asistencia.")
        return False

    return True


def main():
    if not EMAIL or not PASSWORD:
        log.error("Faltan credenciales. Configura BUK_EMAIL y BUK_PASSWORD en .env")
        return

    # Guardar hora de inicio para filtrar correos más viejos en Gmail
    inicio = datetime.now().astimezone()

    log.info("=" * 60)
    log.info(f"Iniciando registro de asistencia — {inicio.strftime('%d/%m/%Y %H:%M')}")

    if not es_dia_habil():
        return

    # Espera aleatoria entre 0 y 5 minutos para no parecer automatizado
    espera = random.randint(0, 300)
    log.info(f"Esperando {espera} segundos antes de iniciar...")
    time.sleep(espera)

    driver = crear_driver(visible=CHROME_VISIBLE)

    try:
        ok = hacer_login(driver, inicio)
        if not ok:
            return

        marcar_asistencia(driver)

    except Exception as e:
        log.exception(f"Error inesperado: {e}")
        screenshot = BASE_DIR / f"error_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        try:
            driver.save_screenshot(str(screenshot))
            log.info(f"Captura de error guardada: {screenshot.name}")
        except Exception:
            pass

    finally:
        driver.quit()
        log.info("Driver cerrado.")


if __name__ == "__main__":
    main()
