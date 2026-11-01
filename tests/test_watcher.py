"""Проверка логики SD-мониторинга без сети и без Telegram.

Запуск:  python tests/test_watcher.py
Сторонних зависимостей не требует (pytest не нужен).
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from sd_client import SDRequest  # noqa: E402
from sd_state import SDStateStore  # noqa: E402
from sd_watcher import SDWatcher  # noqa: E402


class FakeBot:
    """Запоминает отправленные сообщения вместо реального Telegram."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs: object) -> None:
        self.sent.append((chat_id, text))


class FakeClient:
    """Отдаёт заранее заданный список заявок."""

    def __init__(self, items: list[SDRequest]) -> None:
        self.items = items

    async def fetch_requests(self, view: str, condition: str, limit: int = 100):
        return list(self.items), len(self.items)


class FailingBot(FakeBot):
    """Telegram недоступен: имитируем сетевую ошибку при отправке."""

    def __init__(self) -> None:
        super().__init__()
        self.fails = True
        self.attempts = 0

    async def send_message(self, chat_id: int, text: str, **_kwargs: object) -> None:
        self.attempts += 1
        if self.fails:
            raise RuntimeError("Telegram недоступен")
        await super().send_message(chat_id, text, **_kwargs)


def make_request(number: int) -> SDRequest:
    return SDRequest(
        sys_id=f"{number:032x}",
        number=f"REQ{4305000 + number}",
        short_description=f"Тестовая заявка {number}",
        assigned_user="Иванов И.И.",
        sys_created_at="01.11.2026 10:00",
        state="Новая",
        multicard_comment_ing="",
    ).with_base_url("https://sd.example.ru")


def make_config(data_dir: Path, seed: bool = True) -> Config:
    return Config(
        bot_token="1:x",
        allowed_user_ids=frozenset({1}),
        data_dir=data_dir,
        sd_enabled=True,
        sd_check_interval=60,
        sd_view="Default",
        sd_condition="state=1",
        sd_seed_on_first_run=seed,
    )


async def scenario_first_run_is_silent(data_dir: Path) -> None:
    """Чистый диск: старые заявки запоминаются, приходит одно сводное сообщение."""
    bot = FakeBot()
    store = SDStateStore(data_dir)
    client = FakeClient([make_request(1), make_request(2), make_request(3)])
    watcher = SDWatcher(make_config(data_dir), bot, store, client)

    result = await watcher._check_once()

    assert result.seeded == 3, f"ожидали 3 запомненные заявки, получили {result.seeded}"
    assert not result.new_requests, "на первом запуске не должно быть уведомлений"
    assert len(bot.sent) == 1, f"ожидали одно сводное сообщение, получили {len(bot.sent)}"
    assert "мониторинг запущен" in bot.sent[0][1].lower()
    print("  OK: первый запуск молча запомнил 3 заявки и прислал одну сводку")


async def scenario_new_ticket_notified_once(data_dir: Path) -> None:
    """Появление новой заявки даёт ровно одно уведомление и не повторяется."""
    bot = FakeBot()
    store = SDStateStore(data_dir)
    client = FakeClient([make_request(10), make_request(11)])
    watcher = SDWatcher(make_config(data_dir), bot, store, client)
    await watcher._check_once()
    sent_after_seed = len(bot.sent)

    client.items = [make_request(10), make_request(11), make_request(12)]
    result = await watcher._check_once()

    assert len(result.new_requests) == 1, "должна быть одна новая заявка"
    assert len(bot.sent) == sent_after_seed + 1, "ровно одно новое сообщение"
    assert "REQ4305012" in bot.sent[-1][1]
    print("  OK: новая заявка пришла одним уведомлением")

    # Повторная проверка того же списка не должна слать снова
    before = len(bot.sent)
    await watcher._check_once()
    assert len(bot.sent) == before, "повторная проверка не должна слать дубли"
    print("  OK: повторная проверка не дублирует уведомления")


def scenario_state_persisted(data_dir: Path) -> None:
    """Состояние читается новым экземпляром хранилища (переживает перезапуск)."""
    store = SDStateStore(data_dir)
    state = store.load()
    assert len(state.seen_numbers) == 3, f"ожидали 3 записанные заявки, получили {len(state.seen_numbers)}"
    assert (data_dir / "sd_state.json").exists(), "файл состояния должен лежать на диске"
    assert state.last_check > 0, "время проверки должно фиксироваться самой проверкой"
    print("  OK: состояние читается с диска после перезапуска")


async def scenario_failed_send_is_retried(data_dir: Path) -> None:
    """Недоставленное уведомление не теряется: повтор на следующей проверке."""
    bot = FailingBot()
    store = SDStateStore(data_dir)
    client = FakeClient([make_request(40)])
    watcher = SDWatcher(make_config(data_dir, seed=False), bot, store, client)

    await watcher._check_once()
    assert bot.attempts == 1, f"попытка отправки не сделана: {bot.attempts}"
    assert not store.state.seen_numbers, "недоставленная заявка помечена как отправленная — она потеряна"
    assert store.state.last_check > 0, "время проверки не зафиксировано — сработает «тихий первый запуск»"
    print("  OK: при ошибке отправки заявка не помечается отправленной")

    bot.fails = False
    await watcher._check_once()
    assert len(bot.sent) == 1, f"повторная проверка не доставила уведомление: {len(bot.sent)}"
    assert store.state.seen_numbers == ["REQ4305040"], f"после успеха заявка не запомнена: {store.state.seen_numbers}"
    print("  OK: следующая проверка доставляет уведомление и запоминает заявку")


