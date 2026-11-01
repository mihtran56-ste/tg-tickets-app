"""Проверка разбора ответа ServiceDesk (SimpleOne).

Главный сценарий: поле status приходит обёрнутым словарём, и в уведомление
должно попадать «В работе», а не дамп {'database_value': ...}.

Запуск:  python tests/test_parsing.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from sd_client import (  # noqa: E402
    SDClient,
    extract_subject_code,
    format_sd_datetime,
    parse_sd_datetime,
    unwrap_value,
)

# Так реально выглядит запись из /v1/list/itsm_request
RAW_ITEM = {
    "sys_id": "179345698101390162",
    "number": {"value": "REQ4308671", "color": "", "read_access": True},
    "short_description": {"value": "SUTSPROD-17353334", "color": ""},
    "assigned_user": {
        "value": {
            "database_value": "171500255301498833",
            "display_value": "Игорь Храмов (Khramov.IK)",
            "entity_table_name": "employee",
        },
        "table_name": "employee",
    },
    "sys_created_at": {"value": "2026-10-31 14:29:41", "color": ""},
    "state": {
        "value": {"database_value": "in_progress", "display_value": "В работе"},
        "color": "green",
    },
    "planning_state": {
        "value": {"database_value": "planned", "display_value": "Запланирована"},
        "color": "",
    },
    "multicard_name_client": {"value": "produkty", "color": ""},
    "multicard_pos": {"value": "82251125906536", "color": ""},
    "multicard_terminal_address": {"value": "603124,Нижний Новгород,пер Камчатский, 7"},
    "multicard_deadline": {"value": "2026-11-01 10:40:53"},
    "multicard_comment_ing": {"value": ""},
    "hold_code": {"value": ""},
}


def make_client() -> SDClient:
    return SDClient(
        Config(bot_token="1:x", allowed_user_ids=frozenset({1}), data_dir=ROOT)
    )


def scenario_unwrap() -> None:
    assert unwrap_value({"value": "текст"}) == "текст"
    assert unwrap_value({"value": {"display_value": "В работе"}}) == "В работе"
    assert unwrap_value({"value": {"database_value": "in_progress", "display_value": "В работе"}}) == "В работе"
    assert unwrap_value({"value": {"database_value": "in_progress"}}) == "in_progress"
    assert unwrap_value(None) == ""
    assert unwrap_value({"value": ""}, "по умолчанию") == "по умолчанию"
    assert unwrap_value("просто строка") == "просто строка"
    # Сам словарь не должен попадать в результат
    result = unwrap_value({"value": {"database_value": "a", "display_value": "b"}})
    assert "{" not in result and "database_value" not in result, f"остался дамп словаря: {result}"
    print("  OK: значения разворачиваются из любой обёртки, дампы не проходят")


def scenario_datetime() -> None:
    assert format_sd_datetime("2026-10-31 14:29:41") == "31.10.2026 14:29"
    assert format_sd_datetime("2026-10-31 14:29") == "31.10.2026 14:29"
    assert format_sd_datetime("2026-10-31") == "31.10.2026"
    assert format_sd_datetime("31.10.2026 14:30") == "31.10.2026 14:30", "знакомый формат не портим"
    assert format_sd_datetime("ерунда") == "ерунда", "неизвестный формат возвращается как есть"
    assert format_sd_datetime("") == ""
    print("  OK: даты приводятся к «31.10.2026 14:29», неизвестные не теряются")


def scenario_deadline_parse() -> None:
    """Для SLA нужен datetime с секундами, а не строка для показа."""
    from datetime import datetime

    expected = datetime(2026, 11, 1, 10, 40, 53)
    assert parse_sd_datetime("2026-11-01 10:40:53") == expected
    assert parse_sd_datetime("2026-11-01 10:40") == datetime(2026, 11, 1, 10, 40)
    assert parse_sd_datetime("2026-11-01") == datetime(2026, 11, 1)
    assert parse_sd_datetime("01.11.2026 10:40") == datetime(2026, 11, 1, 10, 40)
    assert parse_sd_datetime("ерунда") is None, "неизвестный формат — не дедлайн"
    assert parse_sd_datetime("") is None
    print("  OK: дедлайн разбирается в datetime с секундами, мусор — None")


def scenario_parse_item() -> None:
    client = make_client()
    request = client._parse_item(RAW_ITEM)

    assert request is not None, "запись не разобралась"
    assert request.number == "REQ4308671"
    assert request.short_description == "SUTSPROD-17353334"
    assert request.assigned_user == "Игорь Храмов (Khramov.IK)", f"исполнитель: {request.assigned_user}"
    assert request.state == "В работе", f"статус: {request.state!r}"
    assert request.planning_state == "Запланирована", f"планирование: {request.planning_state!r}"
    assert request.sys_created_at == "31.10.2026 14:29", f"дата создания: {request.sys_created_at!r}"
    assert request.multicard_deadline == "01.11.2026 10:40", f"дедлайн: {request.multicard_deadline!r}"
    assert request.deadline_at == datetime(2026, 11, 1, 10, 40, 53), (
        f"дедлайн для SLA: {request.deadline_at!r}"
    )
    assert request.with_base_url("https://sd.example.ru").deadline_at == request.deadline_at, (
        "deadline_at потерялся при подстановке base_url"
    )
    assert request.multicard_pos == "82251125906536"
    print("  OK: запись разбирается в человеческие значения")


def scenario_notification() -> None:
    client = make_client()
    request = client._parse_item(RAW_ITEM).with_base_url("https://sd.servionica.ru")
    text = request.format_notification(23)

    assert "database_value" not in text, f"в уведомлении остался дамп словаря:\n{text}"
    assert "display_value" not in text, f"в уведомлении остался дамп словаря:\n{text}"
    # В теме лежит рабочий код — он и должен быть заголовком
    assert "<b>Новая заявка: SUTSPROD-17353334</b>" in text, f"нет кода в заголовке:\n{text}"
    assert "REQ4308671" not in text, f"REQ-номер не нужен в уведомлении:\n{text}"
    assert "<b>Статус:</b> В работе" in text, f"статус не человеческий:\n{text}"
    assert "<b>Планирование:</b> Запланирована" in text, f"планирование не человеческое:\n{text}"
    assert "31.10.2026 14:29" in text, f"дата не отформатирована:\n{text}"
    assert 'href="https://sd.servionica.ru/record/itsm_request/' in text, "ссылка должна вести на /record/"
    print("  OK: заголовок — только код SUTSPROD, без REQ")


def scenario_code_detection() -> None:
    assert extract_subject_code("SUTSPROD-17353334") == "SUTSPROD-17353334"
    assert extract_subject_code("SUTSPROD - 12345") == "SUTSPROD - 12345", "пробелы вокруг дефиса допустимы"
    assert extract_subject_code("Не работает принтер") == "", "обычный текст — не код"
    assert extract_subject_code("") == ""
    assert extract_subject_code("Очень длинная тема заявки без кода" * 2) == "", "длинный текст — не код"
    print("  OK: код определяется в теме, обычный текст не принимается за код")


def scenario_notification_without_code() -> None:
    plain = dict(RAW_ITEM)
    plain["short_description"] = {"value": "Не работает принтер"}
    client = make_client()
    request = client._parse_item(plain).with_base_url("https://sd.servionica.ru")
    text = request.format_notification()

    assert "<b>Новая заявка: REQ4308671</b>" in text, f"без кода заголовок — REQ:\n{text}"
    assert "<b>Тема:</b> Не работает принтер" in text, f"тема потерялась:\n{text}"
    assert "Номер в SD" not in text, "без кода не должно быть строки про REQ"
    print("  OK: без кода в теме показывается REQ и строка «Тема»")


def main() -> None:
    print("Проверка разбора ответа ServiceDesk")
    for title, scenario in (
        ("[разворачивание обёрток]", scenario_unwrap),
        ("[формат дат]", scenario_datetime),
        ("[дедлайн для SLA]", scenario_deadline_parse),
        ("[разбор записи]", scenario_parse_item),
        ("[текст уведомления]", scenario_notification),
        ("[код в теме]", scenario_code_detection),
        ("[без кода]", scenario_notification_without_code),
    ):
        print(f"\n{title}")
        scenario()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    main()
