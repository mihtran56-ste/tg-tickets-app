# Сторож для бота TG_Tickets: держит процесс живым, перезапускает при падении,
# пишет общий лог. Запускается через start_bot_watchdog.cmd.
#
# Остановить: закрыть окно, либо (если запущено скрыто) завершить процесс pwsh,
# в командной строке которого есть watchdog.ps1.

param(
    [int]$RestartDelaySec = 10,
    [int]$MaxLogMb = 5
)

$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$botScript = Join-Path $root 'bot.py'
$logDir = Join-Path $root 'logs'
$log = Join-Path $logDir 'bot.log'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Чтобы русский текст из Python не превращался в кракозябры при перенаправлении
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # в некоторых окружениях консоль недоступна — не критично
}

function Write-Log {
    param([string]$Message)
    $line = '{0} | {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $log -Value $line -Encoding utf8
}

if (-not (Test-Path $python)) {
    Write-Log "ОШИБКА: не найден python: $python"
    exit 1
}

# Ротация лога, чтобы файл не разрастался
if ((Test-Path $log) -and ((Get-Item $log).Length -gt ($MaxLogMb * 1MB))) {
    $archived = Join-Path $logDir ('bot-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Move-Item -Path $log -Destination $archived -Force
}

Write-Log "сторож запущен (python: $python)"

# PID-файл, чтобы можно было аккуратно остановить через stop_bot.cmd
$pidFile = Join-Path $logDir 'watchdog.pid'
$PID | Out-File -FilePath $pidFile -Encoding ascii -Force

while ($true) {
    Write-Log 'запуск бота'
    & $python $botScript *>&1 | ForEach-Object { Write-Log $_ }
    $code = $LASTEXITCODE
    Write-Log "бот завершился (код $code), перезапуск через ${RestartDelaySec}с"
    Start-Sleep -Seconds $RestartDelaySec
}

# Сюда попадаем только если цикл прерван — убираем PID-файл
Remove-Item -Path $pidFile -Force -ErrorAction SilentlyContinue
