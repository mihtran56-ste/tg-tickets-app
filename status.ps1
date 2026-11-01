# Показывает состояние мониторинга и последние строки лога.
# Запускается через status.cmd (или: powershell -File status.ps1).

$root = $PSScriptRoot
$statePath = Join-Path $root 'data\sd_state.json'
$logPath = Join-Path $root 'logs\bot.log'
$pidPath = Join-Path $root 'logs\watchdog.pid'

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # консоль может быть недоступна — не критично
}

Write-Host '=== Сторож ===' -ForegroundColor Cyan
$running = $false
if (Test-Path $pidPath) {
    $wdPid = (Get-Content $pidPath -Raw).Trim()
    if ($wdPid -and (Get-Process -Id $wdPid -ErrorAction SilentlyContinue)) {
        Write-Host "работает (PID $wdPid)" -ForegroundColor Green
        $running = $true
    } else {
        Write-Host 'PID-файл есть, но процесс не найден (остановлен)' -ForegroundColor Yellow
        # Убираем устаревший PID, чтобы он не путал следующие проверки
        Remove-Item -Path $pidPath -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host 'не запущен' -ForegroundColor Yellow
}

Write-Host ''
Write-Host '=== Состояние SD ===' -ForegroundColor Cyan
if (Test-Path $statePath) {
    $state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.last_check -gt 0) {
        $lastCheck = [DateTimeOffset]::FromUnixTimeSeconds([long]$state.last_check).ToLocalTime()
        $age = [int]((Get-Date) - $lastCheck.LocalDateTime).TotalMinutes
        Write-Host ("последняя проверка: {0} ({1} мин назад)" -f $lastCheck.ToString('dd.MM.yyyy HH:mm:ss'), $age)
        if ($age -gt 5) {
            Write-Host '  ВНИМАНИЕ: проверка давно не выполнялась — бот, скорее всего, не работает' -ForegroundColor Yellow
        }
    } else {
        Write-Host 'проверок ещё не было'
    }
    Write-Host ("заявок в памяти: {0}" -f $state.seen_numbers.Count)
    Write-Host ("уведомлений отправлено: {0}" -f $state.total_notifications_sent)
    if ($state.last_error) {
        Write-Host ("последняя ошибка: {0}" -f $state.last_error) -ForegroundColor Red
    } else {
        Write-Host 'последняя ошибка: нет'
    }
} else {
    Write-Host 'файл состояния не найден'
}

Write-Host ''
Write-Host '=== Отметка о жизни ===' -ForegroundColor Cyan
$runtimePath = Join-Path $root 'data\runtime.json'
if (Test-Path $runtimePath) {
    $rt = Get-Content $runtimePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($rt.last_alive -gt 0) {
        $alive = [DateTimeOffset]::FromUnixTimeSeconds([long]$rt.last_alive).ToLocalTime()
        $aliveAge = [int]((Get-Date) - $alive.LocalDateTime).TotalSeconds
        Write-Host ("последний «я жив»: {0} ({1} с назад)" -f $alive.ToString('dd.MM.yyyy HH:mm:ss'), $aliveAge)
        if ($aliveAge -gt 90) {
            Write-Host '  ВНИМАНИЕ: сердцебиения нет — процесс, похоже, не работает' -ForegroundColor Yellow
        }
    }
    if ($rt.clean_stop) {
        Write-Host 'остановлен штатно — падением считаться не будет'
    }
    if ($rt.last_outage_notified_at -gt 0) {
        $notified = [DateTimeOffset]::FromUnixTimeSeconds([long]$rt.last_outage_notified_at).ToLocalTime()
        Write-Host ("последний отчёт о падении: {0}" -f $notified.ToString('dd.MM.yyyy HH:mm'))
    } else {
        Write-Host 'отчётов о падении ещё не было'
    }
} else {
    Write-Host 'отметки о жизни нет (бот ещё ни разу не запускался с этой версией)'
}

$pendingPath = Join-Path $root 'data\pending_events.json'
if (Test-Path $pendingPath) {
    $pending = Get-Content $pendingPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $count = @($pending).Count
    Write-Host ("недоставленных сообщений в очереди: {0}" -f $count) -ForegroundColor Yellow
} else {
    Write-Host 'очередь недоставленных пуста'
}

Write-Host ''
Write-Host '=== Последние строки лога ===' -ForegroundColor Cyan
if (Test-Path $logPath) {
    Get-Content $logPath -Encoding UTF8 -Tail 8
} else {
    Write-Host 'лог не создан'
}

Write-Host ''
if ($running) {
    Write-Host 'Остановить: stop_bot.cmd' -ForegroundColor DarkGray
} else {
    Write-Host 'Запустить: start_bot_watchdog.cmd' -ForegroundColor DarkGray
}
