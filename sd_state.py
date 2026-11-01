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

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_check": self.last_check,
            "seen_numbers": self.seen_numbers,
            "seen_sys_ids": self.seen_sys_ids,
            "seen_labels": self.seen_labels,
            "total_notifications_sent": self.total_notifications_sent,
            "last_error": self.last_error,
            "last_error_time": self.last_error_time,
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

        return cls(
            last_check=float(data.get("last_check", 0.0)),
            seen_numbers=[str(item) for item in data.get("seen_numbers", [])],
            seen_sys_ids=[str(item) for item in data.get("seen_sys_ids", [])],
            seen_labels={str(key): str(value) for key, value in labels.items()},
            total_notifications_sent=int(total_sent),
            last_error=str(data.get("last_error") or ""),
            last_error_time=float(data.get("last_error_time", 0.0)),
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
        }