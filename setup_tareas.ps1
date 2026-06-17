# setup_tareas.ps1
# Registra dos tareas en el Programador de Tareas de Windows:
#   - 09:00  →  marcar entrada
#   - 18:30  →  marcar salida
# Ejecutar UNA SOLA VEZ como administrador:
#   PowerShell -ExecutionPolicy Bypass -File setup_tareas.ps1

$ErrorActionPreference = "Stop"

# ── Rutas ──────────────────────────────────────────────────────────────────────
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPy   = Join-Path $scriptDir "asistencia_buk.py"

# Preferir el Python del venv si existe, si no usar el del sistema
$venvPython = Join-Path $scriptDir "venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonExe = $venvPython
    Write-Host "  Usando Python del venv: $pythonExe"
} else {
    $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $pythonExe) {
        Write-Error "Python no encontrado. Instálalo o crea el venv en $scriptDir\venv"
        exit 1
    }
    Write-Host "  Venv no encontrado, usando Python del sistema: $pythonExe"
}

if (-not (Test-Path $scriptPy)) {
    Write-Error "No se encontró asistencia_buk.py en $scriptDir"
    exit 1
}

Write-Host ""
Write-Host "Configurando tareas automáticas de asistencia Buk..." -ForegroundColor Cyan
Write-Host "  Python : $pythonExe"
Write-Host "  Script : $scriptPy"
Write-Host ""

# ── Función auxiliar ───────────────────────────────────────────────────────────
function Crear-Tarea {
    param(
        [string]$Nombre,
        [string]$Hora,       # formato "HH:mm"
        [string]$Descripcion
    )

    # Eliminar tarea previa si existe
    Unregister-ScheduledTask -TaskName $Nombre -Confirm:$false -ErrorAction SilentlyContinue

    $accion  = New-ScheduledTaskAction `
                    -Execute $pythonExe `
                    -Argument "`"$scriptPy`"" `
                    -WorkingDirectory $scriptDir

    # Lunes a viernes a la hora indicada
    $trigger = New-ScheduledTaskTrigger `
                    -Weekly `
                    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
                    -At $Hora

    $settings = New-ScheduledTaskSettingsSet `
                    -ExecutionTimeLimit  (New-TimeSpan -Minutes 10) `
                    -StartWhenAvailable `
                    -WakeToRun:$false `
                    -RunOnlyIfNetworkAvailable:$false

    # Correr con el usuario actual, sin requerir contraseña visible
    $principal = New-ScheduledTaskPrincipal `
                    -UserId $env:USERNAME `
                    -LogonType Interactive `
                    -RunLevel Limited

    Register-ScheduledTask `
        -TaskName   $Nombre `
        -Action     $accion `
        -Trigger    $trigger `
        -Settings   $settings `
        -Principal  $principal `
        -Description $Descripcion `
        -Force | Out-Null

    Write-Host "  ✅ Tarea '$Nombre' creada para las $Hora (L-V)" -ForegroundColor Green
}

# ── Crear las dos tareas ───────────────────────────────────────────────────────
Crear-Tarea `
    -Nombre      "BukAsistencia_Entrada" `
    -Hora        "09:00" `
    -Descripcion "Marca entrada automática en Buk (globalconexus)"

Crear-Tarea `
    -Nombre      "BukAsistencia_Salida" `
    -Hora        "18:30" `
    -Descripcion "Marca salida automática en Buk (globalconexus)"

Write-Host ""
Write-Host "Listo. Las tareas se ejecutarán de lunes a viernes." -ForegroundColor Cyan
Write-Host "Puedes verlas en: Inicio → Programador de tareas → Biblioteca"
Write-Host ""
Write-Host "Para probar manualmente ahora:" -ForegroundColor Yellow
Write-Host "  python `"$scriptPy`""
Write-Host ""
