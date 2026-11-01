"""Инструменты для работы с заявками через LLM."""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tickets import Ticket, TicketCategory, TicketPriority, TicketStatus, TicketStore

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger("tickets")


def _esc(value: object) -> str:
    """Экранирует значение для HTML-разметки Telegram.

    Ответы бота отправляются с parse_mode=HTML, а заголовки и описания заявок
    пишут пользователи: символы «<», «&» без экранирования ломают сообщение.
    """
    return html.escape(str(value)) if value is not None else ""

# Подписи для пользователя (в заявке хранятся английские значения enum)
PRIORITY_RU = {
    "low": "низкий",
    "medium": "обычный",
    "high": "высокий",
    "critical": "критический",
}

CATEGORY_RU = {
    "technical": "техническая",
    "billing": "оплата",
    "general": "общая",
    "feature_request": "новая функция",
    "bug_report": "ошибка",
    "other": "другое",
}

# Определения инструментов для LLM
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "Создать новую заявку. Используй, когда пользователь описывает проблему или просит помощь.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Краткий заголовок заявки (до 80 символов)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Подробное описание проблемы или запроса",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                        "description": "Приоритет: low — низкий, medium — обычный, high — высокий, critical — критический",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["technical", "billing", "general", "feature_request", "bug_report", "other"],
                        "description": "Категория заявки",
                    },
                },
                "required": ["title", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticket",
            "description": "Получить информацию о заявке по ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "ID заявки (8 символов)",
                    },
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_user_tickets",
            "description": "Показать список заявок пользователя.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_closed": {
                        "type": "boolean",
                        "description": "Показывать закрытые заявки",
                        "default": False,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_ticket_message",
            "description": "Добавить сообщение в существующую заявку (комментарий, уточнение).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "ID заявки",
                    },
                    "text": {
                        "type": "string",
                        "description": "Текст сообщения",
                    },
                },
                "required": ["ticket_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_ticket",
            "description": "Закрыть заявку. Используй когда проблема решена.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "ID заявки",
                    },
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reopen_ticket",
            "description": "Открыть закрытую заявку снова.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "ID заявки",
                    },
                },
                "required": ["ticket_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "change_ticket_priority",
            "description": "Изменить приоритет заявки.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "ID заявки",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                        "description": "Новый приоритет",
                    },
                },
                "required": ["ticket_id", "priority"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "change_ticket_category",
            "description": "Изменить категорию заявки.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "ID заявки",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["technical", "billing", "general", "feature_request", "bug_report", "other"],
                        "description": "Новая категория",
                    },
                },
                "required": ["ticket_id", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_ticket",
            "description": "Назначить заявку на администратора (только для админов).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "ID заявки",
                    },
                    "assignee_id": {
                        "type": "integer",
                        "description": "Telegram ID администратора",
                    },
                },
                "required": ["ticket_id", "assignee_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tickets",
            "description": (
                "Найти заявки по слову в заголовке или описании, либо по ID. "
                "Используй, когда пользователь спрашивает про конкретную проблему или заявку."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Слово, фраза или ID заявки",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


class Toolbox:
    """Набор инструментов для работы с заявками."""

    def __init__(
        self,
        tickets: TicketStore,
        chat_id: int,
        user_id: int,
        is_admin: bool = False,
    ) -> None:
        self._tickets = tickets
        self._chat_id = chat_id
        self._user_id = user_id
        self._is_admin = is_admin

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Выполняет инструмент по имени с аргументами."""
        if name == "create_ticket":
            return await self._create_ticket(arguments)
        if name == "get_ticket":
            return await self._get_ticket(arguments)
        if name == "list_user_tickets":
            return await self._list_user_tickets(arguments)
        if name == "add_ticket_message":
            return await self._add_ticket_message(arguments)
        if name == "close_ticket":
            return await self._close_ticket(arguments)
        if name == "reopen_ticket":
            return await self._reopen_ticket(arguments)
        if name == "change_ticket_priority":
            return await self._change_ticket_priority(arguments)
        if name == "change_ticket_category":
            return await self._change_ticket_category(arguments)
        if name == "assign_ticket":
            return await self._assign_ticket(arguments)
        if name == "search_tickets":
            return await self._search_tickets(arguments)
        return f"Неизвестный инструмент: {name}"

    async def _create_ticket(self, args: dict[str, Any]) -> str:
        title = args.get("title", "").strip()
        description = args.get("description", "").strip()
        priority_str = args.get("priority", "medium")
        category_str = args.get("category", "general")

        if not title or not description:
            return "Ошибка: заголовок и описание обязательны."

        try:
            priority = TicketPriority(priority_str)
        except ValueError:
            priority = TicketPriority.MEDIUM

        try:
            category = TicketCategory(category_str)
        except ValueError:
            category = TicketCategory.GENERAL

        ticket = self._tickets.create(
            user_id=self._user_id,
            chat_id=self._chat_id,
            title=title,
            description=description,
            priority=priority,
            category=category,
        )
        return (
            f"✅ Заявка создана!\n"
            f"📋 ID: {ticket.id}\n"
            f"📌 Заголовок: {_esc(ticket.title)}\n"
            f"📂 Категория: {CATEGORY_RU.get(ticket.category.value, ticket.category.value)}\n"
            f"⚡ Приоритет: {PRIORITY_RU.get(ticket.priority.value, ticket.priority.value)}"
        )

    async def _get_ticket(self, args: dict[str, Any]) -> str:
        ticket_id = _esc(args.get("ticket_id", "")).strip()
        if not ticket_id:
            return "Ошибка: укажи ID заявки."

        ticket = self._tickets.get(ticket_id)
        if not ticket:
            return f"Заявка {ticket_id} не найдена."

        # Проверка прав доступа
        if not self._is_admin and ticket.user_id != self._user_id:
            return "У тебя нет доступа к этой заявке."

        return self._format_ticket(ticket)

    async def _list_user_tickets(self, args: dict[str, Any]) -> str:
        include_closed = args.get("include_closed", False)

        if self._is_admin:
            tickets = self._tickets.list_all(include_closed=include_closed)
        else:
            tickets = self._tickets.list_for_user(self._user_id, include_closed=include_closed)

        if not tickets:
            return "Заявок пока нет."

        lines = ["📋 Все заявки:" if self._is_admin else "📋 Твои заявки:"]
        for t in tickets[:15]:
            status_emoji = {
                TicketStatus.OPEN: "🟢",
                TicketStatus.IN_PROGRESS: "🟡",
                TicketStatus.WAITING_USER: "🟠",
                TicketStatus.CLOSED: "✅",
                TicketStatus.CANCELLED: "❌",
            }.get(t.status, "❓")

            priority_emoji = {
                TicketPriority.LOW: "🔵",
                TicketPriority.MEDIUM: "🟡",
                TicketPriority.HIGH: "🟠",
                TicketPriority.CRITICAL: "🔴",
            }.get(t.priority, "⚪")

            assignee = f" → @{t.assignee_id}" if t.assignee_id else ""
            lines.append(
                f"{status_emoji} {priority_emoji} #{t.id} | {_esc(t.title[:40])} | {t.category.value}{assignee}"
            )

        if len(tickets) > 15:
            lines.append(f"... и ещё {len(tickets) - 15} заявок")

        return "\n".join(lines)

    async def _add_ticket_message(self, args: dict[str, Any]) -> str:
        ticket_id = _esc(args.get("ticket_id", "")).strip()
        text = args.get("text", "").strip()

        if not ticket_id or not text:
            return "Ошибка: укажи ID заявки и текст сообщения."

        ticket = self._tickets.get(ticket_id)
        if not ticket:
            return f"Заявка {ticket_id} не найдена."

        if not self._is_admin and ticket.user_id != self._user_id:
            return "У тебя нет доступа к этой заявке."

        if ticket.status == TicketStatus.CLOSED:
            return "Нельзя писать в закрытую заявку. Сначала открой её снова (/reopen)."

        ticket.add_message(self._user_id, text, is_from_staff=self._is_admin)
        self._tickets.update(ticket)
        return f"💬 Сообщение добавлено в заявку #{ticket_id}."

    async def _close_ticket(self, args: dict[str, Any]) -> str:
        ticket_id = _esc(args.get("ticket_id", "")).strip()
        if not ticket_id:
            return "Ошибка: укажи ID заявки."

        ticket = self._tickets.get(ticket_id)
        if not ticket:
            return f"Заявка {ticket_id} не найдена."

        if not self._is_admin and ticket.user_id != self._user_id:
            return "Только автор или админ могут закрыть заявку."

        ticket.close()
        self._tickets.update(ticket)
        return f"✅ Заявка #{ticket_id} закрыта."

    async def _reopen_ticket(self, args: dict[str, Any]) -> str:
        ticket_id = _esc(args.get("ticket_id", "")).strip()
        if not ticket_id:
            return "Ошибка: укажи ID заявки."

        ticket = self._tickets.get(ticket_id)
        if not ticket:
            return f"Заявка {ticket_id} не найдена."

        if not self._is_admin and ticket.user_id != self._user_id:
            return "Только автор или админ могут открыть заявку снова."

        ticket.reopen()
        self._tickets.update(ticket)
        return f"🔄 Заявка #{ticket_id} открыта снова."

    async def _change_ticket_priority(self, args: dict[str, Any]) -> str:
        ticket_id = _esc(args.get("ticket_id", "")).strip()
        priority_str = args.get("priority", "").strip()

        if not ticket_id or not priority_str:
            return "Ошибка: укажи ID заявки и приоритет."

        if not self._is_admin:
            return "Только администраторы могут менять приоритет."

        try:
            priority = TicketPriority(priority_str)
        except ValueError:
            return f"Неверный приоритет. Допустимые: low, medium, high, critical."

        if self._tickets.change_priority(ticket_id, priority):
            return f"⚡ Приоритет заявки #{ticket_id} изменён на {priority.value}."
        return f"Заявка {ticket_id} не найдена."

    async def _change_ticket_category(self, args: dict[str, Any]) -> str:
        ticket_id = _esc(args.get("ticket_id", "")).strip()
        category_str = args.get("category", "").strip()

        if not ticket_id or not category_str:
            return "Ошибка: укажи ID заявки и категорию."

        if not self._is_admin:
            return "Только администраторы могут менять категорию."

        try:
            category = TicketCategory(category_str)
        except ValueError:
            return f"Неверная категория. Допустимые: technical, billing, general, feature_request, bug_report, other."

        if self._tickets.change_category(ticket_id, category):
            return f"📂 Категория заявки #{ticket_id} изменена на {category.value}."
        return f"Заявка {ticket_id} не найдена."

    async def _assign_ticket(self, args: dict[str, Any]) -> str:
        ticket_id = _esc(args.get("ticket_id", "")).strip()
        assignee_id = args.get("assignee_id")

        if not ticket_id or assignee_id is None:
            return "Ошибка: укажи ID заявки и ID администратора."

        if not self._is_admin:
            return "Только администраторы могут назначать заявки."

        if self._tickets.assign(ticket_id, assignee_id):
            return f"👤 Заявка #{ticket_id} назначена на пользователя {assignee_id}."
        return f"Заявка {ticket_id} не найдена."

    async def _search_tickets(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "Ошибка: укажи слово или ID для поиска."

        found = self._tickets.search(query, limit=20)
        # Обычный пользователь ищет только среди своих заявок
        if not self._is_admin:
            found = [ticket for ticket in found if ticket.user_id == self._user_id]

        if not found:
            return f"🔍 По запросу «{_esc(query)}» ничего не найдено."

        status_labels = {
            TicketStatus.OPEN: "открыта",
            TicketStatus.IN_PROGRESS: "в работе",
            TicketStatus.WAITING_USER: "ждёт ответа",
            TicketStatus.CLOSED: "закрыта",
            TicketStatus.CANCELLED: "отменена",
        }

        lines = [f"🔍 Найдено по запросу «{_esc(query)}»: {len(found)}"]
        for ticket in found[:10]:
            lines.append(
                f"  #{ticket.id} | {_esc(ticket.title[:40])} | {status_labels.get(ticket.status, ticket.status.value)}"
            )
        if len(found) > 10:
            lines.append(f"  ... и ещё {len(found) - 10}")
        return "\n".join(lines)

    def _format_ticket(self, ticket: Ticket) -> str:
        status_emoji = {
            TicketStatus.OPEN: "🟢 Открыта",
            TicketStatus.IN_PROGRESS: "🟡 В работе",
            TicketStatus.WAITING_USER: "🟠 Ждёт пользователя",
            TicketStatus.CLOSED: "✅ Закрыта",
            TicketStatus.CANCELLED: "❌ Отменена",
        }.get(ticket.status, ticket.status.value)

        priority_emoji = {
            TicketPriority.LOW: "🔵 Низкий",
            TicketPriority.MEDIUM: "🟡 Обычный",
            TicketPriority.HIGH: "🟠 Высокий",
            TicketPriority.CRITICAL: "🔴 Критический",
        }.get(ticket.priority, ticket.priority.value)

        lines = [
            f"📋 Заявка #{ticket.id}",
            f"📌 {_esc(ticket.title)}",
            f"📂 Категория: {ticket.category.value}",
            f"⚡ Приоритет: {priority_emoji}",
            f"📊 Статус: {status_emoji}",
            f"👤 Автор: {ticket.user_id}",
        ]

        if ticket.assignee_id:
            lines.append(f"👨‍💻 Исполнитель: {ticket.assignee_id}")

        lines.append(f"🕐 Создана: {datetime.fromtimestamp(ticket.created_ts).strftime('%d.%m.%Y %H:%M')}")
        lines.append(f"🕐 Обновлена: {datetime.fromtimestamp(ticket.updated_ts).strftime('%d.%m.%Y %H:%M')}")

        if ticket.closed_ts:
            lines.append(f"🔒 Закрыта: {datetime.fromtimestamp(ticket.closed_ts).strftime('%d.%m.%Y %H:%M')}")

        lines.append(f"\n📝 Описание:\n{_esc(ticket.description)}")

        if ticket.messages:
            lines.append("\n💬 Сообщения:")
            for msg in ticket.messages[-5:]:
                role = "👨‍💻" if msg.is_from_staff else "👤"
                lines.append(f"  {role} [{msg.created_local()}] {_esc(msg.text[:100])}")
            if len(ticket.messages) > 5:
                lines.append(f"  ... и ещё {len(ticket.messages) - 5} сообщений")

        return "\n".join(lines)
