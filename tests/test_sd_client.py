"""Проверка маскирования секретов в логах.

Auth-ключ и пароль не должны попадать в логи хостинга в открытом виде.

Запуск:  python tests/test_sd_client.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from sd_client import SDClient, redact_secrets  # noqa: E402

KEY = "af152c000d93405646fa5f3213caf48a1483a1cb5bea6b04b840a30c6c151ebe"


def scenario_json_key() -> None:
    """Ключ из JSON-ответа авторизации скрывается, остальное остаётся."""
    raw = (
        '{"messages":[],"status":"OK",'
        f'"data":{{"auth_key":"{KEY}"}},"page_debug_id":"","timing":{{"before_echo":1.9}}}}'
    )
    masked = redact_secrets(raw)

    assert KEY not in masked, f"ключ остался в логе: {masked}"
    assert "<скрыто>" in masked, "нет отметки о скрытом значении"
    assert '"status":"OK"' in masked, "пропали полезные данные"
    assert "before_echo" in masked, "пропали служебные данные"
    print("  OK: auth_key в JSON скрывается, остальной ответ сохраняется")


def scenario_form_password() -> None:
    """Пароль в теле формы скрывается."""
    raw = "user_name=ivanov&user_password=S3cretPass!&remember_me=true"
    masked = redact_secrets(raw)

    assert "S3cretPass!" not in masked, f"пароль остался в логе: {masked}"
    assert "user_name=ivanov" in masked, "пропало имя пользователя"
    assert "remember_me=true" in masked, "пропали прочие поля"
    print("  OK: пароль из формы скрывается, логин остаётся")


def scenario_json_password() -> None:
    """Пароль в JSON-запросе скрывается."""
    raw = '{"username": "ivanov", "password": "Pa$$w0rd"}'
    masked = redact_secrets(raw)
    assert "Pa$$w0rd" not in masked, f"пароль остался: {masked}"
    assert "ivanov" in masked
    print("  OK: пароль в JSON скрывается")


def scenario_no_false_positives() -> None:
    """Обычный текст не портится."""
    cases = [
        "Проверка SD: всего 20 (total=23), новых 0",
        "password policy обновлена",
        "Эндпоинт /v1/list/itsm_request рабочий",
    ]
    for case in cases:
        assert redact_secrets(case) == case, f"текст испорчен: {redact_secrets(case)!r}"
    print("  OK: обычные сообщения не меняются")


async def scenario_ssl_verification() -> None:
    """Сертификат ServiceDesk проверяется: через соединение идут логин и пароль."""

    def client(verify: bool) -> SDClient:
        cfg = Config(
            bot_token="1:x",
            allowed_user_ids=frozenset({1}),
            data_dir=ROOT,
            sd_verify_ssl=verify,
        )
        return SDClient(cfg)

    verified = client(True)
    await verified._ensure_session()
    try:
        assert verified.session is not None
        setting = verified.session.connector._ssl
        assert setting is not False, "по умолчанию проверка сертификата должна быть включена"
        print("  OK: по умолчанию сертификат SD проверяется")
    finally:
        await verified.close()

    unverified = client(False)
    await unverified._ensure_session()
    try:
        assert unverified.session is not None
        assert unverified.session.connector._ssl is False, "SD_VERIFY_SSL=false не применяется"
        print("  OK: отключение проверки через SD_VERIFY_SSL=false работает")
    finally:
        await unverified.close()


async def main() -> None:
    print("Проверка клиента ServiceDesk")
    for title, scenario in (
        ("[ключ в JSON]", scenario_json_key),
        ("[пароль в форме]", scenario_form_password),
        ("[пароль в JSON]", scenario_json_password),
        ("[ложные срабатывания]", scenario_no_false_positives),
    ):
        print(f"\n{title}")
        scenario()

    print("\n[проверка сертификата]")
    await scenario_ssl_verification()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
