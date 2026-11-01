"""Контроль доступа к боту по списку разрешённых пользователей.

Раньше ALLOWED_USER_IDS действовал только в командах /sd_*: остальные
команды и диалог с ИИ отвечали любому, кто нашёл бота. Для корпоративного
бота это лишнее — заявки и их содержимое не должны быть доступны посторонним.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

log = logging.getLogger("access")

DENY_TEXT = (
    "⛔ Доступ ограничен.\n\n"
    "Отправь боту /id и попроси администратора добавить твой Telegram ID "
    "в ALLOWED_USER_IDS."
)

# Команды, которые должны работать у всех: без /id новый пользователь
# не узнает свой ID и не сможет попросить доступ.
ALWAYS_ALLOWED_COMMANDS = ("/id",)


def _command_of(message: Message) -> str:
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return ""
    # /id@MyBot -> /id
    return text.split()[0].split("@")[0].lower()


class AccessMiddleware(BaseMiddleware):
    """Пропускает только пользователей из ALLOWED_USER_IDS.

    Пустой список означает «бот открыт» — это режим отладки, о нём
    предупреждает сообщение при запуске.
    """

    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        self.allowed = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self.allowed:
            return await handler(event, data)

        user = getattr(event, "from_user", None)
        if user is not None and user.id in self.allowed:
            return await handler(event, data)

        # /id разрешён всем: иначе человек не узнает свой ID, чтобы попросить доступ
        if isinstance(event, Message) and _command_of(event) in ALWAYS_ALLOWED_COMMANDS:
            return await handler(event, data)

        if user is not None:
            log.warning(
                "Отказ в доступе: user_id=%s username=%s",
                user.id,
                getattr(user, "username", None),
            )

        # Отвечаем только там, где это возможно (в канале from_user нет)
        if isinstance(event, Message):
            try:
                await event.answer(DENY_TEXT)
            except Exception:  # noqa: BLE001 — отказ не должен валить обработку
                log.exception("Не удалось отправить отказ в доступе")
        return None
