"""Будильник для бесплатного сервиса Render.

Free-тариф засыпает после ~15 минут без входящих HTTP-запросов, а long polling
в Telegram входящим трафиком не считается. GitHub Actions держит расписание
нестабильно (проверено: интервалы доходили до 40–60 минут), поэтому дублируем
пинг с компьютера — раз в 5 минут, пока он включён.

Запуск: start_keepalive.cmd (скрытое окно). Остановка: stop_keepalive.cmd
или завершить процесс pythonw.

Почему на Python, а не PowerShell: на этой машине сломан системный TLS
(Schannel, ошибка SEC_E_NO_CREDENTIALS) — curl, git и Invoke-WebRequest
падают, а Python использует собственный TLS и работает.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG = LOG_DIR / "keepalive.log"
PID_FILE = LOG_DIR / "keepalive.pid"

URL = os.getenv("KEEPALIVE_URL", "https://tg-tickets.onrender.com/health")
INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", "300"))
TIMEOUT = int(os.getenv("KEEPALIVE_TIMEOUT", "60"))
MAX_LOG_BYTES = 2 * 1024 * 1024


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {message}"
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def rotate() -> None:
    if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
        archived = LOG_DIR / f"keepalive-{datetime.now():%Y%m%d-%H%M%S}.log"
        LOG.replace(archived)


def ping() -> tuple[bool, int, str]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT) as response:
            body = response.read(200).decode("utf-8", errors="replace")
            return True, int((time.monotonic() - started) * 1000), f"HTTP {response.status} {body.strip()[:60]}"
    except urllib.error.HTTPError as exc:
        return False, int((time.monotonic() - started) * 1000), f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 — будильник не должен падать
        return False, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {exc}"


def main() -> int:
    rotate()
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    log(f"будильник запущен: {URL} каждые {INTERVAL} с")

    ok = fail = 0
    try:
        while True:
            success, ms, detail = ping()
            if success:
                ok += 1
                log(f"OK за {ms} мс | {detail} (успехов {ok}, сбоев {fail})")
            else:
                fail += 1
                log(f"СБОЙ {detail} (успехов {ok}, сбоев {fail}) — возможно, сервис просыпается")
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        log("будильник остановлен (Ctrl+C)")
        return 0
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
