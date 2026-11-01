"""Интеграционная проверка бота: настоящие апдейты Telegram без сети.

Апдейты собираются как JSON и скармливаются Dispatcher через feed_update,
ответы перехватываются на уровне Bot.__call__.

Запуск:  python tests/test_updates.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiogram import Bot  # noqa: E402
from aiogram.methods import GetMe  # noqa: E402
from aiogram.types import Update, User  # noqa: E402

BOT_ID = 9115169086
BOT_USERNAME = "ServMS_bot"

from bot import build_dispatcher  # noqa: E402
from config import Config  # noqa: E402
from tickets import TicketStore  # noqa: E402

ALLOWED = 1001
STRANGER = 555
CHAT = 1001


class RecordingBot(Bot):
    """Перехватывает исходящие вызовы API вместо отправки в Telegram."""

    def __init__(self) -> None:
        super().__init__(token="123456789:AA-test")
        self.calls: list[object] = []

    async def __call__(self, method, request_timeout=None):  # noqa: ANN001
        self.calls.append(method)
        # Фильтр команд для «/id@бот» спрашивает у Telegram данные бота
        if isinstance(method, GetMe):
            return User(
                id=BOT_ID,
                is_bot=True,
                first_name="ServZa",
                username=BOT_USERNAME,
            )
        return method

    @property
    def texts(self) -> list[str]:
        return [str(getattr(call, "text", "")) for call in self.calls]

    def last_text(self) -> str:
        return self.texts[-1] if self.texts else ""

    def reset(self) -> None:
        self.calls.clear()


def make_update(
    text: str,
    user_id: int = ALLOWED,
    chat_id: int = CHAT,
    message_id: int = 1,
    first_name: str = "Иван",
    chat_type: str = "private",
):
    payload = {
        "update_id": int(time.time() * 1000) % 1_000_000 + message_id,
        "message": {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": chat_type},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": first_name,
                "language_code": "ru",
            },
            "text": text,
        },
    }
    return payload


def make_cfg(data_dir: Path, allowed: frozenset[int] = frozenset({ALLOWED}), sd_enabled: bool = False) -> Config:
    return Config(
        bot_token="123456789:AA-test",
        allowed_user_ids=allowed,
        admin_user_ids=frozenset({ALLOWED}),
        data_dir=data_dir,
        sd_enabled=sd_enabled,
        sd_check_interval=60,
    )


async def feed(dp, bot, payload) -> None:
    update = Update.model_validate(payload, context={"bot": bot})
    await dp.feed_update(bot, update)


async def scenario_allowed_user(data_dir: Path) -> None:
    """/start приходит с приветствием, /help — со справкой."""
    cfg = make_cfg(data_dir)
    dp = build_dispatcher(cfg, TicketStore(data_dir / "tickets.json"))
    bot = RecordingBot()
    try:
        await feed(dp, bot, make_update("/start"))
        assert "Привет" in bot.last_text(), f"нет приветствия: {bot.last_text()}"
        assert "Иван" in bot.last_text(), f"нет имени: {bot.last_text()}"

        await feed(dp, bot, make_update("/help", message_id=2))
        assert "Бот заявок" in bot.last_text(), f"нет справки: {bot.last_text()}"
        print("  OK: /start и /help отвечают разрешённому пользователю")

        bot.reset()
        await feed(dp, bot, make_update("/my", message_id=3))
        assert "Заявок пока нет" in bot.last_text(), f"/my не ответил: {bot.last_text()}"
        print("  OK: /my отвечает и не падает на пустом списке")

        bot.reset()
        await feed(dp, bot, make_update("/id", message_id=4))
        assert str(ALLOWED) in bot.last_text(), f"/id не показал ID: {bot.last_text()}"
        print("  OK: /id показывает идентификаторы")
    finally:
        await bot.session.close()


async def scenario_stranger_denied(data_dir: Path) -> None:
    """Посторонний не получает ни данных, ни диалога."""
    cfg = make_cfg(data_dir)
    dp = build_dispatcher(cfg, TicketStore(data_dir / "tickets.json"))
    bot = RecordingBot()
    try:
        for command in ("/help", "/my", "/all", "/sd_status", "привет, создай заявку"):
            bot.reset()
            await feed(dp, bot, make_update(command, user_id=STRANGER, chat_id=STRANGER))
            assert bot.calls, f"на «{command}» бот вообще не ответил (лучше явный отказ)"
            assert "Доступ ограничен" in bot.last_text(), (
                f"на «{command}» нет отказа: {bot.last_text()}"
            )
        print("  OK: посторонний получает отказ на команды и на текст")
    finally:
        await bot.session.close()


async def scenario_open_bot(data_dir: Path) -> None:
    """Пустой ALLOWED_USER_IDS — бот открыт (режим отладки)."""
    cfg = make_cfg(data_dir, allowed=frozenset())
    dp = build_dispatcher(cfg, TicketStore(data_dir / "tickets.json"))
    bot = RecordingBot()
    try:
        await feed(dp, bot, make_update("/help", user_id=STRANGER, chat_id=STRANGER))
        assert "Бот заявок" in bot.last_text(), f"открытый бот не ответил: {bot.last_text()}"
        print("  OK: при пустом ALLOWED_USER_IDS бот отвечает всем")
    finally:
        await bot.session.close()


async def scenario_admin_commands(data_dir: Path) -> None:
    """/all доступен админу и закрыт для обычного пользователя."""
    cfg = make_cfg(data_dir, allowed=frozenset({ALLOWED}), sd_enabled=False)
    store = TicketStore(data_dir / "tickets.json")
    store.create(user_id=STRANGER, chat_id=STRANGER, title="Чужая", description="d")
    dp = build_dispatcher(cfg, store)
    bot = RecordingBot()
    try:
        await feed(dp, bot, make_update("/all", user_id=ALLOWED))
        assert "Чужая" in bot.last_text(), f"админ не увидел заявку: {bot.last_text()}"
        print("  OK: админ видит все заявки через /all")

        bot.reset()
        await feed(dp, bot, make_update("/all", user_id=STRANGER, chat_id=STRANGER, message_id=9))
        assert "Доступ ограничен" in bot.last_text(), (
            f"посторонний не должен даже узнать про /all: {bot.last_text()}"
        )
        print("  OK: посторонний не проходит дальше отказа")
    finally:
        await bot.session.close()


async def scenario_text_without_llm(data_dir: Path) -> None:
    """Без LLM_API_KEY текст получает понятный ответ, а не ошибку."""
    cfg = make_cfg(data_dir)
    dp = build_dispatcher(cfg, TicketStore(data_dir / "tickets.json"), llm_client=None)
    bot = RecordingBot()
    try:
        await feed(dp, bot, make_update("не работает принтер"))
        text = bot.last_text()
        assert "не настроен" in text.lower(), f"нет пояснения про LLM: {text}"
        assert "/sd_status" in text, f"нет подсказки по командам: {text}"
        print("  OK: без LLM бот отвечает подсказкой, а не падает")
    finally:
        await bot.session.close()


async def scenario_sd_disabled(data_dir: Path) -> None:
    """При выключенном мониторинге команды /sd_* честно сообщают об этом."""
    cfg = make_cfg(data_dir, sd_enabled=False)
    dp = build_dispatcher(cfg, TicketStore(data_dir / "tickets.json"), sd_watcher=None)
    bot = RecordingBot()
    try:
        await feed(dp, bot, make_update("/sd_status"))
        assert "отключен" in bot.last_text().lower(), f"нет сообщения об отключении: {bot.last_text()}"
        print("  OK: /sd_status без SD-мониторинга сообщает об отключении")
    finally:
        await bot.session.close()


async def scenario_stranger_can_get_id(data_dir: Path) -> None:
    """/id доступен постороннему: иначе он не узнает свой ID для запроса доступа."""
    cfg = make_cfg(data_dir)
    dp = build_dispatcher(cfg, TicketStore(data_dir / "tickets.json"))
    bot = RecordingBot()
    try:
        await feed(dp, bot, make_update("/id", user_id=STRANGER, chat_id=STRANGER))
        text = bot.last_text()
        assert "Доступ ограничен" not in text, f"/id не должен блокироваться: {text}"
        assert str(STRANGER) in text, f"/id не показал ID постороннего: {text}"
        print("  OK: посторонний может узнать свой ID через /id")

        bot.reset()
        await feed(dp, bot, make_update("/id@ServMS_bot", user_id=STRANGER, chat_id=STRANGER, message_id=7))
        assert str(STRANGER) in bot.last_text(), f"форма /id@bot не распознана: {bot.last_text()}"
        print("  OK: /id@бот тоже пропускается")
    finally:
        await bot.session.close()


async def scenario_help_matches_capabilities(data_dir: Path) -> None:
    """Справка не обещает диалог с ИИ, если он не подключён."""
    store = TicketStore(data_dir / "tickets.json")

    off = build_dispatcher(make_cfg(data_dir), store, llm_client=None)
    bot = RecordingBot()
    try:
        await feed(off, bot, make_update("/help"))
        text = bot.last_text()
        assert "не подключён" in text, f"без LLM справка должна это говорить: {text}"
        assert "создам заявку" not in text, f"без LLM нельзя обещать создание заявки: {text}"
        print("  OK: без LLM справка честно говорит об отключённом диалоге")
    finally:
        await bot.session.close()

    on = build_dispatcher(make_cfg(data_dir), store, llm_client=object())
    bot = RecordingBot()
    try:
        await feed(on, bot, make_update("/help"))
        text = bot.last_text()
        assert "напиши, в чём проблема" in text.lower(), f"с LLM справка должна приглашать к диалогу: {text}"
        assert "не подключён" not in text, f"с LLM не должно быть отказа: {text}"
        print("  OK: с LLM справка приглашает описать проблему")
    finally:
        await bot.session.close()


async def scenario_many_messages(data_dir: Path) -> None:
    """Несколько сообщений подряд обрабатываются по порядку, без потерь."""
    cfg = make_cfg(data_dir)
    dp = build_dispatcher(cfg, TicketStore(data_dir / "tickets.json"))
    bot = RecordingBot()
    try:
        commands = ["/help", "/id", "/my", "/help", "/my"]
        for index, command in enumerate(commands, start=1):
            before = len(bot.calls)
            await feed(dp, bot, make_update(command, message_id=index))
            assert len(bot.calls) == before + 1, (
                f"на «{command}» пришло {len(bot.calls) - before} ответов вместо одного"
            )
        assert len(bot.calls) == len(commands), "часть сообщений осталась без ответа"
        print(f"  OK: {len(commands)} сообщений подряд — столько же ответов, без потерь")
    finally:
        await bot.session.close()


async def scenario_name_escaping(data_dir: Path) -> None:
    """Имя из профиля не ломает HTML-разметку приветствия."""
    cfg = make_cfg(data_dir)
    dp = build_dispatcher(cfg, TicketStore(data_dir / "tickets.json"))
    bot = RecordingBot()
    try:
        await feed(dp, bot, make_update("/start", first_name="<b>Ваня"))
        text = bot.last_text()
        assert "&lt;b&gt;Ваня" in text, f"имя не экранировано: {text}"
        print("  OK: имя с тегом экранируется в приветствии")
    finally:
        await bot.session.close()


async def main() -> None:
    base = ROOT / ".tmp-test-updates"
    print("Интеграционная проверка обработчиков бота")
    for title, scenario in (
        ("[разрешённый пользователь]", scenario_allowed_user),
        ("[посторонний]", scenario_stranger_denied),
        ("[посторонний и /id]", scenario_stranger_can_get_id),
        ("[открытый бот]", scenario_open_bot),
        ("[админские команды]", scenario_admin_commands),
        ("[текст без LLM]", scenario_text_without_llm),
        ("[SD выключен]", scenario_sd_disabled),
        ("[справка про ИИ]", scenario_help_matches_capabilities),
        ("[много сообщений подряд]", scenario_many_messages),
        ("[экранирование имени]", scenario_name_escaping),
    ):
        print(f"\n{title}")
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True)
        await scenario(base)
    shutil.rmtree(base, ignore_errors=True)
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
