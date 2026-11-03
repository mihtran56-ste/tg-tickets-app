"""Загрузка и проверка настроек из .env для бота заявок."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_SYSTEM_PROMPT = (
    "Ты — дежурный бот поддержки. Помогаешь пользователю с его заявками.\n"
    "Если пользователь описывает проблему — создай заявку через инструмент "
    "create_ticket, придумав краткий заголовок и понятное описание.\n"
    "Отвечай коротко, по-русски, без выдуманных номеров заявок: номера бери "
    "только из результатов инструментов."
)


@dataclass(frozen=True)
class Config:
    """Все настройки бота заявок в одном месте."""

    bot_token: str
    allowed_user_ids: frozenset[int]
    data_dir: Path
    telegram_api_ip: str = ""
    proxy_url: str = ""
    notify_on_start: bool = True
    notify_on_stop: bool = True
    notify_chat_ids: frozenset[int] = frozenset()
    admin_user_ids: frozenset[int] = frozenset()
    # ServiceDesk интеграция
    sd_enabled: bool = False
    sd_base_url: str = ""
    sd_login: str = ""
    sd_password: str = ""
    sd_check_interval: int = 60
    sd_proxy_port: int = 0
    sd_notify_chat_ids: frozenset[int] = frozenset()
    sd_view: str = "Default"
    sd_condition: str = ""
    sd_seed_on_first_run: bool = True
    sd_verify_ssl: bool = True
    # Авторизация в SD: один метод в бою, чтобы не плодить неудачные логины
    sd_auth_method: str = "simpleone"
    # После скольких неудачных проверок подряд — автопауза мониторинга
    sd_max_failures: int = 2
    sd_pause_notify: bool = True
    # SLA: предупреждение за N часов до дедлайна СУТС и сообщение о просрочке
    sd_sla_enabled: bool = True
    sd_sla_warn_hours: float = 2.0
    sd_timezone: str = "Europe/Moscow"
    # Хостинг
    health_port: int = 0
    health_token: str = ""
    # Отчёты о падении: минимальный простой и защита от спама
    outage_min_seconds: int = 10
    outage_cooldown_seconds: int = 600
    # LLM-диалог (без llm_api_key отключён)
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    system_prompt: str = ""
    llm_temperature: float = 0.7
    llm_max_tokens: int = 1024


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _parse_ids(raw: str) -> frozenset[int]:
    """Разбирает строку вида '123, 456' в набор ID."""
    result: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            result.add(int(chunk))
        except ValueError as exc:
            raise ValueError(
                f"ALLOWED_USER_IDS: «{chunk}» — это не число. "
                "Укажи Telegram ID (только цифры) через запятую."
            ) from exc
    return frozenset(result)


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}: «{raw}» — нужно целое число.") from exc
    return max(minimum, value)


def _float_env(name: str, default: float, low: float, high: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = float(raw.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{name}: «{raw}» — нужно число, например 0.7.") from exc
    return min(high, max(low, value))


def _data_dir() -> Path:
    """Папка данных: DATA_DIR из окружения или data/ рядом с кодом.

    На хостинге с постоянным томом указывается, например, DATA_DIR=/data.
    """
    raw = _env("DATA_DIR")
    if raw:
        return Path(raw).expanduser()
    return BASE_DIR / "data"


def _bool_env(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да", "вкл"}


def load_config() -> Config:
    """Читает .env и возвращает Config.

    Если чего-то не хватает — бросает ValueError с понятным списком проблем.
    """
    load_dotenv(BASE_DIR / ".env")

    problems: list[str] = []

    bot_token = _env("BOT_TOKEN")
    if not bot_token:
        problems.append(
            "BOT_TOKEN не задан — возьми токен у @BotFather и впиши в .env."
        )
    elif ":" not in bot_token:
        problems.append(
            "BOT_TOKEN не похож на токен бота (ожидается вид 123456789:AA...)."
        )

    if problems:
        raise ValueError("\n".join(f"• {problem}" for problem in problems))

    # Часовой пояс проверяем до создания папок: опечатка в SD_TIMEZONE иначе
    # всплывёт только на первом тике мониторинга, когда посчитать SLA уже нельзя.
    timezone_name = _env("SD_TIMEZONE", "Europe/Moscow") or "Europe/Moscow"
    try:
        from sd_sla import resolve_timezone

        resolve_timezone(timezone_name)
    except ValueError as exc:
        problems.append(str(exc))

    sd_auth_method = _env("SD_AUTH_METHOD", "simpleone") or "simpleone"
    if sd_auth_method not in {
        "simpleone",
        "api_json",
        "auth_do",
        "form",
        "get_params",
        "auto",
    }:
        problems.append(
            "SD_AUTH_METHOD: допустимые значения — simpleone, api_json, "
            "auth_do, form, get_params, auto."
        )

    if problems:
        raise ValueError("\n".join(f"• {problem}" for problem in problems))

    data_dir = _data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            f"DATA_DIR «{data_dir}» недоступна для записи: {exc}. "
            "Укажи существующую папку или убери DATA_DIR из .env."
        ) from exc

    return Config(
        bot_token=bot_token,
        allowed_user_ids=_parse_ids(_env("ALLOWED_USER_IDS")),
        admin_user_ids=_parse_ids(_env("ADMIN_USER_IDS")),
        data_dir=data_dir,
        telegram_api_ip=_env("TELEGRAM_API_IP"),
        proxy_url=_env("PROXY_URL"),
        notify_on_start=_bool_env("NOTIFY_ON_START", True),
        notify_on_stop=_bool_env("NOTIFY_ON_STOP", True),
        notify_chat_ids=_parse_ids(_env("NOTIFY_CHAT_IDS")),
        # ServiceDesk
        sd_enabled=_bool_env("SD_ENABLED", False),
        sd_base_url=_env("SD_BASE_URL", "").rstrip("/"),
        sd_login=_env("SD_LOGIN"),
        sd_password=_env("SD_PASSWORD"),
        sd_check_interval=_int_env("SD_CHECK_INTERVAL", 60, 10),
        sd_proxy_port=_int_env("SD_PROXY_PORT", 0, 0),
        sd_notify_chat_ids=_parse_ids(_env("SD_NOTIFY_CHAT_IDS")),
        sd_view=_env("SD_VIEW", "Default"),
        sd_condition=_env("SD_CONDITION"),
        sd_seed_on_first_run=_bool_env("SD_SEED_ON_FIRST_RUN", True),
        sd_verify_ssl=_bool_env("SD_VERIFY_SSL", True),
        sd_auth_method=sd_auth_method,
        sd_max_failures=_int_env("SD_MAX_FAILURES", 2, 1),
        sd_pause_notify=_bool_env("SD_PAUSE_NOTIFY", True),
        sd_sla_enabled=_bool_env("SD_SLA_ENABLED", True),
        sd_sla_warn_hours=_float_env("SD_SLA_WARN_HOURS", 2.0, 0.0, 72.0),
        sd_timezone=timezone_name,
        # Хостинг
        health_port=_int_env("PORT", 0, 0),
        health_token=_env("HEALTH_TOKEN"),
        outage_min_seconds=_int_env("OUTAGE_MIN_SECONDS", 10, 5),
        outage_cooldown_seconds=_int_env("OUTAGE_COOLDOWN_SECONDS", 600, 60),
        # LLM
        llm_api_key=_env("LLM_API_KEY"),
        llm_base_url=_env("LLM_BASE_URL"),
        llm_model=_env("LLM_MODEL", "gpt-4o-mini"),
        system_prompt=_env("LLM_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        llm_temperature=_float_env("LLM_TEMPERATURE", 0.7, 0.0, 2.0),
        llm_max_tokens=_int_env("LLM_MAX_TOKENS", 1024, 64),
    )