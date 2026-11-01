"""Проверка обязательных настроек ServiceDesk перед запуском мониторинга.

Зачем: если на хостинге забыть заполнить SD_CONDITION, фильтр окажется пустым
и бот начнёт мониторить ВЕСЬ ServiceDesk — все заявки компании вместо нужного
среза. Лучше не запускать мониторинг вовсе и громко сказать, чего не хватает.
"""

from __future__ import annotations

from config import Config

# (имя переменной окружения, атрибут Config, что означает)
REQUIRED = (
    ("SD_BASE_URL", "sd_base_url", "адрес ServiceDesk"),
    ("SD_LOGIN", "sd_login", "логин"),
    ("SD_PASSWORD", "sd_password", "пароль"),
    ("SD_CONDITION", "sd_condition", "фильтр заявок"),
)


def missing_sd_settings(cfg: Config) -> list[str]:
    """Возвращает список незаполненных обязательных настроек SD."""
    missing: list[str] = []
    for env_name, attr, _meaning in REQUIRED:
        if not getattr(cfg, attr, ""):
            missing.append(env_name)
    return missing


def describe_missing(cfg: Config) -> str:
    """Человеческое объяснение, чего не хватает и почему это важно."""
    missing = missing_sd_settings(cfg)
    if not missing:
        return ""

    details = [
        f"{env_name} ({meaning})"
        for env_name, _attr, meaning in REQUIRED
        if env_name in missing
    ]
    text = "SD-мониторинг не запущен: не заданы " + ", ".join(details)

    if "SD_CONDITION" in missing:
        text += (
            ". Без фильтра бот мониторил бы весь ServiceDesk — "
            "заполни SD_CONDITION из ссылки фильтра в SD."
        )
    return text
