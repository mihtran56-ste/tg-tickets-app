"""Проверка SLA-логики: границы, часовой пояс, тексты и отчёт «сегодня».

Запуск:  python tests/test_sla.py
Сети, Telegram и pytest не требует.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sd_client import SDRequest  # noqa: E402
from sd_sla import (  # noqa: E402
    SLA_EXPIRED,
    SLA_WARN,
    digest_line,
    format_digest,
    format_sla_notification,
    human_left,
    resolve_timezone,
    sla_stage,
    today_blocks,
)

TZ = resolve_timezone("Europe/Moscow")


def at(day: int = 1, hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
    """Время в поясе SD (Москва) для проверок."""
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TZ)


def make_req(
    number: int = 1,
    deadline: datetime | None = None,
    *,
    code: str = "SUTSPROD-17353334",
    address: str = "603124, Нижний Новгород, пер Камчатский, 7",
    client: str = "produkty",
    request_type: str = "Поддержка касс",
    ab_spot: str | None = None,
    resolved: str | None = None,
) -> SDRequest:
    return SDRequest(
        sys_id=f"{number:032x}",
        number=f"REQ{4300000 + number}",
        short_description=code,
        assigned_user="Иванов И.И.",
        sys_created_at="01.11.2026 10:00",
        state="В работе",
        multicard_comment_ing="",
        multicard_request_type=request_type,
        multicard_name_client=client,
        multicard_terminal_address=address,
        multicard_deadline=(
            deadline.strftime("%d.%m.%Y %H:%M") if deadline is not None else None
        ),
        ab_spot_address=ab_spot,
        resolved_at=resolved,
        deadline_at=deadline,
    ).with_base_url("https://sd.example.ru")


def scenario_stage_boundaries() -> None:
    deadline = at(hour=18)
    warn = timedelta(hours=2)

    assert sla_stage(deadline, at(hour=15, minute=59, second=59), warn) is None
    assert sla_stage(deadline, at(hour=16, minute=0, second=0), warn) == SLA_WARN
    assert sla_stage(deadline, at(hour=17, minute=59, second=59), warn) == SLA_WARN
    assert sla_stage(deadline, at(hour=18), warn) == SLA_EXPIRED
    assert sla_stage(deadline, at(hour=18, minute=0, second=1), warn) == SLA_EXPIRED
    assert sla_stage(None, at(hour=18), warn) is None, "без дедлайна SLA не считается"
    assert sla_stage(deadline, at(hour=17), timedelta(0)) is None, (
        "порог 0 должен выключать предупреждение, но не просрочку"
    )
    assert sla_stage(deadline, at(hour=18), timedelta(0)) == SLA_EXPIRED
    print("  OK: ровно 2 часа — предупреждение, ровно дедлайн — просрочка")


def scenario_human_left() -> None:
    assert human_left(timedelta(seconds=41)) == "41 с"
    assert human_left(timedelta(seconds=90)) == "1 мин"
    assert human_left(timedelta(hours=1)) == "1 ч 0 мин"
    assert human_left(timedelta(hours=1, seconds=60)) == "1 ч 1 мин"
    assert human_left(timedelta(hours=25)) == "1 дн 1 ч"
    assert human_left(-timedelta(minutes=5)) == "0 с", "отрицательный остаток не показываем"
    print("  OK: остаток времени читается по-человечески")


def scenario_warn_message() -> None:
    req = make_req(1, at(hour=18))
    text = format_sla_notification(req, SLA_WARN, at(hour=16, minute=30), "Europe/Moscow")

    assert text.startswith("⏳ <b>До конца SLA 2 ч</b>"), f"заголовок: {text[:60]!r}"
    assert "SUTSPROD-17353334 (REQ4300001)" in text, f"нет кода и REQ:\n{text}"
    assert "<b>Тип:</b> Поддержка касс" in text
    assert "<b>Клиент:</b> produkty" in text
    assert "<b>Адрес:</b> 603124, Нижний Новгород, пер Камчатский, 7" in text
    assert "<b>Дедлайн СУТС:</b> 01.11.2026 18:00" in text
    assert "<b>Осталось:</b> 1 ч 30 мин" in text
    assert 'href="https://sd.example.ru/record/itsm_request/' in text
    print("  OK: предупреждение содержит тип, клиента, адрес, дедлайн и ссылку")


def scenario_expired_message() -> None:
    req = make_req(2, at(hour=18))
    text = format_sla_notification(
        req, SLA_EXPIRED, at(hour=18, minute=30), "Europe/Moscow"
    )

    assert text.startswith("🔥 <b>Заявка сгорела по SLA</b>"), f"заголовок: {text[:60]!r}"
    assert "<b>Просрочено:</b> 30 мин" in text
    assert "<b>Дедлайн СУТС:</b> 01.11.2026 18:00" in text
    print("  OK: сообщение о просрочке показывает, на сколько сгорела")


def scenario_escaping() -> None:
    req = make_req(
        3,
        at(hour=18),
        request_type="Тип <b>&",
        client='Клиент & "Co"',
        address="<script>alert(1)</script>",
    )
    text = format_sla_notification(req, SLA_WARN, at(hour=17), "Europe/Moscow")

    assert "&lt;b&gt;" in text and "&amp;" in text, f"спецсимволы не экранированы:\n{text}"
    assert "&lt;script&gt;" in text
    assert "<script>" not in text, "живой тег из данных попал в сообщение"
    assert "<b>До конца SLA 2 ч</b>" in text, "своя разметка должна остаться"
    print("  OK: данные SD экранируются, разметка бота цела")


def scenario_missing_fields() -> None:
    req = make_req(4, at(hour=18), address="", ab_spot="Адрес точки", client="", request_type="")
    text = format_sla_notification(req, SLA_WARN, at(hour=17), "Europe/Moscow")

    assert "<b>Адрес:</b> Адрес точки" in text, "не сработал запасной адрес ab_spot_address"
    assert "<b>Клиент:</b> —" in text and "<b>Тип:</b> —" in text
    print("  OK: пустые поля показываются как «—», адрес берётся из ab_spot_address")


def scenario_today_report() -> None:
    now = at(hour=12)
    burned = make_req(10, at(hour=10))
    future = make_req(11, at(hour=18))
    tomorrow = make_req(12, at(day=2, hour=9))
    resolved = make_req(13, at(hour=11), resolved="01.11.2026 11:30")
    no_deadline = make_req(14, None)

    blocks = today_blocks(
        [future, burned, tomorrow, resolved, no_deadline], now, "Europe/Moscow"
    )
    header = blocks[0]
    text = "\n\n".join(blocks)

    assert header == (
        "🔥 <b>Сгорят сегодня, 01.11.2026 — 2</b>\n❗ из них уже сгорели: 1"
    ), f"заголовок: {header!r}"
    assert text.index("10:00") < text.index("18:00"), "сортировка по времени сломана"
    assert "🔥 сгорела" in text and "⏳ осталось 6 ч 0 мин" in text
    assert "REQ4300012" not in text, "завтрашняя заявка не должна попадать в «сегодня»"
    assert "REQ4300013" not in text, "закрытая заявка не должна попадать в список"
    assert "REQ4300014" not in text, "без дедлайна заявка не попадает в список"
    assert text.count("Дедлайн СУТС: 01.11.2026") == 2
    print("  OK: отчёт «сегодня» сортирует по времени и помечает сгоревшие")


def scenario_today_timezone() -> None:
    """23:30 UTC — это уже 02:30 следующего дня в Москве."""
    now_utc = datetime(2026, 11, 1, 23, 30, tzinfo=timezone.utc)
    in_msk_today = make_req(20, datetime(2026, 11, 2, 3, 0))
    yesterday_msk = make_req(21, datetime(2026, 11, 1, 23, 0))

    blocks = today_blocks([in_msk_today, yesterday_msk], now_utc, "Europe/Moscow")
    text = "\n\n".join(blocks)

    assert "02.11.2026 — 1" in text, f"граница суток не в поясе SD:\n{text}"
    assert "REQ4300020" in text and "REQ4300021" not in text
    print("  OK: «сегодня» считается по часовому поясу SD, а не сервера")


def scenario_digest() -> None:
    """Дайджест: ссылки, дата создания, дедлайн и ограничение строк."""
    first = make_req(40, at(hour=18), code="SUTSPROD-10001")
    second = make_req(41, at(day=2, hour=10), code="SUTSPROD-10002")
    no_deadline = make_req(42, None, code="SUTSPROD-10003")

    text = format_digest(
        [first, second, no_deadline], "Пропущенные заявки", "Europe/Moscow", limit=2
    )

    assert text.startswith("🆕 <b>Пропущенные заявки: 3</b>"), f"заголовок:\n{text}"
    assert "SUTSPROD-10001" in text and "SUTSPROD-10002" in text
    assert "создана 01.11.2026 10:00" in text
    assert "дедлайн 01.11.2026 18:00" in text
    assert "… и ещё 1 — /sd_dump" in text, f"нет пометки об остатке:\n{text}"
    assert 'href="https://sd.example.ru/record/itsm_request/' in text

    line = digest_line(no_deadline, "Europe/Moscow")
    assert "дедлайн —" in line, f"пустой дедлайн:\n{line}"
    assert format_digest([], "Пусто", "Europe/Moscow") == ""
    print("  OK: дайджест показывает ссылки, даты и остаток, пустой не создаётся")


def scenario_empty_today() -> None:
    blocks = today_blocks([make_req(30, at(day=3, hour=9))], at(hour=12), "Europe/Moscow")
    assert "— 0" in blocks[0], f"заголовок пустого отчёта: {blocks[0]!r}"
    assert "ничего не сгорает" in blocks[1]
    print("  OK: пустой отчёт «сегодня» понятен пользователю")


def scenario_bad_timezone() -> None:
    try:
        resolve_timezone("Mars/Base")
    except ValueError as exc:
        assert "SD_TIMEZONE" in str(exc), f"ошибка не подсказывает переменную: {exc}"
        print("  OK: неизвестная зона даёт понятную ошибку")
    else:
        raise AssertionError("ожидали ValueError на неизвестной зоне")


def main() -> None:
    print("Проверка SLA-логики")
    for title, scenario in (
        ("[границы 2 часа и дедлайн]", scenario_stage_boundaries),
        ("[человеческий остаток]", scenario_human_left),
        ("[предупреждение]", scenario_warn_message),
        ("[просрочка]", scenario_expired_message),
        ("[экранирование]", scenario_escaping),
        ("[пустые поля]", scenario_missing_fields),
        ("[отчёт «сегодня»]", scenario_today_report),
        ("[дайджест]", scenario_digest),
        ("[пояс SD в отчёте]", scenario_today_timezone),
        ("[пустой отчёт]", scenario_empty_today),
        ("[неизвестная зона]", scenario_bad_timezone),
    ):
        print(f"\n{title}")
        scenario()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    main()
