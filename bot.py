"""Telegram-бот для работы с заявками (тикеты) + мониторинг ServiceDesk.

Запуск:
    python bot.py
"""

from __future__ import annotations

import asyncio
import html
import logging
import signal
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart

from access import AccessMiddleware
from config import Config, load_config
from health import start_health_server
from llm import LLMClient
from notify import flush_pending, format_duration, notify_lifecycle, send_event
from runtime_events import RuntimeStore, detect_outage, outage_notify_allowed
from tickets import TicketStore
from tools import TOOLS, Toolbox

# ServiceDesk интеграция
from sd_client import SDClient
from sd_settings import describe_missing
from sd_state import SDStateStore
from sd_watcher import SDWatcher
from sd_commands import register_sd_commands

log = logging.getLogger("tickets")


def install_stop_handlers(stop_event: asyncio.Event) -> None:
    """Ставит обработчики SIGTERM и SIGINT на аккуратное завершение.

    На хостинге SIGTERM приходит при передеплое или остановке сервиса. Без
    обработчика процесс умирает сразу: сессии не закрыты, состояние не дописано.
    """
    loop = asyncio.get_running_loop()

    def request_stop(*_args: object) -> None:
        if not stop_event.is_set():
            log.info("Получен сигнал остановки")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler не поддерживается
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(request_stop))
            except (ValueError, OSError):
                log.debug("Не удалось повесить обработчик сигнала %s", sig)

def build_help_text(llm_enabled: bool) -> str:
    """Справка. Первая строка честно говорит, доступен ли диалог с ИИ."""
    if llm_enabled:
        intro = "Просто напиши, в чём проблема — я создам заявку и буду вести диалог.\n\n"
    else:
        intro = (
            "Диалог с ИИ не подключён (нет LLM_API_KEY), поэтому создавать заявки\n"
            "текстом пока нельзя — используй команды ниже.\n\n"
        )

    return (
        "🎫 <b>Бот заявок (Ticket Bot)</b>\n\n"
        + intro
        + "🛠 <b>Что умею:</b>\n"
        "• Показывать твои заявки и их статус\n"
        "• Искать заявки по слову или номеру\n"
        "• Добавлять комментарии к заявкам\n"
        "• Закрывать/открывать заявки\n"
        "• Менять приоритет и категорию (админы)\n"
        "• Назначать заявки на исполнителей (админы)\n\n"
        "🤖 <b>SD-мониторинг:</b>\n"
        "Автоматически присылает уведомления о новых заявках из ServiceDesk.\n\n"
        "📋 <b>Команды:</b>\n"
        "/help — эта справка\n"
        "/my — мои заявки\n"
        "/all — все заявки (админы)\n"
        "/id — мой Telegram ID\n"
        "/sd_status — статус SD-мониторинга\n"
        "/sd_test — проверить SD прямо сейчас\n"
        "/sd_last N — последние N уведомлений из SD\n"
        "/sd_count — текущее количество заявок по фильтру\n"
        "/sd_pause / /sd_resume — пауза/возобновление SD"
    )


async def _runtime_loop(
    runtime: RuntimeStore,
    bot: Bot,
    cfg: Config,
    stop_event: asyncio.Event,
    beat_sec: int = 20,
    flush_sec: int = 60,
) -> None:
    """Отмечается «я жив» и доотправляет отложенные сообщения.

    Если связь пропала, сообщение о падении остаётся в очереди и уйдёт,
    как только Telegram снова ответит.
    """
    last_flush = time.time()
    while not stop_event.is_set():
        try:
            runtime.beat()
            if time.time() - last_flush >= flush_sec:
                await flush_pending(bot, cfg, runtime)
                last_flush = time.time()
        except Exception:  # noqa: BLE001 — служебный цикл не должен падать
            log.exception("Ошибка в цикле учёта жизни")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=beat_sec)
        except asyncio.TimeoutError:
            continue


