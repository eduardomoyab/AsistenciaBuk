# Asistencia Buk — Automatización

Registra automáticamente la entrada (09:00) y salida (18:30) en Buk de lunes a viernes.
Si Buk pide validación por correo, el script la resuelve solo leyendo el código desde Gmail.
No marca asistencia en feriados de Chile.

---

## Requisitos

- Python 3.10+
- Google Chrome instalado
- Cuenta Gmail con verificación en 2 pasos activada

---

## Instalación (una sola vez)

### 1. Clonar el repo y crear el entorno virtual

```powershell
git clone https://github.com/tu-usuario/AsistenciaBuk.git
cd AsistenciaBuk

python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Crear contraseña de aplicación en Google

Esto le permite al script leer tu Gmail sin exponer tu contraseña real.

1. Ve a [myaccount.google.com/security](https://myaccount.google.com/security)
   → asegúrate de tener **Verificación en 2 pasos** activada

2. Ve a [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   → ponle un nombre (ej: `BukAsistencia`) → clic en **Crear**

3. Google te muestra un código de **16 caracteres** tipo `abcd efgh ijkl mnop`
   → cópialo (sin espacios)

### 3. Configurar credenciales

Copia `.env.example` como `.env` y completa los tres valores:

```powershell
copy .env.example .env
```

```
BUK_EMAIL=tu_correo@empresa.cl
BUK_PASSWORD=tu_contraseña_buk
GMAIL_APP_PASSWORD=abcdefghijklmnop   ← los 16 caracteres, sin espacios
```

> ⚠️ Nunca subas `.env` a git. El archivo `.env.example` es la referencia segura.

### 4. Programar las tareas automáticas

Abre PowerShell **como Administrador** desde cualquier directorio y ejecuta:

```powershell
PowerShell -ExecutionPolicy Bypass -File "C:\ruta\donde\clonaste\AsistenciaBuk\setup_tareas.ps1"
```

El script detecta automáticamente el Python del venv. ¡Listo, ya no tienes que hacer nada más!

---

## Cómo funciona

| Hora de ejecución | Acción        |
|-------------------|---------------|
| 09:00 L-V         | Marca Entrada |
| 18:30 L-V         | Marca Salida  |

- El script detecta si es mañana o tarde para elegir el botón correcto.
- Chrome corre en modo invisible (sin ventana).
- 🇨🇱 **No marca asistencia en feriados de Chile** — detectados automáticamente, no necesitas configurar nada.
- Cada ejecución queda registrada en `asistencia.log`.
- Si algo falla, se guarda una captura `debug_YYYYMMDD_HHMMSS.png` para diagnóstico.

### Flujo del código de validación (automático)

Cuando Buk pide el código de 6 dígitos:

1. El script registra la hora exacta en que Buk pidió el código
2. Se conecta a Gmail vía IMAP con la contraseña de aplicación
3. Busca correos de `noreply@buk.cl` recibidos a partir de ese momento (ignora códigos viejos)
4. Reintenta cada 10 segundos durante un máximo de **5 minutos**
5. En cuanto llega el correo, extrae el código y lo ingresa automáticamente en Buk
6. No tienes que hacer nada — todo ocurre en segundo plano

---

## Si la contraseña de aplicación de Google deja de funcionar

Puede pasar si la revocas o cambian permisos. Para regenerarla:

1. Ve a [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Elimina la entrada `BukAsistencia` y crea una nueva
3. Actualiza `GMAIL_APP_PASSWORD` en el archivo `.env`

---

## Ver el log

```powershell
Get-Content asistencia.log -Tail 20
```

---

## Archivos

```
AsistenciaBuk/
├── asistencia_buk.py   ← script principal
├── setup_tareas.ps1    ← configura el Programador de Tareas
├── .env                ← credenciales (NO subir a git)
├── .env.example        ← referencia de variables (sí se puede subir a git)
├── requirements.txt    ← dependencias Python
├── .gitignore
└── README.md
```
