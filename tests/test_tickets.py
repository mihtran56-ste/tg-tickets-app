"""Проверка хранилища заявок: сохранение, восстановление, фильтры, поиск.

Запуск:  python tests/test_tickets.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tickets import (  # noqa: E402
    TicketCategory,
    TicketPriority,
    TicketStatus,
    TicketStore,
)


def fresh(tmp: Path) -> Path:
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    return tmp / "tickets.json"


def scenario_persistence(tmp: Path) -> None:
    """Заявка переживает перезапуск: файл читается новым хранилищем."""
    path = fresh(tmp)
    store = TicketStore(path)
    ticket = store.create(
        user_id=1,
        chat_id=10,
        title="Не работает принтер",
        description="Не печатает",
        priority=TicketPriority.HIGH,
        category=TicketCategory.TECHNICAL,
    )
    ticket.add_message(1, "Есть новости?", is_from_staff=False)
    store.update(ticket)

    reloaded = TicketStore(path)
    restored = reloaded.get(ticket.id)
    assert restored is not None, "заявка не восстановилась после перезапуска"
    assert restored.title == "Не работает принтер"
    assert restored.priority is TicketPriority.HIGH
    assert restored.category is TicketCategory.TECHNICAL
    assert len(restored.messages) == 1, "сообщения не сохранились"
    assert restored.messages[0].text == "Есть новости?"
    print("  OK: заявка с сообщением переживает перезапуск")

    assert not path.with_suffix(".tmp").exists(), "остался временный файл — запись не атомарна"
    print("  OK: временный файл после записи не остаётся")


def scenario_ids(tmp: Path) -> None:
    """Идентификаторы заявок уникальны."""
    path = fresh(tmp)
    store = TicketStore(path)
    ids = {store.create(user_id=1, chat_id=1, title=f"T{i}", description="d").id for i in range(200)}
    assert len(ids) == 200, f"дубли ID: {200 - len(ids)}"
    assert all(len(tid) == 8 for tid in ids), "ожидался ID из 8 символов"
    print("  OK: 200 заявок — все ID уникальны")


def scenario_status_changes(tmp: Path) -> None:
    """Закрытие проставляет дату, повторное открытие её сбрасывает."""
    path = fresh(tmp)
    store = TicketStore(path)
    ticket = store.create(user_id=1, chat_id=1, title="Статус", description="d")
    assert ticket.status is TicketStatus.OPEN
    assert ticket.closed_ts is None

    before = ticket.updated_ts
    time.sleep(0.01)
    ticket.close()
    assert ticket.status is TicketStatus.CLOSED
    assert ticket.closed_ts is not None, "дата закрытия не проставлена"
    assert ticket.updated_ts >= before, "время обновления не изменилось"
    print("  OK: закрытие проставляет дату и обновляет время")

    ticket.reopen()
    assert ticket.status is TicketStatus.OPEN
    assert ticket.closed_ts is None, "дата закрытия не сброшена"
    print("  OK: открытие снова сбрасывает дату закрытия")


def scenario_assign(tmp: Path) -> None:
    """Назначение исполнителя переводит заявку в работу."""
    path = fresh(tmp)
    store = TicketStore(path)
    ticket = store.create(user_id=1, chat_id=1, title="Назначить", description="d")

    assert store.assign(ticket.id, 3003) is True
    assert ticket.assignee_id == 3003
    assert ticket.status is TicketStatus.IN_PROGRESS, f"статус не сменился: {ticket.status}"
    print("  OK: назначение исполнителя переводит заявку в работу")

    assert store.assign("неттакой", 3003) is False, "несуществующая заявка должна вернуть False"
    print("  OK: назначение несуществующей заявки возвращает False")


def scenario_filters(tmp: Path) -> None:
    """Списки: свои/все, скрытие закрытых, сортировка по обновлению."""
    path = fresh(tmp)
    store = TicketStore(path)
    first = store.create(user_id=1, chat_id=1, title="Первая", description="d")
    second = store.create(user_id=2, chat_id=1, title="Вторая", description="d")
    time.sleep(0.01)
    store.update(first)  # первая стала свежее

    mine = store.list_for_user(1)
    assert [t.id for t in mine] == [first.id], f"чужая заявка в списке: {[t.id for t in mine]}"
    print("  OK: list_for_user отдаёт только свои заявки")

    everything = store.list_all()
    assert len(everything) == 2
    assert everything[0].id == first.id, "сортировка по времени обновления сломана"
    print("  OK: list_all сортирует по времени обновления (свежие первыми)")

    first.close()
    store.update(first)
    assert first.id not in [t.id for t in store.list_for_user(1)], "закрытая заявка не скрыта"
    assert first.id in [t.id for t in store.list_for_user(1, include_closed=True)], (
        "include_closed не показывает закрытую заявку"
    )
    print("  OK: закрытые заявки скрыты, но доступны с include_closed")

    by_status = store.list_by_status(TicketStatus.OPEN)
    assert [t.id for t in by_status] == [second.id], "list_by_status вернул не то"
    assigned = store.assign(second.id, 3003)
    assert assigned is True
    assert [t.id for t in store.list_assigned(3003)] == [second.id], "list_assigned вернул не то"
    print("  OK: list_by_status и list_assigned работают")


def scenario_search(tmp: Path) -> None:
    """Поиск по заголовку, описанию и ID."""
    path = fresh(tmp)
    store = TicketStore(path)
    printer = store.create(user_id=1, chat_id=1, title="Принтер не печатает", description="бумага застряла")
    other = store.create(user_id=1, chat_id=1, title="Нужен доступ", description="к папке отчётов")

    assert [t.id for t in store.search("принтер")] == [printer.id], "поиск по заголовку не работает"
    assert [t.id for t in store.search("бумага")] == [printer.id], "поиск по описанию не работает"
    assert [t.id for t in store.search(printer.id)] == [printer.id], "поиск по ID не работает"
    assert store.search("нет такого") == [], "поиск нашёл лишнее"
    assert len(store.search("", limit=1)) == 1, "лимит поиска не соблюдается"
    print("  OK: поиск по заголовку, описанию, ID и лимиту")


def scenario_corrupt_file(tmp: Path) -> None:
    """Повреждённый файл откладывается в сторону, а не затирается."""
    path = fresh(tmp)
    path.write_text("{это не json", encoding="utf-8")

    store = TicketStore(path)
    assert store.list_all() == [], "из повреждённого файла не должно быть заявок"

    backups = list(path.parent.glob("tickets.json.corrupt-*"))
    assert backups, "повреждённый файл не сохранён — данные потеряны безвозвратно"
    assert "{это не json" in backups[0].read_text(encoding="utf-8"), "копия не содержит исходных данных"
    print(f"  OK: повреждённый файл отложен как {backups[0].name}")

    store.create(user_id=1, chat_id=1, title="После сбоя", description="d")
    assert TicketStore(path).list_all(), "после сбоя запись не восстановилась"
    print("  OK: после повреждения хранилище продолжает работать")


def scenario_unwritable_disk(tmp: Path) -> None:
    """Недоступный диск не роняет создание заявки."""
    path = fresh(tmp)
    store = TicketStore(path)
    # Делаем целевой путь каталогом — замена файла на него должна провалиться
    store._path.unlink(missing_ok=True)
    store._path.mkdir()

    ticket = store.create(user_id=1, chat_id=1, title="Без диска", description="d")
    assert store.get(ticket.id) is not None, "заявка должна остаться в памяти"
    print("  OK: ошибка записи на диск не роняет создание заявки")


def main() -> None:
    base = ROOT / ".tmp-test-tickets"
    print("Проверка хранилища заявок")
    for title, scenario in (
        ("[перезапуск]", scenario_persistence),
        ("[уникальность ID]", scenario_ids),
        ("[статусы]", scenario_status_changes),
        ("[назначение]", scenario_assign),
        ("[фильтры]", scenario_filters),
        ("[поиск]", scenario_search),
        ("[повреждённый файл]", scenario_corrupt_file),
        ("[недоступный диск]", scenario_unwritable_disk),
    ):
        print(f"\n{title}")
        scenario(base / title.strip("[]").replace(" ", "_"))
    shutil.rmtree(base, ignore_errors=True)
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    main()
