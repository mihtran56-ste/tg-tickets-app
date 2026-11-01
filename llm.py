"""Работа с языковой моделью через OpenAI-совместимый API для бота заявок."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger("tickets")


class LLMClient:
    """Тонкая обёртка над чат-моделью."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        system_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        timeout: float = 60.0,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout,
            max_retries=2,
        )
        self._model = model
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens

    @classmethod
    def from_config(cls, cfg: "Config") -> "LLMClient":
        return cls(
            api_key=cfg.llm_api_key,
            base_url=cfg.llm_base_url,
            model=cfg.llm_model,
            system_prompt=cfg.system_prompt,
            temperature=cfg.llm_temperature,
            max_tokens=cfg.llm_max_tokens,
        )

    async def ask(self, history: list[dict[str, str]]) -> str:
        """Отправляет историю диалога модели и возвращает текст ответа."""
        messages = [{"role": "system", "content": self._system_prompt}, *history]
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        if not response.choices:
            return ""
        content = response.choices[0].message.content
        return (content or "").strip()

    async def ask_with_tools(
        self,
        history: list[dict[str, str]],
        tools: list[dict[str, Any]],
        executor: Callable[[str, dict[str, Any]], Awaitable[str]],
        *,
        system_extra: str = "",
        max_rounds: int = 5,
    ) -> str:
        """Диалог с инструментами: модель сама решает, что вызвать."""
        system = self._system_prompt
        if system_extra:
            system = f"{system}\n\n{system_extra}"

        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *history]
        for round_index in range(max_rounds):
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=tools,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
            if not response.choices:
                return ""

            message = response.choices[0].message
            if not message.tool_calls:
                return (message.content or "").strip()

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )
            for call in message.tool_calls:
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                log.info("Инструмент: %s(%s)", call.function.name, arguments)
                result = await executor(call.function.name, arguments)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": str(result)}
                )

        return "Не удалось получить ответ: слишком много шагов с инструментами."

    async def close(self) -> None:
        await self._client.close()