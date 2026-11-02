"""Проверка логики SD-мониторинга без сети и без Telegram.

Запуск:  python tests/test_watcher.py
Сторонних зависимостей не требует (pytest не нужен).
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from sd_client import SDRequest  # noqa: E402
from sd_sla import resolve_timezone  # noqa: E402
from sd_state import SDStateStore  # noqa: E402
from sd_watcher import SDWatcher  # noqa: E402

TZ = resolve_timezone("Europe/Moscow")


def at(hour: int = 12, minute: int = 0, day: int = 1) -> datetime:
    """Время в поясе SD (Москва) для сценариев SLA."""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


class FakeBot:
    """Запоминает отправленные сообщения вместо реального Telegram."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.kwargs: list[dict] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.sent.append((chat_id, text))
        self.kwargs.append(kwargs)


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


def make_request(number: int, created: str = "01.11.2026 10:00") -> SDRequest:
    return SDRequest(
        sys_id=f"{number:032x}",
        number=f"REQ{4305000 + number}",
        short_description=f"Тестовая заявка {number}",
        assigned_user="Иванов И.И.",
        sys_created_at=created,
        state="Новая",
        multicard_comment_ing="",
    ).with_base_url("https://sd.example.ru")


def make_sla_request(
    number: int,
    deadline: datetime | None,
    *,
    created: str = "01.11.2026 10:00",
    resolved: str | None = None,
) -> SDRequest:
    """Заявка с дедлайном СУТС для сценариев SLA."""
    return SDRequest(
        sys_id=f"{number:032x}",
        number=f"REQ{4305000 + number}",
        short_description=f"SUTSPROD-{17350000 + number}",
        assigned_user="Иванов И.И.",
        sys_created_at=created,
        state="В работе",
        multicard_comment_ing="",
        multicard_request_type="Поддержка касс",
        multicard_name_client="produkty",
        multicard_terminal_address="Нижний Новгород, пер Камчатский, 7",
        multicard_deadline=(
            deadline.strftime("%d.%m.%Y %H:%M") if deadline is not None else None
        ),
        resolved_at=resolved,
        deadline_at=deadline,
    ).with_base_url("https://sd.example.ru")


def signature_for(cfg: Config) -> str:
    """Та же подпись фильтра, что считает watcher."""
    raw = f"{cfg.sd_view}\n{cfg.sd_condition}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def seen_store(data_dir: Path, req: SDRequest, cfg: Config) -> SDStateStore:
    """Состояние, в котором заявка известна, а подпись фильтра уже стоит."""
    store = SDStateStore(data_dir)
    state = store.load()
    state.mark_seen(req.number, req.sys_id, req.code)
    state.sla_initialized = True
    state.filter_signature = signature_for(cfg)
    store.save()
    return store


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
    assert '<a href="https://sd.example.ru/record/itsm_request/' in text, "пропала ссылка на заявку"
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


async def scenario_sla_warn_once(data_dir: Path) -> None:
    """В окне 2 ч приходит одно предупреждение с кнопкой и дедлайном."""
    req = make_sla_request(100, at(18))
    cfg = make_config(data_dir, seed=False)
    store = seen_store(data_dir, req, cfg)
    bot = FakeBot()
    watcher = SDWatcher(cfg, bot, store, FakeClient([req]))

    result = await watcher._check_once(now=at(16, 30))

    assert result.sla_warned == 1, f"предупреждение не отправлено: {result}"
    text = bot.sent[-1][1]
    assert "До конца SLA 2 ч" in text, f"не тот текст:\n{text}"
    assert "Дедлайн СУТС:</b> 01.11.2026 18:00" in text
    assert "Тип:</b> Поддержка касс" in text
    assert "Клиент:</b> produkty" in text
    assert store.state.sla_warn_sent == 1

    markup = bot.kwargs[-1].get("reply_markup")
    assert markup is not None, "нет кнопки «Открыть в SD»"
    button = markup.inline_keyboard[0][0]
    assert button.text == "Открыть в SD" and button.url == req.url, (
        f"кнопка ведёт не туда: {button.text} -> {button.url}"
    )

    before = len(bot.sent)
    result = await watcher._check_once(now=at(16, 31))
    assert len(bot.sent) == before, "предупреждение продублировано"
    assert result.sla_warned == 0
    print("  OK: предупреждение за 2 ч приходит один раз, со ссылкой и кнопкой")


