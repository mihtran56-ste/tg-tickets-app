"""Проверка LLM-диалога без сети: подменяем клиент OpenAI на фейк.

Запуск:  python tests/test_llm.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from llm import LLMClient  # noqa: E402
from tools import TOOLS  # noqa: E402


# --- Фейки в формате OpenAI SDK -------------------------------------------

class FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict) -> None:
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, json.dumps(arguments, ensure_ascii=False))


class FakeMessage:
    def __init__(self, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message


class FakeResponse:
    def __init__(self, message: FakeMessage) -> None:
        self.choices = [FakeChoice(message)]


class FakeCompletions:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("лишний вызов модели: сценарий исчерпан")
        return self._responses.pop(0)


class FakeChat:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.completions = FakeCompletions(responses)


class FakeOpenAI:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.chat = FakeChat(responses)

    async def close(self) -> None:
        pass


def make_client(responses: list[FakeResponse]) -> LLMClient:
    client = LLMClient(
        api_key="test-key",
        base_url="",
        model="test-model",
        system_prompt="Ты тестовый бот.",
    )
    client._client = FakeOpenAI(responses)  # подмена транспорта
    return client


class RecordingExecutor:
    def __init__(self, result: str = "Заявка ABC12345 создана") -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        return self.result


# --- Сценарии --------------------------------------------------------------

async def scenario_tool_call_then_answer() -> None:
    """Модель просит создать заявку, получает результат и отвечает текстом."""
    responses = [
        FakeResponse(
            FakeMessage(
                tool_calls=[
                    FakeToolCall(
                        "call_1",
                        "create_ticket",
                        {
                            "title": "Не работает принтер",
                            "description": "В бухгалтерии не печатает",
                            "priority": "high",
                            "category": "technical",
                        },
                    )
                ]
            )
        ),
        FakeResponse(FakeMessage(content="Готово: заявка создана.")),
    ]
    client = make_client(responses)
    executor = RecordingExecutor()

    answer = await client.ask_with_tools(
        [{"role": "user", "content": "Не работает принтер в бухгалтерии"}],
        TOOLS,
        executor,
    )

    assert answer == "Готово: заявка создана.", f"неожиданный ответ: {answer!r}"
    assert len(executor.calls) == 1, f"инструмент должен вызваться один раз, было {len(executor.calls)}"
    name, arguments = executor.calls[0]
    assert name == "create_ticket", f"вызван не тот инструмент: {name}"
    assert arguments["title"] == "Не работает принтер"
    assert arguments["priority"] == "high"
    print("  OK: вызов инструмента разобран, аргументы дошли до исполнителя")

    calls = client._client.chat.completions.calls
    assert len(calls) == 2, f"ожидали два обращения к модели, было {len(calls)}"
    second_messages = calls[1]["messages"]
    roles = [m["role"] for m in second_messages]
    assert roles[0] == "system", "первое сообщение должно быть системным промптом"
    assert "tool" in roles, f"результат инструмента не передан модели: {roles}"
    tool_message = next(m for m in second_messages if m["role"] == "tool")
    assert tool_message["tool_call_id"] == "call_1"
    assert "ABC12345" in tool_message["content"]
    print("  OK: результат инструмента вернулся модели с правильным tool_call_id")


async def scenario_plain_answer() -> None:
    """Модель отвечает сразу, без инструментов."""
    client = make_client([FakeResponse(FakeMessage(content="  Привет! Чем помочь?  "))])
    executor = RecordingExecutor()

    answer = await client.ask_with_tools(
        [{"role": "user", "content": "привет"}], TOOLS, executor
    )

    assert answer == "Привет! Чем помочь?", f"ответ не очищен от пробелов: {answer!r}"
    assert not executor.calls, "инструменты не должны вызываться"
    print("  OK: текстовый ответ возвращается без вызова инструментов")


async def scenario_tool_limit() -> None:
    """Модель зациклилась на инструментах — возвращаем понятное сообщение."""
    looping = [
        FakeResponse(FakeMessage(tool_calls=[FakeToolCall(f"c{i}", "get_ticket", {"ticket_id": "X"})]))
        for i in range(5)
    ]
    client = make_client(looping)
    executor = RecordingExecutor(result="нет такой заявки")

    answer = await client.ask_with_tools(
        [{"role": "user", "content": "покажи заявку"}], TOOLS, executor, max_rounds=2
    )

    assert "слишком много шагов" in answer.lower(), f"неожиданный ответ: {answer!r}"
    assert len(executor.calls) == 2, f"должно быть ровно 2 шага, было {len(executor.calls)}"
    print("  OK: превышение лимита шагов даёт понятное сообщение")


async def scenario_broken_arguments() -> None:
    """Модель прислала битый JSON в аргументах — не падаем."""
    class BrokenCall(FakeToolCall):
        def __init__(self) -> None:
            super().__init__("call_x", "get_ticket", {})
            self.function.arguments = "{не json"

    client = make_client([
        FakeResponse(FakeMessage(tool_calls=[BrokenCall()])),
        FakeResponse(FakeMessage(content="Не смог разобрать запрос.")),
    ])
    executor = RecordingExecutor()

    answer = await client.ask_with_tools(
        [{"role": "user", "content": "покажи"}], TOOLS, executor
    )

    assert executor.calls == [("get_ticket", {})], f"битый JSON должен дать пустые аргументы: {executor.calls}"
    assert answer == "Не смог разобрать запрос."
    print("  OK: битый JSON в аргументах не роняет диалог")


def scenario_from_config() -> None:
    """LLMClient собирается из настроек, а без ключа бот его не создаёт."""
    cfg = Config(
        bot_token="1:x",
        allowed_user_ids=frozenset({1}),
        data_dir=ROOT,
        llm_api_key="key-123",
        llm_model="my-model",
        llm_temperature=0.3,
        llm_max_tokens=256,
        system_prompt="свой промпт",
    )
    client = LLMClient.from_config(cfg)
    assert client._model == "my-model"
    assert client._temperature == 0.3
    assert client._max_tokens == 256
    assert client._system_prompt == "свой промпт"
    print("  OK: настройки LLM_* попадают в клиент")

    empty = Config(bot_token="1:x", allowed_user_ids=frozenset({1}), data_dir=ROOT)
    assert empty.llm_api_key == "", "по умолчанию ключа нет — диалог должен быть выключен"
    print("  OK: без LLM_API_KEY диалог выключен")


async def main() -> None:
    print("Проверка LLM-диалога")
    print("\n[инструмент + ответ]")
    await scenario_tool_call_then_answer()
    print("\n[текстовый ответ]")
    await scenario_plain_answer()
    print("\n[лимит шагов]")
    await scenario_tool_limit()
    print("\n[битые аргументы]")
    await scenario_broken_arguments()
    print("\n[настройки]")
    scenario_from_config()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