def build_dispatcher(
    cfg: Config,
    ticket_store: TicketStore,
    sd_watcher: SDWatcher | None = None,
    llm_client: LLMClient | None = None,
) -> Dispatcher:
    """Создаёт Dispatcher со всеми обработчиками."""
    dp = Dispatcher()

    # Контроль доступа: только пользователи из ALLOWED_USER_IDS.
    # Пустой список — бот открыт (режим отладки, о нём предупреждаем при старте).
    dp.message.middleware(AccessMiddleware(cfg.allowed_user_ids))

    # SD-команды регистрируются всегда: при выключенном мониторинге они
    # отвечают «отключен», а не молчат (watcher при этом не используется).
    register_sd_commands(dp, cfg, sd_watcher)

    @dp.message(CommandStart())
    async def cmd_start(message):
        # Имя из профиля Telegram подставляется в HTML-текст: у пользователя
        # может стоять «<» или «&», без экранирования Telegram отвергнет сообщение.
        name = html.escape(message.from_user.first_name) if message.from_user else ""
        help_text = build_help_text(llm_client is not None)
        await message.answer(f"Привет, {name}! 👋\n\n{help_text}", parse_mode=ParseMode.HTML)

    @dp.message(Command("help"))
    async def cmd_help(message):
        await message.answer(build_help_text(llm_client is not None), parse_mode=ParseMode.HTML)

    @dp.message(Command("id"))
    async def cmd_id(message):
        if message.from_user:
            await message.answer(
                f"Твой Telegram ID: {message.from_user.id}\nChat ID: {message.chat.id}",
                parse_mode=ParseMode.HTML,
            )

    @dp.message(Command("my"))
    async def cmd_my(message):
        """Мои заявки."""
        if message.from_user is None:
            await message.answer("Эта команда работает только в личных сообщениях.")
            return
        user_id = message.from_user.id
        is_admin = user_id in cfg.admin_user_ids
        toolbox = Toolbox(ticket_store, message.chat.id, user_id, is_admin)
        result = await toolbox.execute("list_user_tickets", {"include_closed": False})
        await message.answer(result, parse_mode=ParseMode.HTML)

    @dp.message(Command("all"))
    async def cmd_all(message):
        """Все заявки (только для админов)."""
        if message.from_user is None:
            await message.answer("Эта команда работает только в личных сообщениях.")
            return
        user_id = message.from_user.id
        if user_id not in cfg.admin_user_ids:
            await message.answer("⛔ Только для администраторов.")
            return
        toolbox = Toolbox(ticket_store, message.chat.id, user_id, True)
        result = await toolbox.execute("list_user_tickets", {"include_closed": True})
        await message.answer(result, parse_mode=ParseMode.HTML)

    @dp.message()
    async def on_text(message):
        """Обработка обычных сообщений — диалог с LLM и создание заявок.

        Если LLM не настроен (нет LLM_API_KEY в .env), честно об этом сообщаем
        вместо падения обработчика.
        """
        if not message.text or message.text.startswith("/"):
            return

        user = message.from_user
        if user is None:
            return

        if llm_client is None:
            await message.answer(
                "🤖 Диалог с ИИ не настроен: добавь LLM_API_KEY в .env.\n\n"
                "Доступные команды:\n"
                "/help — справка\n"
                "/my — мои заявки\n"
                "/sd_status — статус SD-мониторинга\n"
                "/sd_count — сколько заявок по фильтру\n"
                "/sd_last N — последние N уведомлений",
                parse_mode=ParseMode.HTML,
            )
            return

        is_admin = user.id in cfg.admin_user_ids
        toolbox = Toolbox(ticket_store, message.chat.id, user.id, is_admin)
        history = [{"role": "user", "content": message.text}]

        try:
            answer = await llm_client.ask_with_tools(
                history,
                TOOLS,
                toolbox.execute,
                system_extra=(
                    f"Telegram ID пользователя: {user.id}. "
                    f"Админ: {'да' if is_admin else 'нет'}."
                ),
            )
        except Exception:
            log.exception("Ошибка LLM")
            await message.answer("😔 Не получилось обработать запрос. Попробуй ещё раз.")
            return

        if not answer:
            answer = "Не смог сформулировать ответ. Попробуй переформулировать."

        # Без parse_mode: модель может вернуть символы, ломающие HTML-разметку
        await message.answer(answer)

    return dp


