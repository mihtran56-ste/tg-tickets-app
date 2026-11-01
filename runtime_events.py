"""Учёт «жизни» бота: падения, восстановления и недоставленные сообщения.

Процесс, который упал, сам написать не может. Поэтому бот оставляет след
жизни (`runtime.json`), а при следующем старте видит, что след устарел,
и сообщает: с какого момента его не было и когда он поднялся.

Если в этот момент нет связи с Telegram, событие попадает в очередь
(`pending_events.json`) и доотправляется при первой возможности — уже
с пометкой о задержке.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("runtime")

RUNTIME_FILE = "runtime.json"
PENDING_FILE = "pending_events.json"

# Не считаем падением перезапуск, который уложился в эти секунды.
# Значение маленькое: пользователь хочет знать о любом отключении, а от спама
# защищает «кулдаун» между сообщениями.
DEFAULT_MIN_OUTAGE_SEC = 10
# Не чаще одного сообщения о падении за этот интервал
DEFAULT_COOLDOWN_SEC = 600


@dataclass
class RuntimeMark:
    """Отметка о состоянии процесса."""

    last_alive: float = 0.0
    started_at: float = 0.0
    clean_stop: bool = False
    last_outage_notified_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_alive": self.last_alive,
            "started_at": self.started_at,
            "clean_stop": self.clean_stop,
            "last_outage_notified_at": self.last_outage_notified_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeMark":
        if not isinstance(data, dict):
            raise TypeError("отметка о жизни должна быть JSON-объектом")
        last_alive = data.get("last_alive", 0.0)
        started_at = data.get("started_at", 0.0)
        notified = data.get("last_outage_notified_at", 0.0)
        for name, value in (
            ("last_alive", last_alive),
            ("started_at", started_at),
            ("last_outage_notified_at", notified),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"поле {name} должно быть числом")
        return cls(
            last_alive=float(last_alive),
            started_at=float(started_at),
            clean_stop=bool(data.get("clean_stop", False)),
            last_outage_notified_at=float(notified),
        )


def detect_outage(
    previous: RuntimeMark | None,
    now: float,
    min_outage_sec: float = DEFAULT_MIN_OUTAGE_SEC,
) -> dict[str, Any] | None:
    """Определяет, было ли падение между прошлым запуском и текущим.

    Возвращает событие {"down_since": ..., "up_at": ...} или None, если
    перезапуск был штатным либо слишком коротким.
    """
    if previous is None or previous.last_alive <= 0:
        return None  # первый запуск
    if previous.clean_stop:
        return None  # остановили намеренно

    down_seconds = now - previous.last_alive
    if down_seconds < min_outage_sec:
        return None  # перезапуск мгновенный, шум не нужен

    return {"down_since": previous.last_alive, "up_at": now}


def outage_notify_allowed(
    last_notified_at: float,
    now: float,
    cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
) -> bool:
    """Не спамим сообщениями о падении: не чаще одного раза за cooldown."""
    if last_notified_at <= 0:
        return True
    return now - last_notified_at >= cooldown_sec


class RuntimeStore:
    """Хранит отметку о жизни и очередь недоставленных событий."""

    def __init__(self, data_dir: Path, min_outage_sec: int = DEFAULT_MIN_OUTAGE_SEC) -> None:
        self.path = Path(data_dir) / RUNTIME_FILE
        self.pending_path = Path(data_dir) / PENDING_FILE
        self.min_outage_sec = min_outage_sec
        self._mark: RuntimeMark | None = None

    # --- отметка о жизни -------------------------------------------------

    def load(self) -> RuntimeMark | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._mark = RuntimeMark.from_dict(data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self._preserve_corrupt(exc)
            self._mark = None
        return self._mark

    def _preserve_corrupt(self, reason: object) -> None:
        backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
        try:
            self.path.replace(backup)
            log.warning("Файл отметки о жизни повреждён (%s). Копия: %s", reason, backup.name)
        except OSError:
            log.exception("Не удалось сохранить повреждённую отметку о жизни")

    def _save(self) -> None:
        if self._mark is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._mark.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError:
            log.exception("Не удалось записать отметку о жизни в %s", self.path)

    def start(self, last_outage_notified_at: float = 0.0) -> None:
        now = time.time()
        self._mark = RuntimeMark(
            last_alive=now,
            started_at=now,
            clean_stop=False,
            last_outage_notified_at=last_outage_notified_at,
        )
        self._save()

    def mark_outage_notified(self, when: float | None = None) -> None:
        """Запоминает, что о падении уже сообщили (для кулдауна)."""
        if self._mark is None:
            self._mark = RuntimeMark()
        self._mark.last_outage_notified_at = when if when is not None else time.time()
        self._save()

    def beat(self) -> None:
        if self._mark is None:
            self._mark = RuntimeMark(last_alive=time.time(), started_at=time.time())
        self._mark.last_alive = time.time()
        self._mark.clean_stop = False
        self._save()

    def stop(self) -> None:
        if self._mark is None:
            self._mark = RuntimeMark()
        self._mark.last_alive = time.time()
        self._mark.clean_stop = True
        self._save()

    # --- очередь недоставленных событий ---------------------------------

    def pending(self) -> list[dict[str, Any]]:
        if not self.pending_path.exists():
            return []
        try:
            data = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("Очередь недоставленных событий не читается — начинаем с пустой")
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def save_pending(self, events: list[dict[str, Any]]) -> None:
        try:
            self.pending_path.parent.mkdir(parents=True, exist_ok=True)
            if not events:
                self.pending_path.unlink(missing_ok=True)
                return
            self.pending_path.write_text(
                json.dumps(events, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            log.exception("Не удалось сохранить очередь событий")

    def add_pending(self, event: dict[str, Any]) -> None:
        events = self.pending()
        event = dict(event)
        event["queued_at"] = event.get("queued_at") or time.time()
        events.append(event)
        self.save_pending(events)
        log.info("Событие %s поставлено в очередь на доотправку", event.get("kind"))
