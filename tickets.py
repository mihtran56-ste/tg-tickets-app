"""Модель и хранилище заявок (тикетов)."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger("tickets")


class TicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_USER = "waiting_user"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class TicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TicketCategory(str, Enum):
    TECHNICAL = "technical"
    BILLING = "billing"
    GENERAL = "general"
    FEATURE_REQUEST = "feature_request"
    BUG_REPORT = "bug_report"
    OTHER = "other"


@dataclass
class TicketMessage:
    id: str
    user_id: int
    text: str
    created_ts: float
    is_from_staff: bool = False

    def created_local(self) -> str:
        return datetime.fromtimestamp(self.created_ts).strftime("%d.%m.%Y %H:%M")


@dataclass
class Ticket:
    id: str
    user_id: int
    chat_id: int
    title: str
    description: str
    status: TicketStatus = TicketStatus.OPEN
    priority: TicketPriority = TicketPriority.MEDIUM
    category: TicketCategory = TicketCategory.GENERAL
    assignee_id: Optional[int] = None
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)
    closed_ts: Optional[float] = None
    messages: list[TicketMessage] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority.value,
            "category": self.category.value,
            "assignee_id": self.assignee_id,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "closed_ts": self.closed_ts,
            "messages": [asdict(m) for m in self.messages],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Ticket":
        messages = []
        for msg_data in data.get("messages") or []:
            if isinstance(msg_data, dict):
                messages.append(TicketMessage(**msg_data))
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            chat_id=data["chat_id"],
            title=data["title"],
            description=data["description"],
            status=TicketStatus(data.get("status", "open")),
            priority=TicketPriority(data.get("priority", "medium")),
            category=TicketCategory(data.get("category", "general")),
            assignee_id=data.get("assignee_id"),
            created_ts=data.get("created_ts", time.time()),
            updated_ts=data.get("updated_ts", time.time()),
            closed_ts=data.get("closed_ts"),
            messages=messages,
        )

    def add_message(self, user_id: int, text: str, is_from_staff: bool = False) -> TicketMessage:
        msg = TicketMessage(
            id=uuid.uuid4().hex[:8],
            user_id=user_id,
            text=text,
            created_ts=time.time(),
            is_from_staff=is_from_staff,
        )
        self.messages.append(msg)
        self.updated_ts = time.time()
        return msg

    def close(self) -> None:
        self.status = TicketStatus.CLOSED
        self.closed_ts = time.time()
        self.updated_ts = time.time()

    def reopen(self) -> None:
        self.status = TicketStatus.OPEN
        self.closed_ts = None
        self.updated_ts = time.time()


class TicketStore:
    """Хранилище заявок в data/tickets.json."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._items: list[Ticket] = []
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._preserve_corrupt(f"файл не читается: {exc}")
            return
        if not isinstance(raw, list):
            self._preserve_corrupt("ожидался список заявок")
            return
        items: list[Ticket] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                items.append(Ticket.from_dict(entry))
            except (TypeError, KeyError):
                continue
        self._items = items

    def _preserve_corrupt(self, reason: str) -> None:
        """Откладывает повреждённый файл, чтобы он не был затёрт новым сохранением."""
        backup = self._path.with_name(f"{self._path.name}.corrupt-{int(time.time())}")
        try:
            self._path.replace(backup)
            log.warning(
                "Файл заявок повреждён (%s). Копия сохранена как %s, начинаем с пустого списка.",
                reason,
                backup.name,
            )
        except OSError:
            log.exception("Файл заявок повреждён (%s), сохранить копию не удалось", reason)

    def _save(self) -> None:
        payload = [item.to_dict() for item in self._items]
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            # Диск может быть недоступен (read-only хостинг, закончилось место).
            # Заявка остаётся в памяти, но не переживёт перезапуск — пишем в лог.
            log.exception("Не удалось сохранить заявки в %s", self._path)

    def create(
        self,
        user_id: int,
        chat_id: int,
        title: str,
        description: str,
        priority: TicketPriority = TicketPriority.MEDIUM,
        category: TicketCategory = TicketCategory.GENERAL,
    ) -> Ticket:
        ticket = Ticket(
            id=uuid.uuid4().hex[:8],
            user_id=user_id,
            chat_id=chat_id,
            title=title,
            description=description,
            priority=priority,
            category=category,
        )
        self._items.append(ticket)
        self._save()
        return ticket

    def get(self, ticket_id: str) -> Optional[Ticket]:
        for ticket in self._items:
            if ticket.id == ticket_id:
                return ticket
        return None

    def list_for_user(self, user_id: int, include_closed: bool = False) -> list[Ticket]:
        result = [t for t in self._items if t.user_id == user_id]
        if not include_closed:
            result = [t for t in result if t.status != TicketStatus.CLOSED]
        return sorted(result, key=lambda t: t.updated_ts, reverse=True)

    def list_all(self, include_closed: bool = False) -> list[Ticket]:
        result = self._items
        if not include_closed:
            result = [t for t in result if t.status != TicketStatus.CLOSED]
        return sorted(result, key=lambda t: t.updated_ts, reverse=True)

    def list_by_status(self, status: TicketStatus) -> list[Ticket]:
        return sorted(
            [t for t in self._items if t.status == status],
            key=lambda t: t.updated_ts,
            reverse=True,
        )

    def list_assigned(self, assignee_id: int) -> list[Ticket]:
        return sorted(
            [t for t in self._items if t.assignee_id == assignee_id and t.status != TicketStatus.CLOSED],
            key=lambda t: t.updated_ts,
            reverse=True,
        )

    def update(self, ticket: Ticket) -> None:
        ticket.updated_ts = time.time()
        self._save()

    def assign(self, ticket_id: str, assignee_id: int) -> bool:
        ticket = self.get(ticket_id)
        if ticket:
            ticket.assignee_id = assignee_id
            ticket.status = TicketStatus.IN_PROGRESS
            self._save()
            return True
        return False

    def change_status(self, ticket_id: str, status: TicketStatus) -> bool:
        ticket = self.get(ticket_id)
        if ticket:
            ticket.status = status
            if status == TicketStatus.CLOSED:
                ticket.closed_ts = time.time()
            self._save()
            return True
        return False

    def change_priority(self, ticket_id: str, priority: TicketPriority) -> bool:
        ticket = self.get(ticket_id)
        if ticket:
            ticket.priority = priority
            self._save()
            return True
        return False

    def change_category(self, ticket_id: str, category: TicketCategory) -> bool:
        ticket = self.get(ticket_id)
        if ticket:
            ticket.category = category
            self._save()
            return True
        return False

    def search(self, query: str, limit: int = 10) -> list[Ticket]:
        query = query.lower()
        results = []
        for ticket in self._items:
            if (query in ticket.title.lower() or
                query in ticket.description.lower() or
                query in ticket.id.lower()):
                results.append(ticket)
                if len(results) >= limit:
                    break
        return results