async def scenario_sla_expired_once(data_dir: Path) -> None:
    """Просрочка приходит один раз и без запоздалого «осталось 2 ч»."""
    req = make_sla_request(101, at(18))
    cfg = make_config(data_dir, seed=False)
    store = seen_store(data_dir, req, cfg)
    bot = FakeBot()
    watcher = SDWatcher(cfg, bot, store, FakeClient([req]))

    result = await watcher._check_once(now=at(18, 30))

    assert result.sla_expired == 1, f"сообщение о просрочке не отправлено: {result}"
    text = bot.sent[-1][1]
    assert "сгорела по SLA" in text, f"не тот текст:\n{text}"
    assert "Просрочено:</b> 30 мин" in text
    assert store.state.sla_expired_sent == 1
    assert store.state.sla_warn_sent == 0, "запоздалое «осталось 2 ч» не нужно"

    before = len(bot.sent)
    result = await watcher._check_once(now=at(18, 31))
    assert len(bot.sent) == before, "просрочка продублирована"
    print("  OK: «сгорела» приходит один раз, даже если предупреждение пропущено")


async def scenario_sla_deadline_changed(data_dir: Path) -> None:
    """Перенос дедлайна в SD запускает новый цикл уведомлений."""
    req = make_sla_request(102, at(18))
    cfg = make_config(data_dir, seed=False)
    store = seen_store(data_dir, req, cfg)
    bot = FakeBot()
    client = FakeClient([req])
    watcher = SDWatcher(cfg, bot, store, client)

    await watcher._check_once(now=at(16, 30))
    assert store.state.sla_warn_sent == 1

    client.items = [make_sla_request(102, at(20))]
    before = len(bot.sent)
    await watcher._check_once(now=at(16, 31))
    assert len(bot.sent) == before, "по новому сроку предупреждение ещё рано"

    result = await watcher._check_once(now=at(18, 30))
    assert result.sla_warned == 1, "по новому дедлайну предупреждение не пришло"
    assert store.state.sla_warn_sent == 2
    print("  OK: после переноса дедлайна уведомления срабатывают заново")


async def scenario_new_requests_sorted_by_date(data_dir: Path) -> None:
    """Пачка новых сортируется по дате, а не по строке «02.11» < «31.10»."""
    old = make_request(130, created="31.10.2026 23:59")
    new = make_request(131, created="02.11.2026 00:01")
    cfg = make_config(data_dir, seed=False)
    bot = FakeBot()
    watcher = SDWatcher(cfg, bot, SDStateStore(data_dir), FakeClient([new, old]))

    result = await watcher._check_once(now=at(12))

    assert result.total_count == 2
    texts = [text for _, text in bot.sent]
    assert len(texts) == 2, f"ожидали два уведомления, получили {len(texts)}"
    assert "REQ4305130" in texts[0], f"первым должно быть 31.10:\n{texts[0]}"
    assert "REQ4305131" in texts[1], f"вторым должно быть 02.11:\n{texts[1]}"
    print("  OK: новые заявки идут в правильном хронологическом порядке")


async def scenario_filter_migration(data_dir: Path) -> None:
    """Смена фильтра: тихая переинициализация и дайджест, без залпа."""
    old = make_request(120, created="30.10.2026 10:00")
    new1 = make_request(121, created="31.10.2026 11:00")
    new2 = make_request(122, created="02.11.2026 09:00")
    cfg = make_config(data_dir, seed=False)
    store = SDStateStore(data_dir)
    state = store.load()
    state.mark_seen(old.number, old.sys_id, old.code)
    state.filter_signature = "old-signature"
    state.sla_initialized = True
    store.save()

    bot = FakeBot()
    watcher = SDWatcher(cfg, bot, store, FakeClient([old, new1, new2]))
    result = await watcher._check_once(now=at(12))

    assert result.filter_changed is True, "смена фильтра не распознана"
    assert result.seeded == 3
    assert result.total_count == 3
    assert len(bot.sent) == 1, f"ожидали одну сводку, получили {len(bot.sent)}"
    text = bot.sent[0][1]
    assert "Фильтр заявок обновлён" in text, text
    assert "Новых для бота: 2" in text, text
    assert "Пропущенные заявки" in text and "REQ4305121" in text and "REQ4305122" in text
    assert "Новая заявка" not in text, "не должно быть отдельных уведомлений"
    assert store.state.is_seen(new1.number, new1.sys_id), "новая заявка не помечена увиденной"
    assert store.state.filter_signature == watcher._filter_signature()

    before = len(bot.sent)
    await watcher._check_once(now=at(12, 1))
    assert len(bot.sent) == before, "после миграции сообщения не повторяются"
    print("  OK: смена фильтра даёт одну сводку с дайджестом, без залпа")


