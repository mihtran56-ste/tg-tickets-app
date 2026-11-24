# Сторож SD-агента: держит опрос ServiceDesk живым, перезапускает при падении.
# Нужен потому, что с хостинга SD недоступен (обрыв на connect), а с этого ПК
# полный сброс фильтра занимает ~3 секунды.
#
# Запуск: start_sd_agent.cmd. Остановка: stop_sd_agent.cmd.

param(
    [int]$RestartDelaySec = 10,
    [int]$MaxLogMb = 5
)

$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$agentScript = Join-Path $root 'sd_agent.py'
$logDir = Join-Path $root 'logs'
$log = Join-Path $logDir 'sd_agent.log'
$pidFile = Join-Path $logDir 'sd_agent.pid'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # консоль может быть недоступна — не критично
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

if ((Test-Path $log) -and ((Get-Item $log).Length -gt ($MaxLogMb * 1MB))) {
    $archived = Join-Path $logDir ('sd_agent-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Move-Item -Path $log -Destination $archived -Force
}

Write-Log "сторож SD-агента запущен (python: $python)"
$PID | Out-File -FilePath $pidFile -Encoding ascii -Force

while ($true) {
    Write-Log 'запуск агента'
    & $python $agentScript *>&1 | ForEach-Object { Write-Log $_ }
    $code = $LASTEXITCODE
    Write-Log "агент завершился (код $code), перезапуск через ${RestartDelaySec}с"
    Start-Sleep -Seconds $RestartDelaySec
}

Remove-Item -Path $pidFile -Force -ErrorAction SilentlyContinue
