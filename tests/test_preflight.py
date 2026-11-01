"""Проверка скрипта готовности к деплою (preflight).

Сети не требует: живые проверки подменяются функциями.

Запуск:  python tests/test_preflight.py
"""

from __future__ import annotations

import asyncio
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from preflight import (  # noqa: E402
    Check,
    config_checks,
    print_report,
    sd_check,
    telegram_check,
)


def make_cfg(**overrides: object) -> Config:
    base: dict[str, object] = {
        "bot_token": "123456789:AA-test-token-value",
        "allowed_user_ids": frozenset({1001}),
        "data_dir": ROOT,
        "sd_enabled": True,
        "sd_base_url": "https://sd.example.ru",
        "sd_login": "ivanov",
        "sd_password": "secret",
        "sd_condition": "state=1",
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def by_name(checks: list[Check], name: str) -> Check:
    found = [c for c in checks if c.name == name]
    assert found, f"нет проверки «{name}» среди {[c.name for c in checks]}"
    return found[0]


def scenario_config_checks() -> None:
    checks = config_checks(make_cfg())
    assert by_name(checks, "BOT_TOKEN").ok, "корректный токен не прошёл"
    assert by_name(checks, "Настройки SD").ok, "полные настройки SD не прошли"
    assert by_name(checks, "Проверка сертификата SD").ok, "SSL должен быть включён по умолчанию"
    print("  OK: корректные настройки проходят проверку")

    bad_token = config_checks(make_cfg(bot_token="не-токен"))
    assert not by_name(bad_token, "BOT_TOKEN").ok, "битый токен прошёл проверку"
    assert by_name(bad_token, "BOT_TOKEN").critical, "плохой токен должен быть критичным"
    print("  OK: битый токен — критичная проблема")

    no_users = config_checks(make_cfg(allowed_user_ids=frozenset()))
    assert not by_name(no_users, "ALLOWED_USER_IDS").ok, "пустой список не отмечен"
    assert not by_name(no_users, "ALLOWED_USER_IDS").critical, "пустой список — всего лишь предупреждение"
    print("  OK: пустой ALLOWED_USER_IDS — предупреждение, а не блокер")

    no_condition = config_checks(make_cfg(sd_condition=""))
    assert not by_name(no_condition, "Настройки SD").ok, "пустой фильтр не отмечен"
    assert "SD_CONDITION" in by_name(no_condition, "Настройки SD").detail, "не указано, чего не хватает"
    print("  OK: пустой фильтр SD блокирует запуск")

    insecure = config_checks(make_cfg(sd_verify_ssl=False))
    ssl_check = by_name(insecure, "Проверка сертификата SD")
    assert not ssl_check.ok and not ssl_check.critical, "отключённый SSL — предупреждение"
    print("  OK: отключённая проверка сертификата видна как предупреждение")

    disabled = config_checks(make_cfg(sd_enabled=False))
    assert not any(c.name == "Настройки SD" for c in disabled), "при SD_ENABLED=false проверки SD не нужны"
    print("  OK: при выключенном мониторинге требования SD не проверяются")


async def scenario_sd_check() -> None:
    async def ok_fetch() -> tuple[int, int]:
        return 20, 23

    result = await sd_check(make_cfg(), fetch=ok_fetch)
    assert result.ok and "20 из 23" in result.detail, f"успешная проверка SD: {result}"
    print("  OK: успешный вход в SD отмечен")

    async def empty_fetch() -> tuple[int, int]:
        return 0, 0

    result = await sd_check(make_cfg(), fetch=empty_fetch)
    assert not result.ok, "пустой фильтр должен считаться проблемой"
    assert "SD_CONDITION" in result.detail, f"нет подсказки про фильтр: {result.detail}"
    print("  OK: пустой результат SD — проблема с подсказкой про фильтр")

    async def broken_fetch() -> tuple[int, int]:
        raise RuntimeError("Не удалось авторизоваться в SD")

    result = await sd_check(make_cfg(), fetch=broken_fetch)
    assert not result.ok, "ошибка SD не отмечена"
    assert "авторизоваться" in result.detail, f"текст ошибки потерян: {result.detail}"
    print("  OK: ошибка входа в SD показывается человеческим текстом")

    skipped = await sd_check(make_cfg(sd_enabled=False), fetch=broken_fetch)
    assert skipped.ok and not skipped.critical, "при выключенном SD проверка пропускается"
    print("  OK: при SD_ENABLED=false проверка SD пропускается")


async def scenario_telegram_check() -> None:
    async def ok_get_me() -> str:
        return "ServMS_bot"

    result = await telegram_check(make_cfg(), get_me=ok_get_me)
    assert result.ok and "@ServMS_bot" in result.detail, f"успешная проверка Telegram: {result}"
    print("  OK: валидный токен отмечен с именем бота")

    async def broken_get_me() -> str:
        raise RuntimeError("Unauthorized")

    result = await telegram_check(make_cfg(), get_me=broken_get_me)
    assert not result.ok, "неверный токен не отмечен"
    print("  OK: неверный токен Telegram показан как проблема")


def scenario_report() -> None:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        ok = print_report([Check("A", True, "норма")])
    assert ok is True, "при отсутствии проблем итог должен быть положительным"
    assert "всё в порядке" in buffer.getvalue()

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        ok = print_report([Check("A", False, "сломано")])
    assert ok is False, "при критичной проблеме итог отрицательный"

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        ok = print_report([Check("A", False, "мелочь", critical=False)])
    assert ok is True, "предупреждение не должно блокировать запуск"
    assert "предупреждения" in buffer.getvalue()
    print("  OK: итог различает провалы и предупреждения")


def scenario_state_checks(tmp: Path) -> None:
    """Локальные файлы: работает ли бот, свежи ли проверки, есть ли очередь."""
    import json
    import shutil
    import time

    from preflight import state_checks

    def prepare(case: str) -> Path:
        data_dir = tmp / case
        shutil.rmtree(data_dir, ignore_errors=True)
        data_dir.mkdir(parents=True)
        return data_dir

    # 1. Живой бот: сердцебиение только что
    data_dir = prepare("alive")
    (data_dir / "runtime.json").write_text(
        json.dumps({"last_alive": time.time(), "started_at": time.time(), "clean_stop": False}),
        encoding="utf-8",
    )
    check = next(c for c in state_checks(make_cfg(data_dir=data_dir)) if c.name == "Бот сейчас")
    assert check.ok and "работает" in check.detail, f"живой бот: {check}"
    print("  OK: живой бот с свежим сердцебиением определяется как работающий")

    # 2. Умер и никто не поднял
    data_dir = prepare("dead")
    (data_dir / "runtime.json").write_text(
        json.dumps({"last_alive": time.time() - 900, "started_at": time.time() - 1800, "clean_stop": False}),
        encoding="utf-8",
    )
    check = next(c for c in state_checks(make_cfg(data_dir=data_dir)) if c.name == "Бот сейчас")
    assert not check.ok, "мёртвый бот должен подсвечиваться"
    assert "НЕ работает" in check.detail, f"нет явного предупреждения: {check.detail}"
    assert not check.critical, "это предупреждение, а не блокер"
    print("  OK: бот, которого никто не поднял, виден как «НЕ работает»")

    # 3. Штатная остановка
    data_dir = prepare("stopped")
    (data_dir / "runtime.json").write_text(
        json.dumps({"last_alive": time.time() - 600, "clean_stop": True}),
        encoding="utf-8",
    )
    check = next(c for c in state_checks(make_cfg(data_dir=data_dir)) if c.name == "Бот сейчас")
    assert check.ok and "штатно" in check.detail, f"штатная остановка: {check}"
    print("  OK: штатная остановка не считается проблемой")

    # 4. Очередь недоставленных
    data_dir = prepare("pending")
    (data_dir / "pending_events.json").write_text(
        json.dumps([{"kind": "outage", "down_since": 1, "up_at": 2}]), encoding="utf-8"
    )
    queued = [c for c in state_checks(make_cfg(data_dir=data_dir)) if c.name == "Недоставленные сообщения"]
    assert queued and not queued[0].ok, "недоставленные сообщения должны подсвечиваться"
    assert "1" in queued[0].detail
    print("  OK: очередь недоставленных видна")

    # 5. Свежесть проверок
    data_dir = prepare("stale")
    (data_dir / "sd_state.json").write_text(
        json.dumps({"last_check": time.time() - 7200, "seen_numbers": [], "seen_sys_ids": []}),
        encoding="utf-8",
    )
    state = [c for c in state_checks(make_cfg(data_dir=data_dir)) if c.name == "Состояние SD"]
    assert state and not state[0].ok, "давняя проверка должна подсвечиваться"
    print("  OK: устаревшая проверка SD видна")

    # 6. Всё чисто
    data_dir = prepare("clean")
    checks = state_checks(make_cfg(data_dir=data_dir))
    assert all(c.ok for c in checks), f"на чистом каталоге проблем быть не должно: {checks}"
    print("  OK: пустой каталог не даёт ложных тревог")


async def main() -> None:
    print("Проверка preflight")
    for title, scenario in (
        ("[настройки]", scenario_config_checks),
        ("[ServiceDesk]", scenario_sd_check),
        ("[Telegram]", scenario_telegram_check),
        ("[итоговый отчёт]", scenario_report),
    ):
        print(f"\n{title}")
        if asyncio.iscoroutinefunction(scenario):
            await scenario()
        else:
            scenario()

    import shutil as _shutil

    base = ROOT / ".tmp-test-preflight"
    _shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    print("\n[локальное состояние]")
    try:
        scenario_state_checks(base)
    finally:
        _shutil.rmtree(base, ignore_errors=True)
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