async def scenario_filter_upgrade_empty_signature(data_dir: Path) -> None:
    """Обновление с версии без подписи: старый срез не рассылается заново."""
    seen = make_request(123, created="30.10.2026 10:00")
    fresh = make_request(124, created="02.11.2026 09:30")
    cfg = make_config(data_dir, seed=False)
    store = SDStateStore(data_dir)
    state = store.load()
    state.mark_seen(seen.number, seen.sys_id, seen.code)
    # filter_signature пустой — как после обновления кода
    state.sla_initialized = True
    store.save()

    bot = FakeBot()
    watcher = SDWatcher(cfg, bot, store, FakeClient([seen, fresh]))
    result = await watcher._check_once(now=at(12))

    assert result.filter_changed is True
    assert len(bot.sent) == 1 and "Фильтр заявок обновлён" in bot.sent[0][1]
    assert "Новых для бота: 1" in bot.sent[0][1]
    assert store.state.is_seen(fresh.number, fresh.sys_id)
    assert store.state.filter_signature == watcher._filter_signature()
    print("  OK: обновление без подписи не присылает старые заявки заново")


async def scenario_sla_first_enable(data_dir: Path) -> None:
    """Первое включение SLA на работающем боте: одна сводка, без пачки «сгоревших»."""
    req = make_sla_request(103, at(10))
    cfg = make_config(data_dir, seed=False)
    store = SDStateStore(data_dir)
    state = store.load()
    state.mark_seen(req.number, req.sys_id, req.code)
    state.filter_signature = signature_for(cfg)  # фильтр не менялся
    state.sla_initialized = False  # SLA включается впервые
    store.save()

    bot = FakeBot()
    watcher = SDWatcher(cfg, bot, store, FakeClient([req]))

    result = await watcher._check_once(now=at(12))

    assert result.sla_expired == 0, "не должно быть сообщений о старых просрочках"
    assert len(bot.sent) == 1, f"ожидали одну сводку, получили {len(bot.sent)}"
    assert "SLA-контроль включён" in bot.sent[0][1], bot.sent[0][1]
    assert store.state.sla_initialized is True
    assert store.state.sla_expired_sent == 0

    before = len(bot.sent)
    await watcher._check_once(now=at(12, 1))
    assert len(bot.sent) == before, "после тихой инициализации алерты не нужны"
    print("  OK: при первом включении SLA тихо берёт сроки под наблюдение")


async def scenario_sla_first_run(data_dir: Path) -> None:
    """Первый запуск: история молчит, но SLA-сводка честная."""
    req = make_sla_request(104, at(10))
    bot = FakeBot()
    watcher = SDWatcher(
        make_config(data_dir, seed=True), bot, SDStateStore(data_dir), FakeClient([req])
    )

    result = await watcher._check_once(now=at(12))

    assert result.seeded == 1
    assert len(bot.sent) == 1, f"ожидали одно сообщение, получили {len(bot.sent)}"
    text = bot.sent[0][1]
    assert "SD-мониторинг запущен" in text and "SLA" in text, text
    assert "Последние заявки по фильтру" in text, text
    assert "Новая заявка" not in text, "история не должна литься отдельными сообщениями"
    assert "сгорела по SLA" not in text, "история не должна литься в чат"

    before = len(bot.sent)
    await watcher._check_once(now=at(12, 1))
    assert len(bot.sent) == before
    print("  OK: первый запуск запоминает сроки молча и присылает одну сводку")


async def scenario_sla_new_ticket_no_double(data_dir: Path) -> None:
    """Новая заявка с уже горящим SLA не даёт два сообщения подряд."""
    req = make_sla_request(105, at(10))
    bot = FakeBot()
    store = SDStateStore(data_dir)
    watcher = SDWatcher(make_config(data_dir, seed=False), bot, store, FakeClient([req]))

    result = await watcher._check_once(now=at(12))

    assert result.new_requests and result.sla_expired == 0, f"двойное уведомление: {result}"
    assert len(bot.sent) == 1 and "Новая заявка" in bot.sent[0][1], bot.sent

    before = len(bot.sent)
    await watcher._check_once(now=at(12, 1))
    assert len(bot.sent) == before, "SLA-алерт по новой заявке всё-таки пришёл"
    assert store.state.sla_expired_sent == 0
    print("  OK: у новой заявки дедлайн сообщается один раз, без второго алерта")


