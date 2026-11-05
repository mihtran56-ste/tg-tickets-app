"""Проверка разбора настроек: числа, флаги, ID и папка данных.

Запуск:  python tests/test_config.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as config_module  # noqa: E402
from config import _bool_env, _float_env, _int_env, _parse_ids  # noqa: E402


def with_env(**values: str | None):
    """Временно выставляет переменные окружения и возвращает функцию отката."""
    previous = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def restore() -> None:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return restore


def scenario_ids() -> None:
    assert _parse_ids("1,2,3") == frozenset({1, 2, 3})
    assert _parse_ids(" 1 ; 2 ") == frozenset({1, 2}), "точка с запятой должна работать как разделитель"
    assert _parse_ids("") == frozenset(), "пустая строка — пустой набор"
    assert _parse_ids("1,,2,") == frozenset({1, 2}), "пустые куски игнорируются"
    print("  OK: ID разбираются из запятых и точек с запятой")

    try:
        _parse_ids("1, ой")
    except ValueError as exc:
        assert "не число" in str(exc), f"непонятная ошибка: {exc}"
        assert "ой" in str(exc), f"в ошибке нет проблемного значения: {exc}"
        print("  OK: нечисловой ID даёт понятную ошибку")
    else:
        raise AssertionError("ожидали ValueError на нечисловом ID")


def scenario_bool() -> None:
    for raw in ("1", "true", "yes", "on", "да", "вкл", "TRUE"):
        restore = with_env(TEST_FLAG=raw)
        try:
            assert _bool_env("TEST_FLAG", False) is True, f"«{raw}» должно быть True"
        finally:
            restore()
    for raw in ("0", "false", "no", "off", "нет"):
        restore = with_env(TEST_FLAG=raw)
        try:
            assert _bool_env("TEST_FLAG", True) is False, f"«{raw}» должно быть False"
        finally:
            restore()
    restore = with_env(TEST_FLAG=None)
    try:
        assert _bool_env("TEST_FLAG", True) is True, "пустое значение — берётся default"
    finally:
        restore()
    print("  OK: флаги понимают true/да/вкл и ложные значения")


def scenario_numbers() -> None:
    restore = with_env(TEST_INT="5")
    try:
        assert _int_env("TEST_INT", 60, 10) == 10, "минимум не применяется"
    finally:
        restore()

    restore = with_env(TEST_INT="abc")
    try:
        try:
            _int_env("TEST_INT", 60, 10)
        except ValueError as exc:
            assert "целое число" in str(exc), f"непонятная ошибка: {exc}"
        else:
            raise AssertionError("ожидали ValueError на нечисловом значении")
    finally:
        restore()

    restore = with_env(TEST_FLOAT="0,3")
    try:
        assert abs(_float_env("TEST_FLOAT", 0.7, 0.0, 2.0) - 0.3) < 1e-9, "запятая как разделитель не работает"
    finally:
        restore()

    restore = with_env(TEST_FLOAT="5")
    try:
        assert _float_env("TEST_FLOAT", 0.7, 0.0, 2.0) == 2.0, "верхняя граница не применяется"
    finally:
        restore()

    restore = with_env(TEST_FLOAT="abc")
    try:
        try:
            _float_env("TEST_FLOAT", 0.7, 0.0, 2.0)
        except ValueError as exc:
            assert "нужно число" in str(exc), f"непонятная ошибка: {exc}"
        else:
            raise AssertionError("ожидали ValueError на нечисловом значении")
    finally:
        restore()
    print("  OK: числа ограничиваются границами, запятая понимается, мусор отклоняется")


def scenario_missing_token(tmp: Path) -> None:
    original_base = config_module.BASE_DIR
    restore_env = with_env(BOT_TOKEN=None, DATA_DIR=None, PORT=None)
    config_module.BASE_DIR = tmp
    try:
        try:
            config_module.load_config()
        except ValueError as exc:
            assert "BOT_TOKEN" in str(exc), f"ошибка не упоминает токен: {exc}"
            print("  OK: без BOT_TOKEN запуск отклоняется с понятным текстом")
        else:
            raise AssertionError("ожидали ValueError без BOT_TOKEN")
    finally:
        config_module.BASE_DIR = original_base
        restore_env()


def scenario_ok(tmp: Path) -> None:
    original_base = config_module.BASE_DIR
    restore_env = with_env(
        BOT_TOKEN="123456789:AA-test",
        ALLOWED_USER_IDS="1,2",
        ADMIN_USER_IDS="2",
        DATA_DIR=None,
        PORT="8080",
        SD_CHECK_INTERVAL="5",
        SD_AUTH_METHOD=None,
        SD_MAX_FAILURES=None,
        SD_PAUSE_NOTIFY=None,
        SD_PROXY_PORT=None,
    )
    config_module.BASE_DIR = tmp
    try:
        cfg = config_module.load_config()
        assert cfg.bot_token == "123456789:AA-test"
        assert cfg.allowed_user_ids == frozenset({1, 2})
        assert cfg.admin_user_ids == frozenset({2})
        assert cfg.data_dir == tmp / "data", f"папка данных по умолчанию: {cfg.data_dir}"
        assert cfg.data_dir.exists(), "папка данных не создана"
        assert cfg.health_port == 8080, f"порт не разобран: {cfg.health_port}"
        assert cfg.sd_check_interval == 10, "минимум интервала не применён"
        assert cfg.llm_api_key == "", "LLM по умолчанию выключен"
        assert cfg.sd_auth_method == "simpleone", f"метод входа по умолчанию: {cfg.sd_auth_method}"
        assert cfg.sd_max_failures == 5, f"порог неудач по умолчанию: {cfg.sd_max_failures}"
        assert cfg.sd_proxy_port == 0, f"SD-прокси по умолчанию выключен: {cfg.sd_proxy_port}"
        assert cfg.sd_pause_notify is True, "уведомление об автопаузе должно быть включено"
        assert cfg.sd_sla_enabled is True, "SLA-контроль должен быть включён по умолчанию"
        assert cfg.sd_sla_warn_hours == 2.0, f"порог SLA: {cfg.sd_sla_warn_hours}"
        assert cfg.sd_timezone == "Europe/Moscow", f"пояс SD: {cfg.sd_timezone}"
        print("  OK: настройки читаются, папка данных создаётся, PORT разбирается")
    finally:
        config_module.BASE_DIR = original_base
        restore_env()


def scenario_auth_settings(tmp: Path) -> None:
    """SD_AUTH_METHOD и SD_MAX_FAILURES: значения, минимум и опечатки."""
    original_base = config_module.BASE_DIR
    config_module.BASE_DIR = tmp
    restore_env = with_env(
        BOT_TOKEN="123456789:AA-test",
        DATA_DIR=None,
        PORT=None,
        SD_AUTH_METHOD="auto",
        SD_MAX_FAILURES="0",
        SD_PAUSE_NOTIFY="false",
        SD_PROXY_PORT="8765",
    )
    try:
        cfg = config_module.load_config()
        assert cfg.sd_auth_method == "auto", f"метод: {cfg.sd_auth_method}"
        assert cfg.sd_max_failures == 1, f"минимум порога не применён: {cfg.sd_max_failures}"
        assert cfg.sd_proxy_port == 8765, f"порт SD-прокси: {cfg.sd_proxy_port}"
        assert cfg.sd_pause_notify is False, "SD_PAUSE_NOTIFY=false не разобран"
        print("  OK: SD_AUTH_METHOD, минимум SD_MAX_FAILURES и флаг уведомления работают")
    finally:
        config_module.BASE_DIR = original_base
        restore_env()

    restore_env = with_env(
        BOT_TOKEN="123456789:AA-test",
        DATA_DIR=None,
        PORT=None,
        SD_AUTH_METHOD="login_v2",
        SD_MAX_FAILURES=None,
        SD_PAUSE_NOTIFY=None,
        SD_PROXY_PORT=None,
    )
    config_module.BASE_DIR = tmp
    try:
        try:
            config_module.load_config()
        except ValueError as exc:
            assert "SD_AUTH_METHOD:" in str(exc), f"ошибка не упоминает метод: {exc}"
            assert "get_params" in str(exc), f"нет списка допустимых значений: {exc}"
            print("  OK: неверный SD_AUTH_METHOD отклоняется с понятным текстом")
        else:
            raise AssertionError("ожидали ValueError на неизвестном SD_AUTH_METHOD")
    finally:
        config_module.BASE_DIR = original_base
        restore_env()


def scenario_sla_settings(tmp: Path) -> None:
    """Порог SLA понимает дробные часы, а опечатка в поясе не проходит молча."""
    original_base = config_module.BASE_DIR
    restore_env = with_env(
        BOT_TOKEN="123456789:AA-test",
        DATA_DIR=None,
        PORT=None,
        SD_SLA_ENABLED="false",
        SD_SLA_WARN_HOURS="0,5",
        SD_TIMEZONE="Europe/Moscow",
    )
    config_module.BASE_DIR = tmp
    try:
        cfg = config_module.load_config()
        assert cfg.sd_sla_enabled is False, "SD_SLA_ENABLED=false не разобран"
        assert cfg.sd_sla_warn_hours == 0.5, f"порог: {cfg.sd_sla_warn_hours}"
        print("  OK: порог SLA понимает дробные часы, флаг выключает контроль")
    finally:
        config_module.BASE_DIR = original_base
        restore_env()

    restore_env = with_env(
        BOT_TOKEN="123456789:AA-test",
        DATA_DIR=None,
        PORT=None,
        SD_TIMEZONE="Mars/Base",
    )
    config_module.BASE_DIR = tmp
    try:
        try:
            config_module.load_config()
        except ValueError as exc:
            assert "SD_TIMEZONE" in str(exc), f"ошибка не упоминает пояс: {exc}"
            print("  OK: неизвестный часовой пояс даёт понятную ошибку при старте")
        else:
            raise AssertionError("ожидали ValueError на неизвестном часовом поясе")
    finally:
        config_module.BASE_DIR = original_base
        restore_env()


def scenario_bad_data_dir(tmp: Path) -> None:
    original_base = config_module.BASE_DIR
    blocker = tmp / "не_папка"
    blocker.write_text("я файл", encoding="utf-8")
    restore_env = with_env(
        BOT_TOKEN="123456789:AA-test",
        DATA_DIR=str(blocker / "data"),
        PORT=None,
    )
    config_module.BASE_DIR = tmp
    try:
        try:
            config_module.load_config()
        except ValueError as exc:
            assert "DATA_DIR" in str(exc), f"ошибка не упоминает DATA_DIR: {exc}"
            print("  OK: недоступная папка данных даёт понятную ошибку")
        else:
            raise AssertionError("ожидали ValueError при недоступной DATA_DIR")
    finally:
        config_module.BASE_DIR = original_base
        restore_env()


def main() -> None:
    base = ROOT / ".tmp-test-config"
    print("Проверка разбора настроек")
    for title, scenario in (
        ("[ID пользователей]", lambda _p: scenario_ids()),
        ("[флаги]", lambda _p: scenario_bool()),
        ("[числа]", lambda _p: scenario_numbers()),
    ):
        print(f"\n{title}")
        scenario(base)
    print("\n[без токена]")
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    scenario_missing_token(base / "no_token")
    print("\n[корректные настройки]")
    (base / "ok").mkdir(parents=True, exist_ok=True)
    scenario_ok(base / "ok")
    print("\n[недоступная DATA_DIR]")
    (base / "bad").mkdir(parents=True, exist_ok=True)
    scenario_bad_data_dir(base / "bad")
    print("\n[SLA-настройки]")
    (base / "sla").mkdir(parents=True, exist_ok=True)
    scenario_sla_settings(base / "sla")
    print("\n[авторизация SD]")
    (base / "auth").mkdir(parents=True, exist_ok=True)
    scenario_auth_settings(base / "auth")
    shutil.rmtree(base, ignore_errors=True)
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    main()
