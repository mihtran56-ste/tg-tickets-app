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


class FakeResponse:
    """Ответ aiohttp, который можно использовать через async with."""

    def __init__(self, status: int = 200, text: str = "", json_data=None) -> None:
        self.status = status
        self._text = text
        self._json = json_data

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def text(self) -> str:
        return self._text

    async def json(self):
        return self._json


class FakeSession:
    """Подменяет aiohttp.ClientSession и записывает запросы."""

    def __init__(
        self,
        post_response: FakeResponse | None = None,
        get_response: FakeResponse | None = None,
        post_error: Exception | None = None,
    ) -> None:
        self.closed = False
        self.calls: list[tuple[str, str, dict]] = []
        self.post_response = post_response or FakeResponse()
        self.get_response = get_response or FakeResponse()
        self.post_error = post_error

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        if self.post_error is not None:
            raise self.post_error
        return self.post_response

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self.get_response


def make_client_cfg(**overrides: object) -> Config:
    base: dict[str, object] = {
        "bot_token": "1:x",
        "allowed_user_ids": frozenset({1}),
        "data_dir": ROOT,
        "sd_enabled": True,
        "sd_base_url": "https://sd.example.ru",
        "sd_login": "ivanov",
        "sd_password": "secret",
        "sd_condition": "state=1",
        "sd_auth_method": "simpleone",
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def auth_ok() -> FakeResponse:
    return FakeResponse(200, '{"data":{"auth_key":"test-auth-key"}}')


async def scenario_single_auth_method() -> None:
    """SD_AUTH_METHOD=simpleone: ровно один POST к /v1/auth/login."""
    client = SDClient(make_client_cfg())
    session = FakeSession(post_response=auth_ok())
    client.session = session  # type: ignore[assignment]

    ok, reason = await client.login()

    assert (ok, reason) == (True, "")
    assert len(session.calls) == 1, f"лишние запросы при входе: {session.calls}"
    method, url, _kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/v1/auth/login"), f"не тот эндпоинт: {url}"
    print("  OK: simpleone делает ровно один POST /v1/auth/login")


async def scenario_401_stops_auto() -> None:
    """401 при auto: (False, "auth") и никаких других методов."""
    client = SDClient(make_client_cfg(sd_auth_method="auto"))
    session = FakeSession(post_response=FakeResponse(401, ""))
    client.session = session  # type: ignore[assignment]

    ok, reason = await client.login()

    assert (ok, reason) == (False, "auth"), f"получили ({ok}, {reason})"
    assert len(session.calls) == 1, (
        f"после 401 были ещё попытки входа: {session.calls}"
    )
    print("  OK: 401 останавливает перебор методов даже при auto")


async def scenario_timeout() -> None:
    """Таймаут: login объясняет причину, fetch_requests — «SD недоступен»."""
    client = SDClient(make_client_cfg())
    session = FakeSession(post_error=asyncio.TimeoutError())
    client.session = session  # type: ignore[assignment]

    ok, reason = await client.login()

    assert ok is False
    assert reason.startswith("таймаут соединения"), f"причина: {reason}"

    try:
        await client.fetch_requests(view="Default", condition="state=1", limit=10)
    except RuntimeError as exc:
        text = str(exc)
        assert "SD недоступен:" in text, f"не тот текст ошибки: {text}"
        assert "таймаут соединения" in text, f"причина потерялась: {text}"
    else:
        raise AssertionError("ожидали RuntimeError при таймауте")
    print("  OK: таймаут различается и доходит до RuntimeError «SD недоступен»")


async def scenario_one_relogin_per_fetch() -> None:
    """401 в fetch_requests: не больше одного релогина за вызов."""
    client = SDClient(make_client_cfg())
    session = FakeSession(
        post_response=auth_ok(),
        get_response=FakeResponse(401, ""),
    )
    client.session = session  # type: ignore[assignment]

    try:
        await client.fetch_requests(view="Default", condition="state=1", limit=10)
    except RuntimeError as exc:
        text = str(exc)
        assert "401" in text or "Сессия" in text, f"не та ошибка: {text}"
    else:
        raise AssertionError("ожидали RuntimeError после 401")

    posts = [call for call in session.calls if call[0] == "POST"]
    assert len(posts) == 2, (
        f"логинов за вызов: {len(posts)} (ожидали 1 вход + 1 релогин)"
    )
    print("  OK: fetch_requests при 401 делает ровно один релогин")



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


def scenario_proxy_base() -> None:
    """С прокси HTTP-запросы идут на localhost, а ссылки остаются на SD."""
    proxied = SDClient(make_client_cfg(sd_proxy_port=8765))
    assert proxied.base_url == "https://sd.example.ru"
    assert proxied._http_base == "http://127.0.0.1:8765", proxied._http_base

    direct = SDClient(make_client_cfg())
    assert direct._http_base == "https://sd.example.ru", direct._http_base
    print("  OK: SDClient ходит через прокси, ссылки остаются на настоящий SD")


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
        ("[прокси-база]", scenario_proxy_base),
    ):
        print(f"\n{title}")
        scenario()

    print("\n[проверка сертификата]")
    await scenario_ssl_verification()

    print("\n[один метод авторизации]")
    await scenario_single_auth_method()
    print("\n[401 останавливает перебор]")
    await scenario_401_stops_auto()
    print("\n[таймаут]")
    await scenario_timeout()
    print("\n[один релогин на вызов]")
    await scenario_one_relogin_per_fetch()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
