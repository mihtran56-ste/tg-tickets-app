"""Проверка отчётов о падении/восстановлении и очереди недоставленных.

Сценарий пользователя: бот упал (или его перезапустил сторож), связи не было —
при восстановлении связи должно прийти сообщение, когда упал и когда поднялся.

Запуск:  python tests/test_runtime_events.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from notify import flush_pending, format_duration, format_outage, send_event  # noqa: E402
from runtime_events import (  # noqa: E402
    RuntimeMark,
    RuntimeStore,
    detect_outage,
    outage_notify_allowed,
)

ALLOWED = 1001


def fresh_dir(name: str) -> Path:
    path = ROOT / ".tmp-test-runtime" / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


def make_cfg(**overrides: object) -> Config:
    base: dict[str, object] = {
        "bot_token": "1:x",
        "allowed_user_ids": frozenset({ALLOWED}),
        "data_dir": ROOT,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: int, text: str, **_kwargs: object) -> None:
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.sent.append((chat_id, text))


def scenario_detect() -> None:
    now = time.time()

    assert detect_outage(None, now) is None, "первый запуск — не падение"
    assert detect_outage(RuntimeMark(last_alive=0.0), now) is None, "нет отметки — не падение"
    assert detect_outage(RuntimeMark(last_alive=now - 600, clean_stop=True), now) is None, (
        "штатная остановка не считается падением"
    )
    assert detect_outage(RuntimeMark(last_alive=now - 30), now, 60) is None, (
        "перезапуск за 30 с не должен считаться падением"
    )

    event = detect_outage(RuntimeMark(last_alive=now - 600), now, 60)
    assert event is not None, "падение на 10 минут не обнаружено"
    assert event["down_since"] == now - 600
    assert event["up_at"] == now
    print("  OK: падение отличается от штатной остановки и быстрого перезапуска")


def scenario_store() -> None:
    data_dir = fresh_dir("store")
    store = RuntimeStore(data_dir, 60)
    assert store.load() is None, "на пустом каталоге отметки быть не должно"

    store.start()
    mark = RuntimeStore(data_dir).load()
    assert mark is not None and mark.last_alive > 0, "отметка о старте не сохранилась"
    assert mark.clean_stop is False, "старт не должен помечаться как штатная остановка"

    time.sleep(0.01)
    store.beat()
    assert RuntimeStore(data_dir).load().last_alive > mark.last_alive, "сердцебиение не обновляет время"

    store.stop()
    stopped = RuntimeStore(data_dir).load()
    assert stopped.clean_stop is True, "штатная остановка не отмечена"
    print("  OK: старт, сердцебиение и остановка сохраняются на диск")

    # Повреждённый файл не должен затираться
    store.path.write_text("{битый json", encoding="utf-8")
    assert RuntimeStore(data_dir).load() is None, "из битого файла отметки быть не должно"
    backups = list(data_dir.glob("runtime.json.corrupt-*"))
    assert backups, "повреждённый файл отметки не сохранён"
    print("  OK: повреждённая отметка откладывается в сторону")


def scenario_pending() -> None:
    data_dir = fresh_dir("pending")
    store = RuntimeStore(data_dir, 60)
    assert store.pending() == [], "очередь должна быть пустой"

    store.add_pending({"kind": "outage", "down_since": 1.0, "up_at": 2.0})
    events = store.pending()
    assert len(events) == 1 and events[0]["kind"] == "outage"
    assert events[0]["queued_at"] > 0, "нет времени постановки в очередь"

    store.save_pending([])
    assert store.pending() == [], "очередь не очищается"
    assert not store.pending_path.exists(), "пустой файл очереди остался на диске"
    print("  OK: очередь событий сохраняется и очищается")


def scenario_text() -> None:
    assert format_duration(45) == "45 с"
    assert format_duration(120) == "2 мин"
    assert format_duration(3600) == "1 ч"
    assert format_duration(3900) == "1 ч 5 мин"
    print("  OK: длительность читается по-человечески")

    down = time.mktime((2026, 11, 1, 10, 15, 0, 0, 0, -1))
    up = down + 27 * 60
    text = format_outage({"down_since": down, "up_at": up})
    assert "Бот прерывался" in text
    assert "01.11.2026 10:15" in text, f"нет времени падения: {text}"
    assert "01.11.2026 10:42" in text, f"нет времени восстановления: {text}"
    assert "27 мин" in text, f"нет длительности: {text}"
    assert "задержкой" not in text, "без задержки не должно быть пометки"
    print("  OK: сообщение содержит время падения, восстановления и простой")

    delayed = format_outage({"down_since": down, "up_at": up}, delivered_at=up + 3600)
    assert "задержкой" in delayed, f"нет пометки о задержке: {delayed}"
    assert "связи не было" in delayed, f"нет объяснения задержки: {delayed}"
    print("  OK: при отправке позже появляется пометка про связь")


async def scenario_send_and_flush() -> None:
    data_dir = fresh_dir("flush")
    store = RuntimeStore(data_dir, 60)

    bot = FakeBot()
    event = {"kind": "outage", "down_since": time.time() - 600, "up_at": time.time()}
    assert await send_event(bot, make_cfg(), event) is True
    assert bot.sent and "Бот прерывался" in bot.sent[0][1]
    print("  OK: при доступной связи событие отправляется сразу")

    broken = FakeBot(fail=True)
    assert await send_event(broken, make_cfg(), event) is False, "сбой отправки не распознан"
    print("  OK: при недоступной связи отправка считается неудачной")

    no_recipients = FakeBot()
    assert await send_event(no_recipients, make_cfg(allowed_user_ids=frozenset()), event) is False
    print("  OK: без получателей отправка не выполняется")

    # Очередь: бот упал час назад, связи не было — событие ждёт и доотправляется
    old_event = {
        "kind": "outage",
        "down_since": time.time() - 4000,
        "up_at": time.time() - 3600,
    }
    store.add_pending(old_event)
    assert await flush_pending(broken, make_cfg(), store) == 0, "при сбое ничего не доставлено"
    remaining = store.pending()
    assert len(remaining) == 1, "событие потерялось из очереди"
    assert remaining[0]["attempts"] == 1, f"попытка не учтена: {remaining[0]}"

    good = FakeBot()
    assert await flush_pending(good, make_cfg(), store) == 1, "событие не доотправилось"
    assert store.pending() == [], "очередь не очистилась после успеха"
    assert "задержкой" in good.sent[0][1], f"при доотправке нет пометки о задержке: {good.sent[0][1]}"
    print("  OK: событие ждёт в очереди и доотправляется с пометкой о задержке")


def scenario_cooldown() -> None:
    """Падения в цикле не должны превращаться в спам сообщениями."""
    now = time.time()
    assert outage_notify_allowed(0, now, 600) is True, "первое падение сообщаем всегда"
    assert outage_notify_allowed(now - 60, now, 600) is False, "внутри кулдауна молчим"
    assert outage_notify_allowed(now - 700, now, 600) is True, "после кулдауна сообщаем снова"
    print("  OK: кулдаун не даёт спамить при падениях подряд")

    data_dir = fresh_dir("cooldown")
    store = RuntimeStore(data_dir, 10)
    store.start(last_outage_notified_at=12345.0)
    assert RuntimeStore(data_dir).load().last_outage_notified_at == 12345.0, (
        "время последнего уведомления не сохраняется при старте"
    )

    store.mark_outage_notified(99999.0)
    assert RuntimeStore(data_dir).load().last_outage_notified_at == 99999.0, (
        "отметка об отправленном уведомлении не сохраняется"
    )
    print("  OK: отметка об уведомлении переживает перезапуск")


async def main() -> None:
    base = ROOT / ".tmp-test-runtime"
    shutil.rmtree(base, ignore_errors=True)
    print("Проверка отчётов о падении и очереди событий")
    for title, scenario in (
        ("[определение падения]", scenario_detect),
        ("[кулдаун уведомлений]", scenario_cooldown),
        ("[отметка о жизни]", scenario_store),
        ("[очередь событий]", scenario_pending),
        ("[текст сообщения]", scenario_text),
        ("[отправка и доотправка]", scenario_send_and_flush),
    ):
        print(f"\n{title}")
        result = scenario()
        if asyncio.iscoroutine(result):
            await result
    shutil.rmtree(base, ignore_errors=True)
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
