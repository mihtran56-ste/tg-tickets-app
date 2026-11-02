"""Хранилище состояния SD-мониторинга.

Помнит, какие заявки уже уведомлены, чтобы не слать дубликаты.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Config

log = logging.getLogger("sd_state")

STATE_FILE = "sd_state.json"
MAX_SEEN = 2000  # максимум запомненных номеров заявок
MAX_SLA_EVENTS = 2000  # максимум заявок под SLA-наблюдением
SLA_MAX_AGE_SECONDS = 7 * 24 * 3600  # записи без обновлений старше недели не нужны


def _sla_number(entry: dict[str, Any], key: str, sys_id: object) -> float:
    """Число из записи SLA с понятной ошибкой вместо тихого ValueError."""
    value = entry.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"sla_events[{sys_id}][{key}] должно быть числом")
    return float(value)


@dataclass
class SDState:
    """Состояние мониторинга SD."""

    last_check: float = 0.0
    seen_numbers: list[str] = field(default_factory=list)
    seen_sys_ids: list[str] = field(default_factory=list)
    # REQ-номер -> рабочий код из темы (SUTSPROD-…) для /sd_last
    seen_labels: dict[str, str] = field(default_factory=dict)
    total_notifications_sent: int = 0
    last_error: str = ""
    last_error_time: float = 0.0
    # SLA: инициализированы ли наблюдения (нужно, чтобы при первом включении
    # не присылать пачку «сгоревших» заявок за всю историю).
    sla_initialized: bool = False
    # sys_id -> {"deadline": строка, "warn": ts, "expired": ts, "last_seen": ts}
    sla_events: dict[str, dict[str, Any]] = field(default_factory=dict)
    sla_warn_sent: int = 0
    sla_expired_sent: int = 0
    # Подпись условия фильтра: по ней видно, что фильтр в SD поменяли.
    filter_signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_check": self.last_check,
            "seen_numbers": self.seen_numbers,
            "seen_sys_ids": self.seen_sys_ids,
            "seen_labels": self.seen_labels,
            "total_notifications_sent": self.total_notifications_sent,
            "last_error": self.last_error,
            "last_error_time": self.last_error_time,
            "sla_initialized": self.sla_initialized,
            "sla_events": self.sla_events,
            "sla_warn_sent": self.sla_warn_sent,
            "sla_expired_sent": self.sla_expired_sent,
            "filter_signature": self.filter_signature,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SDState":
        """Собирает состояние из JSON, проверяя типы.

        Проверка обязательна: если в файле «seen_numbers» окажется строкой,
        mark_seen() упадёт на .append, и цикл мониторинга будет падать
        каждую минуту — уведомления просто перестанут приходить.
        """
        if not isinstance(data, dict):
            raise TypeError("состояние должно быть JSON-объектом")

        for key in ("seen_numbers", "seen_sys_ids"):
            value = data.get(key, [])
            if not isinstance(value, list):
                raise TypeError(f"поле {key} должно быть списком, а не {type(value).__name__}")

        labels = data.get("seen_labels", {})
        if not isinstance(labels, dict):
            raise TypeError("поле seen_labels должно быть объектом")

        for key in ("last_check", "last_error_time"):
            value = data.get(key, 0.0)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"поле {key} должно быть числом, а не {type(value).__name__}")

        total_sent = data.get("total_notifications_sent", 0)
        if not isinstance(total_sent, int) or isinstance(total_sent, bool):
            raise TypeError("поле total_notifications_sent должно быть целым числом")

        sla_initialized = data.get("sla_initialized", False)
        if not isinstance(sla_initialized, bool):
            raise TypeError("поле sla_initialized должно быть true или false")

        raw_events = data.get("sla_events", {})
        if not isinstance(raw_events, dict):
            raise TypeError("поле sla_events должно быть объектом")

        sla_events: dict[str, dict[str, Any]] = {}
        for key, entry in raw_events.items():
            if not isinstance(entry, dict):
                raise TypeError(f"sla_events[{key}] должно быть объектом")
            sla_events[str(key)] = {
                "deadline": str(entry.get("deadline") or ""),
                "warn": _sla_number(entry, "warn", key),
                "expired": _sla_number(entry, "expired", key),
                "last_seen": _sla_number(entry, "last_seen", key),
            }

        for name in ("sla_warn_sent", "sla_expired_sent"):
            value = data.get(name, 0)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"поле {name} должно быть целым числом")

        filter_signature = data.get("filter_signature", "")
        if not isinstance(filter_signature, str):
            raise TypeError("поле filter_signature должно быть строкой")

        return cls(
            last_check=float(data.get("last_check", 0.0)),
            seen_numbers=[str(item) for item in data.get("seen_numbers", [])],
            seen_sys_ids=[str(item) for item in data.get("seen_sys_ids", [])],
            seen_labels={str(key): str(value) for key, value in labels.items()},
            total_notifications_sent=int(total_sent),
            last_error=str(data.get("last_error") or ""),
            last_error_time=float(data.get("last_error_time", 0.0)),
            sla_initialized=sla_initialized,
            sla_events=sla_events,
            sla_warn_sent=int(data.get("sla_warn_sent", 0)),
            sla_expired_sent=int(data.get("sla_expired_sent", 0)),
            filter_signature=filter_signature,
        )

    def is_seen(self, number: str, sys_id: str) -> bool:
        """Проверяет, видела ли мы уже эту заявку."""
        return number in self.seen_numbers or sys_id in self.seen_sys_ids

    def mark_seen(self, number: str, sys_id: str, label: str = "") -> None:
        """Отмечает заявку как виданную, запоминая её рабочий код."""
        if number not in self.seen_numbers:
            self.seen_numbers.append(number)
        if sys_id not in self.seen_sys_ids:
            self.seen_sys_ids.append(sys_id)

        if label:
            # Храним только для заявок, которые помним, чтобы словарь не рос
            self.seen_labels[number] = label

        # Ограничиваем размер (LRU - удаляем самые старые)
        if len(self.seen_numbers) > MAX_SEEN:
            self.seen_numbers = self.seen_numbers[-MAX_SEEN:]
        if len(self.seen_sys_ids) > MAX_SEEN:
            self.seen_sys_ids = self.seen_sys_ids[-MAX_SEEN:]
        if len(self.seen_labels) > MAX_SEEN:
            keep = set(self.seen_numbers)
            self.seen_labels = {
                number: label for number, label in self.seen_labels.items() if number in keep
            }

    def label_for(self, number: str) -> str:
        """Рабочий код заявки (SUTSPROD-…), если он известен."""
        return self.seen_labels.get(number, "")

    # --- SLA -----------------------------------------------------------------

    def sync_sla(self, sys_id: str, deadline: str, now_ts: float) -> dict[str, Any]:
        """Обновляет запись SLA и сбрасывает флаги, если дедлайн перенесли."""
        entry = self.sla_events.get(sys_id)
        if entry is None:
            entry = {"deadline": deadline, "warn": 0.0, "expired": 0.0, "last_seen": 0.0}
            self.sla_events[sys_id] = entry
        elif entry.get("deadline") != deadline:
            entry["deadline"] = deadline
            entry["warn"] = 0.0
            entry["expired"] = 0.0
        entry["last_seen"] = now_ts
        return entry

    def mark_sla(self, sys_id: str, kind: str, deadline: str, ts: float) -> None:
        """Отмечает, что сообщение SLA по заявке уже отправлено."""
        entry = self.sla_events.get(sys_id)
        if entry is None:
            entry = {"deadline": deadline, "warn": 0.0, "expired": 0.0, "last_seen": ts}
            self.sla_events[sys_id] = entry
        if entry.get("deadline") != deadline:
            entry["deadline"] = deadline
        if kind == "warn":
            entry["warn"] = entry.get("warn") or ts
        else:
            entry["expired"] = entry.get("expired") or ts
            # Просрочка отменяет предупреждение: «осталось 2 ч» уже неактуально.
            entry["warn"] = entry.get("warn") or ts

    def record_sla_sent(self, kind: str) -> None:
        if kind == "warn":
            self.sla_warn_sent += 1
        else:
            self.sla_expired_sent += 1

    def prune_sla(
        self,
        now_ts: float,
        max_age: float = SLA_MAX_AGE_SECONDS,
        limit: int = MAX_SLA_EVENTS,
    ) -> None:
        """Убирает записи по заявкам, которых давно нет в фильтре."""
        self.sla_events = {
            sys_id: entry
            for sys_id, entry in self.sla_events.items()
            if now_ts - float(entry.get("last_seen") or 0.0) <= max_age
        }
        if len(self.sla_events) > limit:
            freshest = sorted(
                self.sla_events.items(),
                key=lambda item: float(item[1].get("last_seen") or 0.0),
                reverse=True,
            )[:limit]
            self.sla_events = dict(freshest)

    def record_notification_sent(self) -> None:
        self.total_notifications_sent += 1

    def record_error(self, error: str) -> None:
        self.last_error = error
        self.last_error_time = time.time()

    def clear_error(self) -> None:
        self.last_error = ""
        self.last_error_time = 0.0


class SDStateStore:
    """Хранилище состояния на диске."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / STATE_FILE
        self._state: SDState | None = None
        self._lock = False  # простая защита от рекурсивных сохранений

    def load(self) -> SDState:
        """Загружает состояние из файла."""
        if self._state is not None:
            return self._state

        if not self.path.exists():
            self._state = SDState()
            log.info("SD state: новый файл (первый запуск)")
            return self._state

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._state = SDState.from_dict(data)
            log.info(
                "SD state loaded: last_check=%.0f, seen=%d, sent=%d",
                self._state.last_check,
                len(self._state.seen_numbers),
                self._state.total_notifications_sent,
            )
        except (json.JSONDecodeError, OSError, TypeError, KeyError, AttributeError) as e:
            # Если просто начать с пустого состояния, следующее сохранение
            # затрёт файл — и бот разошлёт все старые заявки заново.
            self._preserve_corrupt(str(e))
            self._state = SDState()

        return self._state

    def _preserve_corrupt(self, reason: str) -> None:
        """Откладывает повреждённый файл состояния, чтобы не потерять его."""
        backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
        try:
            self.path.replace(backup)
            log.warning(
                "Файл состояния повреждён (%s). Копия сохранена как %s, "
                "начинаем с пустого состояния — возможна повторная рассылка.",
                reason,
                backup.name,
            )
        except OSError:
            log.exception("Файл состояния повреждён (%s), сохранить копию не удалось", reason)

    def save(self) -> None:
        """Сохраняет состояние в файл (атомарно через .tmp)."""
        if self._state is None or self._lock:
            return

        self._lock = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._state.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError:
            log.exception("Не удалось сохранить SD state")
        finally:
            self._lock = False

    @property
    def state(self) -> SDState:
        if self._state is None:
            return self.load()
        return self._state

    def mark_seen(self, number: str, sys_id: str, label: str = "") -> None:
        self.state.mark_seen(number, sys_id, label)
        self.save()

    def record_notification(self) -> None:
        self.state.record_notification_sent()
        self.save()

    def record_error(self, error: str) -> None:
        self.state.record_error(error)
        self.save()

    def clear_error(self) -> None:
        self.state.clear_error()
        self.save()

    def update_last_check(self) -> None:
        self.state.last_check = time.time()
        self.save()

    def get_stats(self) -> dict[str, Any]:
        s = self.state
        return {
            "last_check": s.last_check,
            "last_check_str": time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(s.last_check)) if s.last_check else "никогда",
            "seen_count": len(s.seen_numbers),
            "total_sent": s.total_notifications_sent,
            "last_error": s.last_error,
            "last_error_time": s.last_error_time,
            "sla_initialized": s.sla_initialized,
            "sla_tracked": len(s.sla_events),
            "sla_warn_sent": s.sla_warn_sent,
            "sla_expired_sent": s.sla_expired_sent,
        }