"""Проверка инструментов, которые вызывает ИИ (Toolbox).

Запуск:  python tests/test_tools.py
Сети не требует, работает на временном файле заявок.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tickets import TicketStore  # noqa: E402
from tools import Toolbox  # noqa: E402

USER = 1001
OTHER = 2002
ADMIN = 3003
CHAT = 900


def fresh_store(tmp: Path) -> TicketStore:
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    return TicketStore(tmp / "tickets.json")


async def scenario_create(tmp: Path) -> str:
    store = fresh_store(tmp)
    box = Toolbox(store, CHAT, USER)

    result = await box.execute(
        "create_ticket",
        {
            "title": "Не работает принтер",
            "description": "Не печатает в бухгалтерии",
            "priority": "high",
            "category": "technical",
        },
    )

    assert "Заявка создана" in result, f"нет подтверждения: {result}"
    assert "высокий" in result.lower(), f"приоритет не отработан: {result}"
    assert len(store.list_all()) == 1, "заявка не сохранилась"
    ticket = store.list_all()[0]
    assert ticket.user_id == USER and ticket.chat_id == CHAT
    print("  OK: заявка создаётся, приоритет и категория применяются")

    empty = await box.execute("create_ticket", {"title": "", "description": ""})
    assert "обязательны" in empty, f"нет проверки пустых полей: {empty}"
    print("  OK: пустые заголовок/описание отклоняются")

    fallback = await box.execute(
        "create_ticket",
        {"title": "Проверка", "description": "Проверка", "priority": "несуществующий", "category": "чушь"},
    )
    assert "создана" in fallback.lower(), f"некорректные значения должны давать дефолт: {fallback}"
    assert "обычный" in fallback.lower() and "общая" in fallback.lower(), f"дефолты не применились: {fallback}"
    print("  OK: неизвестные приоритет/категория не роняют создание")
    return store.list_all()[0].id


async def scenario_access(tmp: Path, ticket_id: str) -> None:
    store = fresh_store(tmp)
    box = Toolbox(store, CHAT, USER)
    created = await box.execute("create_ticket", {"title": "Доступ", "description": "Проверка прав"})
    assert "создана" in created.lower()
    tid = store.list_all()[0].id

    own = await Toolbox(store, CHAT, USER).execute("get_ticket", {"ticket_id": tid})
    assert "Доступ" in own, f"автор должен видеть заявку: {own}"
    print("  OK: автор видит свою заявку")

    foreign = await Toolbox(store, CHAT, OTHER).execute("get_ticket", {"ticket_id": tid})
    assert "нет доступа" in foreign.lower(), f"чужой не должен видеть заявку: {foreign}"
    print("  OK: чужой пользователь получает отказ")

    admin = await Toolbox(store, CHAT, ADMIN, is_admin=True).execute("get_ticket", {"ticket_id": tid})
    assert "Доступ" in admin, f"админ должен видеть любую заявку: {admin}"
    print("  OK: админ видит любую заявку")


async def scenario_list(tmp: Path) -> None:
    store = fresh_store(tmp)
    user_box = Toolbox(store, CHAT, USER)
    await user_box.execute("create_ticket", {"title": "Моя заявка", "description": "текст"})
    other_box = Toolbox(store, CHAT, OTHER)
    await other_box.execute("create_ticket", {"title": "Чужая заявка", "description": "текст"})

    mine = await user_box.execute("list_user_tickets", {})
    assert "Моя заявка" in mine and "Чужая заявка" not in mine, f"видна чужая заявка: {mine}"
    print("  OK: пользователь видит только свои заявки")

    everything = await Toolbox(store, CHAT, ADMIN, is_admin=True).execute("list_user_tickets", {})
    assert "Моя заявка" in everything and "Чужая заявка" in everything, "админ должен видеть все"
    assert "Все заявки" in everything, f"для админа заголовок должен быть «Все заявки»: {everything}"
    print("  OK: админ видит все заявки")

    empty = await Toolbox(fresh_store(tmp / "empty"), CHAT, USER).execute("list_user_tickets", {})
    assert "Заявок пока нет" in empty, f"пустой список должен сообщать об этом: {empty}"
    print("  OK: пустой список обрабатывается")


async def scenario_messages_and_status(tmp: Path) -> None:
    store = fresh_store(tmp)
    box = Toolbox(store, CHAT, USER)
    await box.execute("create_ticket", {"title": "Диалог", "description": "текст"})
    tid = store.list_all()[0].id

    added = await box.execute("add_ticket_message", {"ticket_id": tid, "text": "Есть новости?"})
    assert "Сообщение добавлено" in added, f"сообщение не добавлено: {added}"
    assert len(store.get(tid).messages) == 1, "сообщение не сохранилось"
    print("  OK: сообщение добавляется в заявку")

    closed = await box.execute("close_ticket", {"ticket_id": tid})
    assert "закрыта" in closed.lower(), f"заявка не закрылась: {closed}"
    print("  OK: автор может закрыть свою заявку")

    blocked = await box.execute("add_ticket_message", {"ticket_id": tid, "text": "ещё"})
    assert "закрытую" in blocked.lower(), f"в закрытую писать нельзя: {blocked}"
    print("  OK: в закрытую заявку писать нельзя")

    stranger_close = await Toolbox(store, CHAT, OTHER).execute("close_ticket", {"ticket_id": tid})
    assert "Только автор" in stranger_close, f"чужой не должен закрывать: {stranger_close}"
    print("  OK: чужой не может закрыть заявку")

    reopened = await box.execute("reopen_ticket", {"ticket_id": tid})
    assert "открыта снова" in reopened.lower(), f"заявка не открылась: {reopened}"
    print("  OK: заявку можно открыть снова")


async def scenario_admin_actions(tmp: Path) -> None:
    store = fresh_store(tmp)
    user_box = Toolbox(store, CHAT, USER)
    await user_box.execute("create_ticket", {"title": "Для админа", "description": "текст"})
    tid = store.list_all()[0].id
    admin_box = Toolbox(store, CHAT, ADMIN, is_admin=True)

    denied = await user_box.execute("change_ticket_priority", {"ticket_id": tid, "priority": "critical"})
    assert "Только администраторы" in denied, f"пользователь не должен менять приоритет: {denied}"
    print("  OK: обычный пользователь не меняет приоритет")

    bad = await admin_box.execute("change_ticket_priority", {"ticket_id": tid, "priority": "срочно"})
    assert "Неверный приоритет" in bad, f"некорректный приоритет должен отклоняться: {bad}"
    print("  OK: некорректный приоритет отклоняется")

    ok = await admin_box.execute("change_ticket_priority", {"ticket_id": tid, "priority": "critical"})
    assert "изменён" in ok, f"админ должен менять приоритет: {ok}"
    assert store.get(tid).priority.value == "critical", "приоритет не сохранился"
    print("  OK: админ меняет приоритет")

    cat = await admin_box.execute("change_ticket_category", {"ticket_id": tid, "category": "billing"})
    assert "изменена" in cat, f"админ должен менять категорию: {cat}"
    print("  OK: админ меняет категорию")

    assigned = await admin_box.execute("assign_ticket", {"ticket_id": tid, "assignee_id": ADMIN})
    assert "назначена" in assigned, f"админ должен назначать: {assigned}"
    assert store.get(tid).assignee_id == ADMIN, "исполнитель не сохранился"
    print("  OK: админ назначает исполнителя")

    missing = await admin_box.execute("change_ticket_priority", {"ticket_id": "неттакой", "priority": "low"})
    assert "не найдена" in missing, f"несуществующая заявка должна сообщать об этом: {missing}"
    print("  OK: несуществующая заявка обрабатывается мягко")


async def scenario_search(tmp: Path) -> None:
    """Поиск заявок: свои видны, чужие скрыты, админ видит всё."""
    store = fresh_store(tmp)
    user_box = Toolbox(store, CHAT, USER)
    await user_box.execute("create_ticket", {"title": "Не работает принтер", "description": "бумага застряла"})
    await Toolbox(store, CHAT, OTHER).execute(
        "create_ticket", {"title": "Принтер в другом отделе", "description": "не печатает"}
    )

    mine = await user_box.execute("search_tickets", {"query": "принтер"})
    assert "Не работает принтер" in mine, f"своя заявка не найдена: {mine}"
    assert "другом отделе" not in mine, f"показана чужая заявка: {mine}"
    print("  OK: поиск находит свои заявки и скрывает чужие")

    admin = await Toolbox(store, CHAT, ADMIN, is_admin=True).execute("search_tickets", {"query": "принтер"})
    assert "Не работает принтер" in admin and "другом отделе" in admin, f"админ должен видеть обе: {admin}"
    print("  OK: админ находит заявки всех пользователей")

    empty = await user_box.execute("search_tickets", {"query": "холодильник"})
    assert "ничего не найдено" in empty.lower(), f"нет сообщения об отсутствии результатов: {empty}"
    print("  OK: пустой результат обрабатывается")

    bad = await user_box.execute("search_tickets", {"query": "  "})
    assert "Ошибка" in bad, f"пустой запрос должен отклоняться: {bad}"
    print("  OK: пустой запрос отклоняется")


async def scenario_html_escaping(tmp: Path) -> None:
    """Тексты заявок не ломают HTML-разметку ответов бота."""
    store = fresh_store(tmp)
    box = Toolbox(store, CHAT, USER)

    created = await box.execute(
        "create_ticket",
        {"title": "Сбой <b>кассы</b> & терминала", "description": "Описание <script>alert(1)</script>"},
    )
    assert "&lt;b&gt;" in created, f"тег в заголовке не экранирован: {created}"
    assert "&amp;" in created, f"амперсанд не экранирован: {created}"
    tid = store.list_all()[0].id

    listed = await box.execute("list_user_tickets", {})
    assert "&lt;b&gt;" in listed, f"тег в списке не экранирован: {listed}"
    assert "<b>" not in listed, f"в список попал живой тег: {listed}"
    print("  OK: заголовок экранируется в создании и в списке")

    shown = await box.execute("get_ticket", {"ticket_id": tid})
    assert "&lt;script&gt;" in shown, f"описание не экранировано: {shown}"
    print("  OK: описание экранируется при просмотре")

    await box.execute("add_ticket_message", {"ticket_id": tid, "text": "текст <b>с тегом</b> & ещё"})
    shown = await box.execute("get_ticket", {"ticket_id": tid})
    assert "&lt;b&gt;с тегом&lt;/b&gt;" in shown, f"сообщение не экранировано: {shown}"
    print("  OK: сообщения в заявке экранируются")

    found = await box.execute("search_tickets", {"query": "кассы"})
    assert "&lt;b&gt;" in found, f"поиск не экранирует заголовок: {found}"
    injected = await box.execute("search_tickets", {"query": "<script>"})
    assert "<script>" not in injected, f"поисковый запрос не экранирован: {injected}"
    print("  OK: поиск экранирует и заголовки, и сам запрос")


async def scenario_unknown_tool(tmp: Path) -> None:
    store = fresh_store(tmp)
    result = await Toolbox(store, CHAT, USER).execute("delete_everything", {})
    assert "Неизвестный инструмент" in result, f"неизвестный инструмент должен отклоняться: {result}"
    print("  OK: неизвестный инструмент не выполняется")


async def main() -> None:
    base = ROOT / ".tmp-test-tools"
    print("Проверка инструментов ИИ (Toolbox)")

    print("\n[создание заявки]")
    await scenario_create(base / "create")
    print("\n[права доступа]")
    await scenario_access(base / "access", "")
    print("\n[списки заявок]")
    await scenario_list(base / "list")
    print("\n[сообщения и статусы]")
    await scenario_messages_and_status(base / "messages")
    print("\n[действия администратора]")
    await scenario_admin_actions(base / "admin")
    print("\n[поиск заявок]")
    await scenario_search(base / "search")
    print("\n[спецсимволы в ответах]")
    await scenario_html_escaping(base / "html")
    print("\n[неизвестный инструмент]")
    await scenario_unknown_tool(base / "unknown")

    shutil.rmtree(base, ignore_errors=True)
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