async def run(cfg: Config) -> None:
    """Основной цикл бота."""
    # Событие остановки: SIGTERM (хостинг) или Ctrl+C (локально)
    stop_event = asyncio.Event()
    install_stop_handlers(stop_event)
    # Настраиваем сессию с прокси если указан
    from telegram_net import build_session
    session = build_session(cfg.proxy_url, cfg.telegram_api_ip)
    bot = Bot(
        token=cfg.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=None),
    )

    # Инициализация компонентов
    ticket_store = TicketStore(cfg.data_dir / "tickets.json")

    # Учёт падений: отметка о жизни + очередь недоставленных сообщений
    runtime = RuntimeStore(cfg.data_dir, cfg.outage_min_seconds)
    previous_mark = runtime.load()

    # LLM-диалог (не обязателен: без ключа бот работает как монитор заявок)
    llm_client: LLMClient | None = None
    if cfg.llm_api_key:
        llm_client = LLMClient.from_config(cfg)
        log.info("LLM: вкл, модель %s", cfg.llm_model)
    else:
        log.info("LLM: выкл (LLM_API_KEY не задан)")

    # SD-мониторинг
    sd_state_store = SDStateStore(cfg.data_dir)
    sd_client = SDClient(cfg)
    sd_watcher: SDWatcher | None = None
    monitoring_state = "выключен"

    if not cfg.sd_enabled:
        log.info("SD мониторинг: выкл (SD_ENABLED=false)")
    else:
        problem = describe_missing(cfg)
        if problem:
            # Без фильтра мониторинг покрыл бы весь ServiceDesk — не запускаем
            log.error(problem)
            monitoring_state = "НЕ запущен: неполные настройки"
        else:
            sd_watcher = SDWatcher(cfg, bot, sd_state_store, sd_client)
            monitoring_state = "включён"
            log.info(
                "SD мониторинг: вкл, интервал %s сек, view=%s",
                cfg.sd_check_interval,
                cfg.sd_view,
            )

    dp = build_dispatcher(cfg, ticket_store, sd_watcher, llm_client)

    # Запуск SD watcher
    if sd_watcher:
        await sd_watcher.start()

    # Мини-HTTP сервер для площадок, которым нужен «живой» порт (PORT=0 — выкл.)
    health_runner = None
    if cfg.health_port:
        health_runner = await start_health_server(
            cfg.health_port,
            status_provider=(lambda: sd_watcher.get_status()) if sd_watcher else None,
            token=cfg.health_token,
        )

    runtime_task: asyncio.Task | None = None

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Бот запущен.")

        if not cfg.allowed_user_ids:
            log.warning(
                "ALLOWED_USER_IDS пуст — бот отвечает всем. "
                "Отправь боту /id и добавь свой ID в .env."
            )

        # Фиксируем, что процесс жив, и проверяем, не упал ли он в прошлый раз
        last_notified = previous_mark.last_outage_notified_at if previous_mark else 0.0
        runtime.start(last_notified)
        outage = detect_outage(previous_mark, time.time(), cfg.outage_min_seconds)

        await notify_lifecycle(bot, cfg, "start", monitoring=monitoring_state)

        if outage:
            log.warning(
                "Обнаружено падение: простоял %s (с %s до %s)",
                format_duration(outage["up_at"] - outage["down_since"]),
                time.strftime("%d.%m.%Y %H:%M", time.localtime(outage["down_since"])),
                time.strftime("%d.%m.%Y %H:%M", time.localtime(outage["up_at"])),
            )
            if outage_notify_allowed(last_notified, time.time(), cfg.outage_cooldown_seconds):
                if await send_event(bot, cfg, outage):
                    runtime.mark_outage_notified()
                else:
                    # Связи не было — сообщим, когда появится
                    runtime.add_pending({"kind": "outage", **outage})
            else:
                log.warning(
                    "Уведомление о падении подавлено: предыдущее отправлено %s назад "
                    "(кулдаун %s с) — так падения в цикле не превращаются в спам",
                    format_duration(time.time() - last_notified),
                    cfg.outage_cooldown_seconds,
                )

        # Доотправляем то, что не удалось отправить раньше (например, без сети)
        await flush_pending(bot, cfg, runtime)

        runtime_task = asyncio.create_task(_runtime_loop(runtime, bot, cfg, stop_event))
        polling = asyncio.create_task(
            dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                handle_as_tasks=False,
            )
        )
        stopper = asyncio.create_task(stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {polling, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stopper.cancel()

        if polling in done and not polling.cancelled():
            # Поллинг завершился сам — пробрасываем его ошибку, если была
            polling.result()
        else:
            polling.cancel()
            try:
                await polling
            except asyncio.CancelledError:
                pass
            log.info("Получен сигнал остановки — завершаюсь аккуратно")
    except Exception:
        log.exception("Бот завершился с ошибкой")
    finally:
        if runtime_task is not None:
            runtime_task.cancel()
        # Отмечаем ШТАТНУЮ остановку: иначе следующий старт сочтёт её падением
        runtime.stop()
        await notify_lifecycle(bot, cfg, "stop")
        if health_runner:
            await health_runner.cleanup()
        if llm_client:
            await llm_client.close()
        if sd_watcher:
            await sd_watcher.stop()
            await sd_client.close()
        await bot.session.close()
        log.info("Бот остановлен.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )

    try:
        cfg = load_config()
    except ValueError as exc:
        log.error("Ошибка настройки:\n%s", exc)
        sys.exit(1)

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C")


if __name__ == "__main__":
    main()