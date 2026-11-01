"""Уведомления о запуске и остановке бота.

Раньше NOTIFY_ON_START / NOTIFY_ON_STOP / NOTIFY_CHAT_IDS читались в настройки,
но нигде не использовались: включённое уведомление о перезапуске просто не
приходило. Для мониторинга это важно — по сообщению о старте видно, что бот
поднялся после сбоя.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from config import Config

log = logging.getLogger("notify")

START_TEXT = "🟢 <b>Бот запущен</b>"
STOP_TEXT = "🔴 <b>Бот остановлен</b>"


def targets(cfg: Config) -> list[int]:
    """Куда слать служебные сообщения: NOTIFY_CHAT_IDS, иначе ALLOWED_USER_IDS."""
    return list(cfg.notify_chat_ids or cfg.allowed_user_ids)


def _text(kind: str, cfg: Config, monitoring: str | None = None) -> str:
    if kind == "start":
        if monitoring is not None:
            state = monitoring
        else:
            state = "включён" if cfg.sd_enabled else "выключен"
        return f"{START_TEXT}\nSD-мониторинг: {state}, интервал {cfg.sd_check_interval} с"
    return STOP_TEXT


async def notify_lifecycle(
    bot: Any,
    cfg: Config,
    kind: str,
    monitoring: str | None = None,
) -> int:
    """Отправляет сообщение о запуске/остановке. Возвращает число доставок.

    `monitoring` подменяет строку про мониторинг: настройки могут быть заданы,
    но мониторинг не запуститься из-за отсутствия обязательных параметров.

    Никогда не бросает исключений: сбой уведомления не должен мешать работе
    или корректному завершению бота.
    """
    enabled = cfg.notify_on_start if kind == "start" else cfg.notify_on_stop
    if not enabled:
        return 0

    chat_ids = targets(cfg)
    if not chat_ids:
        log.info("Уведомление о %s пропущено: нет получателей", kind)
        return 0

    text = _text(kind, cfg, monitoring)
    sent = 0
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
            sent += 1
        except TelegramUnauthorizedError:
            log.error("Бот не авторизован в Telegram — уведомление не отправлено")
            break
        except TelegramNetworkError as exc:
            log.warning("Сеть Telegram недоступна для chat_id=%s: %s", chat_id, exc)
        except Exception:  # noqa: BLE001 — уведомление не должно ронять бота
            log.exception("Не удалось отправить уведомление в chat_id=%s", chat_id)

    log.info("Уведомление о %s доставлено в %d чат(ов)", kind, sent)
    return sent


# --- отчёт о падении и восстановлении --------------------------------------


def _fmt_ts(ts: float) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "—"


def format_duration(seconds: float) -> str:
    """«45 с», «27 мин», «1 ч 5 мин» — без секунд, когда их уже неважно."""
    total = int(max(0, seconds))
    if total < 60:
        return f"{total} с"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if minutes == 0:
        return f"{hours} ч"
    return f"{hours} ч {minutes} мин"


def format_outage(event: dict, delivered_at: float | None = None) -> str:
    """Текст сообщения: когда бот упал, когда поднялся, сколько не работал."""
    down_since = float(event.get("down_since") or 0)
    up_at = float(event.get("up_at") or 0)

    lines = [
        "⚠️ <b>Бот прерывался</b>",
        f"🔴 Не работал с: {_fmt_ts(down_since)}",
        f"🟢 Восстановлен: {_fmt_ts(up_at)}",
    ]
    if down_since and up_at:
        lines.append(f"⏱ Простой: {format_duration(up_at - down_since)}")

    if delivered_at and up_at and delivered_at - up_at > 120:
        lines.append(
            f"📨 Отправлено с задержкой {format_duration(delivered_at - up_at)}: "
            f"связи не было, сообщение ждало в очереди ({_fmt_ts(delivered_at)})"
        )
    return "\n".join(lines)


async def send_event(bot: Any, cfg: Config, event: dict) -> bool:
    """Пытается отправить событие о падении. True — если доставлено хотя бы раз."""
    chat_ids = targets(cfg)
    if not chat_ids:
        log.info("Событие о падении некуда отправлять: нет получателей")
        return False

    text = format_outage(event, delivered_at=time.time())
    delivered = False
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
            delivered = True
            log.info("Сообщение о падении отправлено в chat_id=%s", chat_id)
        except TelegramUnauthorizedError:
            log.error("Бот не авторизован — сообщение о падении не отправлено")
            break
        except TelegramNetworkError as exc:
            log.warning("Сеть Telegram недоступна (%s) — сообщение ждёт в очереди", exc)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить сообщение о падении в chat_id=%s", chat_id)
    return delivered


async def flush_pending(bot: Any, cfg: Config, store: Any) -> int:
    """Доотправляет накопленные события. Возвращает число доставленных."""
    events = store.pending()
    if not events:
        return 0

    remaining: list[dict] = []
    delivered = 0
    for event in events:
        ok = await send_event(bot, cfg, event)
        if ok:
            delivered += 1
            continue
        event["attempts"] = int(event.get("attempts", 0)) + 1
        remaining.append(event)

    store.save_pending(remaining)
    if delivered:
        log.info("Доотправлено отложенных событий: %d, осталось %d", delivered, len(remaining))
    return delivered