async def scenario_html_in_data(data_dir: Path) -> None:
    """Спецсимволы из SD не ломают HTML-разметку уведомления."""
    bot = FakeBot()
    store = SDStateStore(data_dir)
    tricky = SDRequest(
        sys_id="a" * 32,
        number="REQ999",
        short_description="Сбой <b>кассы</b> & терминала",
        assigned_user="Петров & Ко",
        sys_created_at="01.11.2026 10:00",
        state="Новая",
        multicard_comment_ing="Клиент: <script>alert(1)</script> & прочее",
    ).with_base_url("https://sd.example.ru")
    watcher = SDWatcher(make_config(data_dir, seed=False), bot, store, FakeClient([tricky]))

    await watcher._check_once()

    text = bot.sent[0][1]
    assert "&amp;" in text, f"амперсанд не экранирован: {text}"
    assert "&lt;b&gt;" in text, f"тег из данных не экранирован: {text}"
    assert "&lt;script&gt;" in text, f"скрипт из данных не экранирован: {text}"
    # Собственная разметка бота должна остаться рабочей
    assert "<b>Тема:</b>" in text, f"пропала своя разметка: {text}"
    assert '<a href="https://sd.example.ru/nav_to.do' in text, "пропала ссылка на заявку"
    print("  OK: данные из SD экранируются, своя разметка сохраняется")


async def scenario_recent_notifications_show_code(data_dir: Path) -> None:
    """В /sd_last заявка показывается рабочим кодом, REQ — в скобках."""
    bot = FakeBot()
    store = SDStateStore(data_dir)
    coded = SDRequest(
        sys_id="c" * 32,
        number="REQ7770001",
        short_description="SUTSPROD-17353334",
        assigned_user="Петров",
        sys_created_at="31.10.2026 14:29",
        state="В работе",
        multicard_comment_ing="",
    ).with_base_url("https://sd.example.ru")
    plain = SDRequest(
        sys_id="d" * 32,
        number="REQ7770002",
        short_description="Не работает принтер",
        assigned_user="Иванов",
        sys_created_at="31.10.2026 14:30",
        state="Новая",
        multicard_comment_ing="",
    ).with_base_url("https://sd.example.ru")

    watcher = SDWatcher(
        make_config(data_dir, seed=False), bot, store, FakeClient([coded, plain])
    )
    await watcher._check_once()

    recent = watcher.get_recent_notifications(5)
    assert "SUTSPROD-17353334" in recent, f"нет кода в списке: {recent}"
    assert "REQ7770002" in recent, f"заявка без кода потерялась: {recent}"
    assert all("SUTSPROD" not in item for item in recent if "7770002" in item), (
        "у заявки без кода не должно быть подписи"
    )
    print("  OK: /sd_last показывает только код SUTSPROD")


async def scenario_seed_disabled(data_dir: Path) -> None:
    """SD_SEED_ON_FIRST_RUN=false — старое поведение: шлём всё, что не видели."""
    bot = FakeBot()
    store = SDStateStore(data_dir)
    client = FakeClient([make_request(20), make_request(21)])
    watcher = SDWatcher(make_config(data_dir, seed=False), bot, store, client)

    result = await watcher._check_once()

    assert result.seeded == 0
    assert len(result.new_requests) == 2, "без тихого запуска должны уйти обе заявки"
    assert len(bot.sent) == 2
    print("  OK: при SD_SEED_ON_FIRST_RUN=false уведомления уходят как раньше")


async def scenario_no_recipients(data_dir: Path) -> None:
    """Без получателей мониторинг не падает, а просто молчит."""
    cfg = Config(bot_token="1:x", allowed_user_ids=frozenset(), data_dir=data_dir)
    bot = FakeBot()
    store = SDStateStore(data_dir)
    client = FakeClient([make_request(30)])
    watcher = SDWatcher(cfg, bot, store, client)

    result = await watcher._check_once()
    assert result.seeded == 1, "заявка всё равно должна быть запомнена"
    assert not bot.sent, "отправлять некуда — сообщений быть не должно"
    print("  OK: пустой список получателей не роняет мониторинг")


async def main() -> None:
    base = ROOT / ".tmp-test-state"
    print("Проверка логики SD-мониторинга")

    for name, coro in (
        ("первый запуск", scenario_first_run_is_silent),
        ("новая заявка", scenario_new_ticket_notified_once),
    ):
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        print(f"\n[{name}]")
        await coro(base)
        if name == "первый запуск":
            scenario_state_persisted(base)

    print("\n[без тихого запуска]")
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    await scenario_seed_disabled(base)

    print("\n[ошибка отправки]")
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    await scenario_failed_send_is_retried(base)

    print("\n[код заявки в /sd_last]")
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    await scenario_recent_notifications_show_code(base)

    print("\n[спецсимволы в данных]")
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    await scenario_html_in_data(base)

    print("\n[без получателей]")
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    await scenario_no_recipients(base)

    shutil.rmtree(base, ignore_errors=True)
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
