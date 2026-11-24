"""SD-агент: опрос ServiceDesk с российского IP.

Зачем отдельный процесс. 24.11.2026 замерено: с адресов Render соединение
с ``sd.servionica.ru`` обрывается на этапе **connect** (пакеты не доходят),
а с российского IP тот же полный сброс фильтра занимает 3 секунды. Бот на
хостинге держит Telegram (long polling, команды), поэтому его не трогаем —
SD-опрос, уведомления и SLA берёт на себя этот агент.

Почему нет конфликта с ботом: агент **не вызывает getUpdates**. Оба процесса
используют один токен, но опрос обновлений делает только бот на хостинге,
агент лишь отправляет сообщения.

Запуск:   start_sd_agent.cmd   (или .venv\\Scripts\\python sd_agent.py)
Остановка: stop_sd_agent.cmd
Лог:      logs/sd_agent.log
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from config import load_config
from sd_client import SDClient
from sd_proxy import start_sd_proxy
from sd_settings import describe_missing
from sd_state import SDStateStore
from sd_watcher import SDWatcher
from telegram_net import build_session

log = logging.getLogger("sd_agent")


def install_stop_handlers(stop_event: asyncio.Event) -> None:
    """Аккуратное завершение по Ctrl+C и SIGTERM (как в bot.py)."""
    loop = asyncio.get_running_loop()

    def request_stop(*_args: object) -> None:
        if not stop_event.is_set():
            log.info("Получен сигнал остановки")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(request_stop))
            except (ValueError, OSError):
                log.debug("Не удалось повесить обработчик сигнала %s", sig)


async def run() -> int:
    """Держит SD-мониторинг живым, пока процесс не остановят."""
    cfg = load_config()

    if not cfg.sd_enabled:
        log.error(
            "SD_ENABLED=false в .env — агенту нечего делать. "
            "Мониторинг SD должен быть включён именно здесь, на ПК."
        )
        return 1

    problem = describe_missing(cfg)
    if problem:
        log.error(problem)
        return 1

    stop_event = asyncio.Event()
    install_stop_handlers(stop_event)

    proxy_runner = None
    session = build_session(cfg.proxy_url, cfg.telegram_api_ip)
    bot = Bot(
        token=cfg.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=None),
    )
    client = SDClient(cfg)
    store = SDStateStore(cfg.data_dir)
    watcher = SDWatcher(cfg, bot, store, client)

    try:
        # Локальный прокси снимает битый CSP-заголовок SD (см. sd_proxy.py)
        if cfg.sd_proxy_port:
            proxy_runner = await start_sd_proxy(
                cfg.sd_base_url,
                cfg.sd_proxy_port,
                verify_ssl=cfg.sd_verify_ssl,
            )

        log.info(
            "SD-агент запущен: интервал %s с, получатели %s",
            cfg.sd_check_interval,
            list(cfg.sd_notify_chat_ids or cfg.allowed_user_ids) or "нет",
        )
        await watcher.start()
        await stop_event.wait()
        log.info("Получен сигнал остановки — завершаюсь")
        return 0
    except Exception:
        log.exception("SD-агент завершился с ошибкой")
        return 1
    finally:
        await watcher.stop()
        await client.close()
        await bot.session.close()
        if proxy_runner is not None:
            await proxy_runner.cleanup()
        log.info("SD-агент остановлен")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    if not (Path(__file__).parent / ".env").exists():
        print("Нет файла .env рядом со скриптом — агент запускается из папки проекта")
        sys.exit(1)
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C")


if __name__ == "__main__":
    main()