async def scenario_sla_skips_unusable(data_dir: Path) -> None:
    """Без дедлайна и по закрытым заявкам SLA-сообщений нет."""
    no_deadline = make_sla_request(106, None)
    resolved = make_sla_request(107, at(10), resolved="01.11.2026 10:30")

    store = SDStateStore(data_dir)
    cfg = make_config(data_dir, seed=False)
    state = store.load()
    for req in (no_deadline, resolved):
        state.mark_seen(req.number, req.sys_id, req.code)
    state.sla_initialized = True
    state.filter_signature = signature_for(cfg)
    store.save()

    bot = FakeBot()
    watcher = SDWatcher(
        cfg,
        bot,
        store,
        FakeClient([no_deadline, resolved]),
    )
    result = await watcher._check_once(now=at(12))

    assert not bot.sent, f"лишние сообщения: {bot.sent}"
    assert result.sla_warned == 0 and result.sla_expired == 0
    print("  OK: заявки без дедлайна и закрытые под SLA не попадают")


async def scenario_sla_retry_after_failure(data_dir: Path) -> None:
    """Недоставленный SLA-алерт не теряется и не дублируется после успеха."""
    req = make_sla_request(108, at(10))
    cfg = make_config(data_dir, seed=False)
    store = seen_store(data_dir, req, cfg)
    bot = FailingBot()
    watcher = SDWatcher(cfg, bot, store, FakeClient([req]))

    await watcher._check_once(now=at(12))
    assert bot.attempts == 1, f"попыток отправки: {bot.attempts}"
    assert store.state.sla_expired_sent == 0
    assert store.state.sla_events[req.sys_id]["expired"] == 0.0, (
        "недоставленное событие отмечено отправленным — оно потеряно"
    )

    bot.fails = False
    result = await watcher._check_once(now=at(12, 1))
    assert result.sla_expired == 1, "повторная попытка не доставила сообщение"
    assert store.state.sla_expired_sent == 1

    before = len(bot.sent)
    await watcher._check_once(now=at(12, 2))
    assert len(bot.sent) == before, "после доставки сообщение продублировано"
    print("  OK: при сбое Telegram SLA-алерт повторяется и не дублируется")


async def scenario_sla_disabled(data_dir: Path) -> None:
    """SD_SLA_ENABLED=false отключает авто-алерты, но не ломает проверку."""
    req = make_sla_request(109, at(10))
    cfg = replace(make_config(data_dir, seed=False), sd_sla_enabled=False)
    store = SDStateStore(data_dir)
    state = store.load()
    state.mark_seen(req.number, req.sys_id, req.code)
    state.filter_signature = signature_for(cfg)
    store.save()
    bot = FakeBot()
    watcher = SDWatcher(cfg, bot, store, FakeClient([req]))

    result = await watcher._check_once(now=at(12))

    assert not bot.sent, f"при выключенном SLA сообщений быть не должно: {bot.sent}"
    assert result.sla_warned == 0 and result.sla_expired == 0
    assert store.state.sla_initialized is False, "выключенный SLA не должен инициализироваться"
    print("  OK: при SD_SLA_ENABLED=false авто-алерты не отправляются")


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

    for title, scenario in (
        ("Фильтр: смена", scenario_filter_migration),
        ("Фильтр: обновление без подписи", scenario_filter_upgrade_empty_signature),
        ("Сортировка новых заявок", scenario_new_requests_sorted_by_date),
        ("SLA: предупреждение", scenario_sla_warn_once),
        ("SLA: просрочка", scenario_sla_expired_once),
        ("SLA: перенос дедлайна", scenario_sla_deadline_changed),
        ("SLA: первое включение", scenario_sla_first_enable),
        ("SLA: первый запуск", scenario_sla_first_run),
        ("SLA: новая заявка без дубля", scenario_sla_new_ticket_no_double),
        ("SLA: неподходящие заявки", scenario_sla_skips_unusable),
        ("SLA: повтор при сбое", scenario_sla_retry_after_failure),
        ("SLA: выключен", scenario_sla_disabled),
    ):
        print(f"\n[{title}]")
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        if title == "SLA: повтор при сбое":
            print("  (ожидаемые ошибки отправки в логе — это часть проверки)")
        await scenario(base)

    shutil.rmtree(base, ignore_errors=True)
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
