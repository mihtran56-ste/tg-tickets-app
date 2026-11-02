"""Команды бота для управления SD-мониторингом."""

from __future__ import annotations

import html

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from config import Config
from sd_sla import now_in, today_blocks
from sd_watcher import SDWatcher


def _esc(value: object) -> str:
    """Экранирует текст для HTML-разметки Telegram.

    Статус собирается из данных SD и текстов ошибок — там бывают «<» и «&»,
    из-за которых Telegram отвергает сообщение целиком.
    """
    return html.escape(str(value)) if value is not None else ""


# Telegram: предел одного сообщения — 4096 символов. Берём с запасом,
# чтобы не ловить отказ на длинных списках заявок.
MESSAGE_LIMIT = 3500
MAX_DUMP = 200


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Режет длинный текст на части по строкам (HTML-теги внутри строк целы)."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for line in text.split("\n"):
        addition = len(line) + 1
        if current and size + addition > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += addition

    if current:
        chunks.append("\n".join(current))
    return chunks


def pack_blocks(blocks: list[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """Собирает блоки в сообщения, не разрывая блок (карточку заявки)."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for block in blocks:
        addition = len(block) + (2 if current else 0)  # разделитель "\n\n"
        if current and size + addition > limit:
            chunks.append("\n\n".join(current))
            current = []
            size = 0
            addition = len(block)
        current.append(block)
        size += addition

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def register_sd_commands(
    dp: Dispatcher,
    cfg: Config,
    watcher: SDWatcher | None,
) -> None:
    """Регистрирует обработчики команд SD.

    Регистрируются всегда, даже при выключенном мониторинге: иначе бот молчит
    в ответ на /sd_status, хотя пользователю нужно знать, что мониторинг выключен.
    Когда cfg.sd_enabled == False, каждый обработчик сразу отвечает об отключении
    и к watcher не обращается.
    """

    @dp.message(Command("sd_status"))
    async def cmd_sd_status(message: Message) -> None:
        """Статус мониторинга SD."""
        if not cfg.sd_enabled:
            await message.answer("⚠️ SD-мониторинг отключен (SD_ENABLED=false в .env)")
            return

        status = watcher.get_status()

        lines = [
            "📊 <b>Статус SD-мониторинга</b>",
            f"🔄 Работает: {'✅ да' if status['running'] else '❌ нет'}",
            f"⏸ Пауза: {'✅ да' if status['paused'] else '❌ нет'}",
            f"⏱ Интервал: {_esc(status['interval_sec'])} сек",
            f"🕒 Последняя проверка: {_esc(status['last_check'])}",
            f"👁 Видано заявок: {_esc(status['seen_count'])}",
        ]

        last = status.get("last_result") or {}
        if last.get("total"):
            lines.append(f"📊 Заявок по фильтру: {_esc(last['total'])}")

        lines.extend([
            f"📨 Отправлено уведомлений: {_esc(status['total_sent'])}",
            f"⚠️ Последняя ошибка: {_esc(status['last_error'])} ({_esc(status['last_error_time'])})",
        ])

        sla = status.get("sla") or {}
        if sla:
            warn_hours = f"{float(sla.get('warn_hours', 2)):g}".replace(".", ",")
            lines.append(
                f"⏰ SLA: {'✅ вкл' if sla.get('enabled') else '❌ выкл'}"
                f", порог {_esc(warn_hours)} ч"
            )
            lines.append(
                f"  Предупреждений: {_esc(sla.get('warn_sent', 0))}, "
                f"сгорело: {_esc(sla.get('expired_sent', 0))}"
            )

        if status["last_result"]:
            lr = status["last_result"]
            lines.append("")
            lines.append("🔍 <b>Последняя проверка:</b>")
            lines.append(f"  Время: {_esc(lr['checked_at'])}")
            lines.append(f"  Найдено новых: {_esc(lr['new_count'])}")
            lines.append(f"  Длительность: {_esc(lr['duration_ms'])} мс")
            if lr["errors"]:
                lines.append(f"  Ошибки: {_esc(', '.join(lr['errors']))}")

        await message.answer("\n".join(lines), parse_mode="HTML")

    @dp.message(Command("sd_test"))
    async def cmd_sd_test(message: Message) -> None:
        """Принудительная проверка SD прямо сейчас."""
        if not cfg.sd_enabled:
            await message.answer("⚠️ SD-мониторинг отключен")
            return

        # Проверяем права доступа (только админы/владельцы)
        if message.from_user and message.from_user.id not in cfg.allowed_user_ids:
            await message.answer("⛔ Нет прав для этой команды")
            return

        await message.answer("🔄 Проверяю SD...")
        try:
            result = await watcher.check_now()
            if result.new_requests:
                lines = [
                    f"✅ Проверка завершена за {result.duration_ms} мс",
                    f"🆕 Найдено новых заявок: {len(result.new_requests)}",
                    "",
                ]
                for req in result.new_requests[:5]:
                    lines.append(f"  • {req.number}: {req.short_description[:50]}")
                if len(result.new_requests) > 5:
                    lines.append(f"  ... и ещё {len(result.new_requests) - 5}")
            else:
                lines = [
                    f"✅ Проверка завершена за {result.duration_ms} мс",
                    "🆕 Новых заявок нет",
                ]
            if result.errors:
                lines.append(f"⚠️ Ошибки: {'; '.join(result.errors)}")
            total = getattr(result, "total_count", 0)
            if total:
                lines.append(f"📊 Всего по фильтру: {total}")
            if getattr(result, "filter_changed", False):
                lines.append("🔄 Фильтр изменился — срез запомнен без рассылки")
            sla_warned = getattr(result, "sla_warned", 0)
            sla_expired = getattr(result, "sla_expired", 0)
            if sla_warned or sla_expired:
                lines.append(
                    f"⏰ SLA: предупреждений {sla_warned}, сгорело {sla_expired}"
                )
            await message.answer("\n".join(lines))
        except Exception as e:
            await message.answer(f"❌ Ошибка: {type(e).__name__}: {e}")

    @dp.message(Command("sd_last"))
    async def cmd_sd_last(message: Message) -> None:
        """Показывает последние уведомлённые заявки."""
        if not cfg.sd_enabled:
            await message.answer("⚠️ SD-мониторинг отключен")
            return

        raw = (message.text or "").partition(" ")[2].strip()
        try:
            limit = int(raw) if raw else 10
            limit = min(max(1, limit), 50)
        except ValueError:
            limit = 10

        recent = watcher.get_recent_notifications(limit)
        if not recent:
            await message.answer("Пока нет уведомлённых заявок.")
            return

        lines = [f"📋 Последние {len(recent)} уведомлённых заявок:"]
        for i, num in enumerate(reversed(recent), 1):
            lines.append(f"  {i}. {num}")
        await message.answer("\n".join(lines))

    @dp.message(Command("sd_pause"))
    async def cmd_sd_pause(message: Message) -> None:
        """Приостанавливает мониторинг SD."""
        if not cfg.sd_enabled:
            await message.answer("⚠️ SD-мониторинг отключен")
            return

        if message.from_user and message.from_user.id not in cfg.allowed_user_ids:
            await message.answer("⛔ Нет прав для этой команды")
            return

        watcher.pause()
        await message.answer("⏸ SD-мониторинг приостановлен. Возобновить: /sd_resume")

    @dp.message(Command("sd_resume"))
    async def cmd_sd_resume(message: Message) -> None:
        """Возобновляет мониторинг SD."""
        if not cfg.sd_enabled:
            await message.answer("⚠️ SD-мониторинг отключен")
            return

        if message.from_user and message.from_user.id not in cfg.allowed_user_ids:
            await message.answer("⛔ Нет прав для этой команды")
            return

        watcher.resume()
        await message.answer("▶️ SD-мониторинг возобновлён.")

    @dp.message(Command("sd_count"))
    async def cmd_sd_count(message: Message) -> None:
        """Показывает текущее количество заявок по фильтру."""
        if not cfg.sd_enabled:
            await message.answer("⚠️ SD-мониторинг отключен")
            return

        if message.from_user and message.from_user.id not in cfg.allowed_user_ids:
            await message.answer("⛔ Нет прав для этой команды")
            return

        await message.answer("🔄 Проверяю количество...")
        try:
            # Переиспользуем сессию монитора: новый клиент означал бы лишний
            # логин в ServiceDesk на каждый вызов команды.
            client = getattr(watcher, "client", None)
            if client is not None:
                requests, total = await client.fetch_requests(
                    view=cfg.sd_view,
                    condition=cfg.sd_condition,
                    limit=1,
                )
            else:
                from sd_client import SDClient

                async with SDClient(cfg) as temporary:
                    requests, total = await temporary.fetch_requests(
                        view=cfg.sd_view,
                        condition=cfg.sd_condition,
                        limit=1,
                    )
            await message.answer(
                f"📊 <b>Заявок по фильтру: {total}</b>\n"
                f"(показано первых: {len(requests)})",
                parse_mode="HTML"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка: {type(e).__name__}: {e}")

    @dp.message(Command("sd_dump"))
    async def cmd_sd_dump(message: Message) -> None:
        """Присылает в чат ВСЕ заявки по фильтру одним списком (админы)."""
        if not cfg.sd_enabled:
            await message.answer("⚠️ SD-мониторинг отключен")
            return

        if message.from_user and message.from_user.id not in cfg.allowed_user_ids:
            await message.answer("⛔ Нет прав для этой команды")
            return

        await message.answer("🔄 Загружаю все заявки по фильтру...")

        client = getattr(watcher, "client", None)
        own_client = client is None
        try:
            if own_client:
                from sd_client import SDClient

                client = SDClient(cfg)
            items, total = await client.fetch_requests(
                view=cfg.sd_view,
                condition=cfg.sd_condition,
                limit=MAX_DUMP,
            )
        except Exception as e:
            await message.answer(f"❌ Не удалось получить заявки: {type(e).__name__}: {e}")
            return
        finally:
            if own_client and client is not None:
                await client.close()

        if not items:
            await message.answer("📭 По фильтру сейчас нет заявок.")
            return

        header = f"📋 <b>Все заявки по фильтру: {len(items)}</b>"
        if total and total > len(items):
            header += f" (всего по фильтру {total}, показаны первые {len(items)})"

        lines = [header, ""]
        lines.extend(item.format_short(index) for index, item in enumerate(items, 1))

        for chunk in split_message("\n".join(lines)):
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)

    @dp.message(Command("sd_today"))
    async def cmd_sd_today(message: Message) -> None:
        """Заявки, у которых дедлайн СУТС сегодня (в часовом поясе SD)."""
        if not cfg.sd_enabled:
            await message.answer("⚠️ SD-мониторинг отключен")
            return

        await message.answer("🔄 Смотрю дедлайны на сегодня...")

        # Переиспользуем сессию монитора, как /sd_count и /sd_dump: иначе
        # каждая команда делала бы лишний логин в ServiceDesk.
        client = getattr(watcher, "client", None)
        own_client = client is None
        try:
            if own_client:
                from sd_client import SDClient

                client = SDClient(cfg)
            items, _total = await client.fetch_requests(
                view=cfg.sd_view,
                condition=cfg.sd_condition,
                limit=MAX_DUMP,
            )
        except Exception as e:
            await message.answer(f"❌ Не удалось получить заявки: {type(e).__name__}: {e}")
            return
        finally:
            if own_client and client is not None:
                await client.close()

        moment = now_in(cfg.sd_timezone)
        blocks = today_blocks(items, moment, cfg.sd_timezone)
        for chunk in pack_blocks(blocks):
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)