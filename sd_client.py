"""ServiceDesk клиент для получения заявок.

Работает с ITSM на базе ServiceNow (sd.servionica.ru).
Авторизация через форму логина (session + cookies).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp

from config import Config

log = logging.getLogger("sd_client")

# Регулярка для извлечения user_token из страницы логина (ServiceNow pattern)
USER_TOKEN_RE = re.compile(r'name="user_token"\s+value="([^"]+)"')
# Регулярка для sys_id в ссылках
SYS_ID_RE = re.compile(r"sys_id=([a-f0-9]{32})")

# Что нельзя показывать в логах: токены сессии и пароли
_SECRET_PATTERNS = (
    re.compile(r'("?auth_key"?\s*[:=]\s*"?)([A-Za-z0-9_\-\.]{8,})', re.IGNORECASE),
    re.compile(r'("?[a-z_]*password"?\s*[:=]\s*"?)([^"&,\s}]{1,})', re.IGNORECASE),
)


CODE_RE = re.compile(r"^[A-Za-zА-Яа-я]{2,}\s*[-\s]\s*\d{3,}$")


def extract_subject_code(text: str) -> str:
    """Достаёт рабочий код заявки из «темы», если он там есть.

    В этом ServiceDesk тема выглядит как «SUTSPROD-17353334» — именно по нему
    заявку узнают, а не по REQ-номеру. Если тема обычная («Не работает принтер»),
    возвращается пустая строка.
    """
    value = (text or "").strip()
    if not value or len(value) > 40:
        return ""
    return value if CODE_RE.match(value) else ""


def extract_total(data: dict) -> int:
    """Достаёт общее количество по фильтру из ответа, если сервер его сообщает.

    Раньше это делалось только для одного эндпоинта — если рабочим оказывался
    другой, total оставался нулём и пагинация молча отключалась.
    """
    for candidate in (data.get("data"), data):
        if not isinstance(candidate, dict):
            continue
        pagination = candidate.get("pagination")
        if isinstance(pagination, dict):
            total = pagination.get("total")
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                return int(total)
    return 0


def unwrap_value(value: object, default: str = "") -> str:
    """Достаёт человекочитаемое значение из обёртки SimpleOne.

    Форматы, которые встречаются в ответах:
    - {"value": "текст", "color": "", ...}
    - {"value": {"database_value": "in_progress", "display_value": "В работе"}, ...}
    - готовые строки и None в старых эндпоинтах
    """
    current = value
    for _ in range(5):  # защита от неожиданной вложенности
        if current is None:
            return default
        if not isinstance(current, dict):
            text = str(current).strip()
            return text or default

        display = current.get("display_value")
        if isinstance(display, str) and display.strip():
            return display.strip()

        if "value" in current:
            current = current["value"]
            continue

        database = current.get("database_value")
        if database is not None:
            text = str(database).strip()
            return text or default

        return default

    return default


def format_sd_datetime(value: str, default: str = "") -> str:
    """Превращает «2026-10-31 14:29:41» в «31.10.2026 14:29».

    Если формат незнакомый — возвращает исходную строку, чтобы не терять данные.
    """
    text = (value or "").strip()
    if not text:
        return default

    for source, target in (
        ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"),
        ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"),
        ("%Y-%m-%d", "%d.%m.%Y"),
    ):
        try:
            return datetime.strptime(text, source).strftime(target)
        except ValueError:
            continue
    return text


def parse_sd_datetime(value: str) -> datetime | None:
    """Разбирает время из SD в datetime (без часового пояса).

    Нужен для SLA: в отличие от format_sd_datetime сохраняет секунды и не
    превращает неизвестный формат в строку — такое время просто не считается
    дедлайном, чтобы не присылать ложные алерты.
    """
    text = (value or "").strip()
    if not text:
        return None

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


MIN_REAL_DEADLINE_YEAR = 2000


def normalize_deadline(value: str) -> tuple[str | None, datetime | None]:
    """Возвращает дедлайн для показа и для расчёта SLA.

    SimpleOne отдаёт «01.01.1970 00:00», когда дедлайна нет. Это не срок:
    иначе SLA пришлёт ложную просрочку, а в уведомлении появится 1970 год.
    """
    text = (value or "").strip()
    if not text:
        return None, None

    parsed = parse_sd_datetime(text)
    if parsed is None or parsed.year < MIN_REAL_DEADLINE_YEAR:
        return None, None
    return format_sd_datetime(text) or None, parsed


def redact_secrets(text: str) -> str:
    """Прячет токены и пароли перед записью в лог.

    Логи на хостинге сохраняются и видны в панели, поэтому auth_key и пароль
    не должны попадать туда в открытом виде.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}<скрыто>", text)
    return text


@dataclass(frozen=True)
class SDRequest:
    """Заявка из SD."""

    # Безопасный дефолт: заполняется через with_base_url() после парсинга.
    # Без аннотации — намеренно, чтобы не стало полем dataclass.
    _base_url = ""

    sys_id: str
    number: str
    short_description: str
    assigned_user: str
    sys_created_at: str
    state: str
    multicard_comment_ing: str
    multicard_request_type: str | None = None
    multicard_name_client: str | None = None
    multicard_pos: str | None = None
    multicard_terminal_address: str | None = None
    multicard_deadline: str | None = None
    resolved_at: str | None = None
    planning_state: str | None = None
    ab_spot_address: str | None = None
    hold_code: str | None = None
    table: str = "itsm_request"
    # Дедлайн СУТС для расчёта SLA: сырое время без потери секунд.
    deadline_at: datetime | None = None

    @property
    def url(self) -> str:
        """Ссылка на заявку в SD.

        SimpleOne открывает записи маршрутом SPA `/record/{таблица}/{sys_id}` —
        это видно в коде самого приложения: openRecord(essence, id) собирает
        «/record/» + essence + «/» + id. Прежний ServiceNow-формат
        `/nav_to.do?uri=itsm_request.do?sys_id=…` в SimpleOne даёт 404.
        """
        return f"{self._base_url}/record/{self.table}/{self.sys_id}"

    @property
    def code(self) -> str:
        """Рабочий код заявки из темы (например, «SUTSPROD-17353334»).

        Пустая строка, если тема — обычный текст.
        """
        return extract_subject_code(self.short_description)

    @property
    def display_name(self) -> str:
        """Как показывать заявку: код, если он есть, иначе REQ-номер."""
        return self.code or self.number

    def format_short(self, index: int = 0) -> str:
        """Компактная строка для списков (/sd_dump): код, статус, исполнитель, дата."""
        name = html.escape(self.display_name)
        link = html.escape(self.url)
        prefix = f"{index}. " if index else ""
        parts = [f'{prefix}<a href="{link}">{name}</a>']
        if self.state:
            parts.append(html.escape(self.state))
        if self.assigned_user:
            parts.append(html.escape(self.assigned_user))
        if self.sys_created_at:
            parts.append(html.escape(self.sys_created_at))
        return " | ".join(parts)

    def __post_init__(self):
        # Хак: base_url не в dataclass, добавим через object.__setattr__
        pass

    def with_base_url(self, base_url: str) -> "SDRequest":
        """Возвращает копию с установленным base_url для генерации ссылок."""
        obj = SDRequest(
            sys_id=self.sys_id,
            number=self.number,
            short_description=self.short_description,
            assigned_user=self.assigned_user,
            sys_created_at=self.sys_created_at,
            state=self.state,
            multicard_comment_ing=self.multicard_comment_ing,
            multicard_request_type=self.multicard_request_type,
            multicard_name_client=self.multicard_name_client,
            multicard_pos=self.multicard_pos,
            multicard_terminal_address=self.multicard_terminal_address,
            multicard_deadline=self.multicard_deadline,
            resolved_at=self.resolved_at,
            planning_state=self.planning_state,
            ab_spot_address=self.ab_spot_address,
            hold_code=self.hold_code,
            table=self.table,
            deadline_at=self.deadline_at,
        )
        object.__setattr__(obj, "_base_url", base_url.rstrip("/"))
        return obj

    def format_notification(self, total_count: int = 0) -> str:
        """Форматирует заявку для уведомления в Telegram.

        Telegram разбирает HTML в тексте сообщения, а данные приходят из SD
        как есть: один «&» или «<» в теме — и сообщение будет отвергнуто
        с ошибкой разбора сущностей. Поэтому все значения экранируются.
        """

        def esc(value: object) -> str:
            return html.escape(str(value)) if value is not None else ""

        lines = [f"🆕 <b>Новая заявка: {esc(self.display_name)}</b>"]

        # Если в теме лежит рабочий код (SUTSPROD-…), он и есть заголовок:
        # REQ-номер и повтор темы пользователю не нужны — он открывает заявку
        # по ссылке ниже. Если кода нет, показываем REQ и саму тему.
        if not self.code:
            lines.append(f"📋 <b>Тема:</b> {esc(self.short_description)}")

        lines.extend([
            f"👤 <b>Исполнитель:</b> {esc(self.assigned_user)}",
            f"🕒 <b>Создана:</b> {esc(self.sys_created_at)}",
            f"📍 <b>Статус:</b> {esc(self.state)}",
        ])
        if total_count:
            lines.append(f"📊 <b>Всего по фильтру:</b> {esc(total_count)}")

        if self.multicard_comment_ing:
            comment = self.multicard_comment_ing[:300]
            if len(self.multicard_comment_ing) > 300:
                comment += "..."
            lines.append(f"💬 <b>Комментарий:</b> {esc(comment)}")

        if self.multicard_request_type:
            lines.append(f"🏷 <b>Тип:</b> {esc(self.multicard_request_type)}")

        if self.multicard_name_client:
            lines.append(f"🏢 <b>Клиент:</b> {esc(self.multicard_name_client)}")

        if self.multicard_pos:
            lines.append(f"🏪 <b>POS:</b> {esc(self.multicard_pos)}")

        if self.multicard_terminal_address:
            lines.append(f"📍 <b>Адрес терминала:</b> {esc(self.multicard_terminal_address)}")

        if self.multicard_deadline:
            lines.append(f"⏰ <b>Дедлайн СУТС:</b> {esc(self.multicard_deadline)}")

        if self.planning_state:
            lines.append(f"📅 <b>Планирование:</b> {esc(self.planning_state)}")

        lines.append(f"🔗 <a href=\"{html.escape(self.url)}\">Открыть в SD</a>")

        return "\n".join(lines)


class SDClient:
    """Клиент для работы с ServiceDesk API."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.base_url = cfg.sd_base_url.rstrip("/")
        self._login = cfg.sd_login
        self._password = cfg.sd_password
        self._verify_ssl = cfg.sd_verify_ssl
        self.session: aiohttp.ClientSession | None = None
        self._logged_in = False
        self._auth_key: str | None = None
        self._login_lock = asyncio.Lock()
        # Один релогин на вызов fetch_requests: не устраиваем серию неудачных
        # логинов при истёкшей сессии (это видно службе безопасности SD).
        self._relogin_attempted = False
        # Рабочий эндпоинт, найденный ранее — чтобы не перебирать все заново
        self._working_endpoint: str | None = None
        # Поддерживает ли сервер постраничную выдачу (проверяем один раз)
        self._pagination_supported: bool | None = None

    async def __aenter__(self) -> "SDClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def _ensure_session(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            # Проверка сертификата обязательна: через это соединение уходят
            # логин и пароль. Отключать можно только при явной необходимости
            # (SD_VERIFY_SSL=false), например при самоподписанном сертификате.
            connector = aiohttp.TCPConnector(limit=10, ssl=self._verify_ssl)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self._logged_in = False

    def _auth_methods(self) -> dict:
        """Все способы входа: имя из SD_AUTH_METHOD -> метод."""
        return {
            "simpleone": self._login_via_simpleone_api,
            "api_json": self._login_via_api_json,
            "auth_do": self._login_via_auth_do,
            "form": self._login_via_form,
            "get_params": self._login_via_get_params,
        }

    async def login(self) -> tuple[bool, str]:
        """Авторизация в SD выбранным методом (SD_AUTH_METHOD).

        Возвращает (успех, причина). Причина "auth" означает явный отказ в
        учётных данных: дальше стучаться нельзя, иначе в SD копится серия
        неудачных логинов под личной учёткой.
        """
        async with self._login_lock:
            if self._logged_in:
                return True, ""
            await self._ensure_session()

            methods = self._auth_methods()
            if self.cfg.sd_auth_method == "auto":
                order = list(methods.values())
            else:
                chosen = methods.get(self.cfg.sd_auth_method)
                if chosen is None:
                    return False, f"неизвестный SD_AUTH_METHOD: {self.cfg.sd_auth_method}"
                order = [chosen]

            last_reason = "unknown"
            for method in order:
                try:
                    ok, reason = await method()
                except asyncio.TimeoutError:
                    last_reason = "таймаут соединения (SD не ответил за 30 с)"
                    log.debug("Метод %s: таймаут", method.__name__)
                    continue
                # ClientSSLError — подкласс ClientConnectorError, поэтому
                # проверяем TLS раньше: иначе причина «ошибка TLS» недостижима.
                except aiohttp.ClientSSLError as exc:
                    last_reason = f"ошибка TLS: {exc}"
                    log.debug("Метод %s: ssl error", method.__name__)
                    continue
                except aiohttp.ClientConnectorError as exc:
                    last_reason = f"не удалось подключиться: {exc}"
                    log.debug("Метод %s: connector error", method.__name__)
                    continue
                except Exception as exc:
                    text = str(exc).strip()
                    last_reason = (f"{type(exc).__name__}: {text}"
                                   if text else type(exc).__name__)
                    log.debug("Метод %s упал: %s", method.__name__, last_reason)
                    continue

                if ok:
                    return True, ""
                last_reason = reason or "unknown"
                if reason == "auth":
                    # Неверный пароль — дальше стучаться нет смысла
                    return False, "auth"
            return False, last_reason

    async def _login_via_api_json(self) -> tuple[bool, str]:
        """JSON API авторизация: /api/now/auth/login (ServiceNow style)."""
        assert self.session is not None
        auth_url = urljoin(self.base_url, "/api/now/auth/login")
        async with self.session.post(auth_url, json={"username": self._login, "password": self._password}) as resp:
            log.info("POST %s -> HTTP %s", auth_url, resp.status)
            if resp.status == 401:
                return False, "auth"
            if resp.status == 200:
                data = await resp.json()
                if data.get("result", {}).get("session_id"):
                    self._logged_in = True
                    log.info("Успешная авторизация через /api/now/auth/login")
                    return True, ""
                return False, "session_id не найден в ответе"
            return False, f"HTTP {resp.status}"

    async def _login_via_simpleone_api(self) -> tuple[bool, str]:
        """SimpleOne API авторизация: /v1/auth/login."""
        assert self.session is not None
        auth_url = urljoin(self.base_url, "/v1/auth/login")
        payload = {"username": self._login, "password": self._password}

        async with self.session.post(auth_url, json=payload) as resp:
            log.info("POST %s -> HTTP %s", auth_url, resp.status)
            text = await resp.text()
            log.info("RAW ответ от %s: %s", auth_url, redact_secrets(text[:500]))
            if resp.status == 401:
                return False, "auth"
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            data = json.loads(text)
            # Ищем auth_key в ответе
            auth_key = data.get("data", {}).get("auth_key")
            if auth_key:
                self._auth_key = auth_key
                self._logged_in = True
                log.info("Успешная авторизация через /v1/auth/login, auth_key получен")
                return True, ""
            log.warning("auth_key не найден в ответе: %s", redact_secrets(str(data)))
            return False, "auth_key не найден в ответе"

    async def _login_via_auth_do(self) -> tuple[bool, str]:
        """Альтернативный эндпоинт: /auth.do."""
        assert self.session is not None
        auth_url = urljoin(self.base_url, "/auth.do")
        payload = {
            "user_name": self._login,
            "user_password": self._password,
            "remember_me": "true",
        }
        async with self.session.post(auth_url, data=payload) as resp:
            log.info("POST %s -> HTTP %s", auth_url, resp.status)
            if resp.status == 401:
                return False, "auth"
            if resp.status in (200, 302):
                text = await resp.text()
                if "logout" in text.lower() or "sign out" in text.lower() or resp.status == 302:
                    self._logged_in = True
                    log.info("Успешная авторизация через /auth.do")
                    return True, ""
                return False, "auth"
            return False, f"HTTP {resp.status}"

    async def _login_via_form(self) -> tuple[bool, str]:
        """Стандартная форма логина: GET /login.do + POST на action из формы."""
        assert self.session is not None

        # 1. Получаем страницу логина
        login_page_url = urljoin(self.base_url, "/login.do")
        async with self.session.get(login_page_url) as resp:
            log.info("GET %s -> HTTP %s", login_page_url, resp.status)
            if resp.status == 401:
                return False, "auth"
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            login_page = await resp.text()

        # DEBUG: сохраняем HTML для анализа (не критично для работы — не роняем логин)
        try:
            debug_path = self.cfg.data_dir / "login_page.html"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(login_page, encoding="utf-8")
            log.info("HTML страницы логина сохранён: %s (%d байт)", debug_path, len(login_page))
        except OSError as exc:
            log.debug("Не удалось сохранить HTML страницы логина: %s", exc)

        # Извлекаем user_token (CSRF)
        match = USER_TOKEN_RE.search(login_page)
        user_token = match.group(1) if match else ""
        if not user_token:
            log.debug("user_token не найден на странице логина")
            return False, "user_token не найден на странице логина"
        log.debug("user_token: %s", user_token[:20] + "...")

        # Извлекаем action формы логина
        form_action = self._extract_form_action(login_page)
        log.info("Form action: %s", form_action)
        if not form_action:
            return False, "форма логина не найдена"

        # 2. POST логин на правильный URL
        login_url = urljoin(self.base_url, form_action)
        payload = {
            "user_name": self._login,
            "user_password": self._password,
            "remember_me": "true",
            "user_token": user_token,
        }

        async with self.session.post(login_url, data=payload) as resp:
            log.info("POST %s -> HTTP %s", login_url, resp.status)
            if resp.status == 401:
                return False, "auth"
            if resp.status not in (200, 302):
                return False, f"HTTP {resp.status}"
            text = await resp.text()
            if "logout" in text.lower() or "sign out" in text.lower() or resp.status == 302:
                self._logged_in = True
                log.info("Успешная авторизация через форму")
                return True, ""
            return False, "auth"

    def _extract_form_action(self, html: str) -> str | None:
        """Извлекает action из формы логина."""
        import re
        # Ищем <form ... action="...">
        match = re.search(r'<form[^>]*action\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
        if match:
            return match.group(1)
        # Fallback: ищем form с id или name
        match = re.search(r'<form[^>]*(?:id|name)\s*=\s*["\']login["\'][^>]*action\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    async def _login_via_get_params(self) -> tuple[bool, str]:
        """GET с параметрами: /login.do?user_name=...&user_password=..."""
        assert self.session is not None
        auth_url = urljoin(self.base_url, "/login.do")
        params = {
            "user_name": self._login,
            "user_password": self._password,
            "remember_me": "true",
        }
        async with self.session.get(auth_url, params=params) as resp:
            log.info("GET %s -> HTTP %s", auth_url, resp.status)
            if resp.status == 401:
                return False, "auth"
            if resp.status in (200, 302):
                text = await resp.text()
                if "logout" in text.lower() or "sign out" in text.lower() or resp.status == 302:
                    self._logged_in = True
                    log.info("Успешная авторизация через GET параметры")
                    return True, ""
                return False, "auth"
            return False, f"HTTP {resp.status}"

    async def fetch_requests(
        self,
        view: str = "Default",
        condition: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[SDRequest], int]:
        """Получает список заявок с заданным фильтром.

        Returns:
            tuple: (список заявок, общее количество по фильтру)
        """
        # Новый вызов — новая попытка релогина: не больше одной на проверку.
        self._relogin_attempted = False

        ok, reason = await self.login()
        if not ok:
            if reason == "auth":
                raise RuntimeError(
                    "Неверный логин или пароль SD (SD_LOGIN/SD_PASSWORD)")
            raise RuntimeError(f"SD недоступен: {reason}")

        await self._ensure_session()
        assert self.session is not None

        # Подготавливаем заголовки с auth_key
        headers = {"Accept": "application/json"}
        if self._auth_key:
            headers["X-Auth-Key"] = self._auth_key
            headers["Authorization"] = f"Bearer {self._auth_key}"

        # Пробуем разные варианты API эндпоинтов (SimpleOne сначала)
        endpoints = [
            # SimpleOne API v1 — разные форматы
            ("/v1/itsm_request", {
                "limit": limit,
                "offset": offset,
                "display_value": "true",
                "fields": "sys_id,number,short_description,assigned_user,sys_created_at,state,multicard_comment_ing,multicard_request_type,multicard_name_client,multicard_pos,multicard_terminal_address,multicard_deadline,resolved_at,planning_state,ab_spot_address,hold_code",
            }),
            ("/v1/table/itsm_request", {
                "limit": limit,
                "offset": offset,
            }),
            ("/v1/data/itsm_request", {
                "limit": limit,
                "offset": offset,
            }),
            ("/v1/records/itsm_request", {
                "limit": limit,
                "offset": offset,
            }),
            ("/v1/search/itsm_request", {
                "limit": limit,
                "offset": offset,
            }),
            ("/v1/list/itsm_request", {
                "limit": limit,
                "offset": offset,
            }),
            # ServiceNow Table API
            ("/api/now/table/itsm_request", {
                "sysparm_limit": limit,
                "sysparm_offset": offset,
                "sysparm_display_value": "true",
                "sysparm_exclude_reference_link": "true",
                "sysparm_fields": "sys_id,number,short_description,assigned_user,sys_created_at,state,multicard_comment_ing,multicard_request_type,multicard_name_client,multicard_pos,multicard_terminal_address,multicard_deadline,resolved_at,planning_state,ab_spot_address,hold_code",
            }),
            # Service Catalog API
            ("/api/sn_sc/servicecatalog/items", {}),
            # list.do (как в браузере)
            ("/list/itsm_request", {}),
        ]

        # Рабочий эндпоинт, найденный в прошлый раз, пробуем первым
        if self._working_endpoint:
            endpoints.sort(key=lambda item: item[0] != self._working_endpoint)

        last_error: str | None = None

        async def _fetch_page(
            endpoint: str,
            default_params: dict,
            page_number: int,
            retry: bool = True,
        ) -> tuple[list[SDRequest], int] | None:
            """Запрашивает одну страницу. None — эндпоинт не подошёл.

            SimpleOne листает параметром `page` (нумерация с 1) и игнорирует
            offset/limit: проверено на живом API — page=2 отдаёт остаток списка.
            Для ServiceNow-подобных эндпоинтов параллельно передаём offset.
            """
            nonlocal last_error
            url = urljoin(self.base_url, endpoint)
            params = dict(default_params)
            if view:
                params["sysparm_view"] = view
            if condition:
                params["condition"] = condition

            params["page"] = page_number
            page_offset = (page_number - 1) * 20  # сервер всё равно отдаёт не больше 20
            if "offset" in params:
                params["offset"] = page_offset
            if "sysparm_offset" in params:
                params["sysparm_offset"] = page_offset

            try:
                async with self.session.get(url, params=params, headers=headers) as resp:
                    text = await resp.text()
                    log.debug("Ответ %s: HTTP %s, %s...", endpoint, resp.status, text[:200])

                    if resp.status in (401, 403):
                        log.warning("Сессия истекла (401/403), пробуем релогин")
                        self._logged_in = False
                        self._auth_key = None
                        if retry and not self._relogin_attempted:
                            self._relogin_attempted = True
                            ok, reason = await self.login()
                            if ok:
                                return await _fetch_page(endpoint, default_params,
                                                         page_number, retry=False)
                            raise RuntimeError(
                                f"Сессия истекла, релогин не удался: {reason}")
                        last_error = f"HTTP {resp.status}"
                        return None

                    if resp.status != 200:
                        last_error = f"HTTP {resp.status}"
                        log.debug("Эндпоинт %s вернул %s", endpoint, resp.status)
                        return None

                    try:
                        data = json.loads(text)
                    except Exception:
                        log.warning("Эндпоинт %s вернул не JSON: %s...", endpoint, text[:200])
                        return None

                    items = self._parse_response(data, endpoint)
                    return items, extract_total(data)

            except aiohttp.ClientError as e:
                last_error = str(e)
                log.debug("Ошибка запроса к %s: %s", endpoint, e)
                return None

        for endpoint, default_params in endpoints:
            first = await _fetch_page(endpoint, default_params, 1)
            if first is None:
                continue

            items, total = first
            self._working_endpoint = endpoint
            log.info("Эндпоинт %s рабочий, заявок на странице: %d (всего %d)", endpoint, len(items), total)

            # Догружаем остальные страницы: сервер отдаёт не больше 20 за раз,
            # а по фильтру их может быть больше. Важно для полноты: если за
            # интервал придёт больше 20 новых заявок, часть уехала бы на
            # следующие страницы и не была бы замечена.
            # Если сервер не сообщает total, всё равно листаем: пока страницы
            # полные, вероятно есть продолжение.
            max_items = min(total, 200) if total else min(limit, 200)
            max_items = max(max_items, len(items))
            seen_sys_ids = {req.sys_id for req in items}
            page_number = 1
            page_size = len(items)
            while (
                len(items) < max_items
                and page_size > 0
                and page_number < 10
                and self._pagination_supported is not False
            ):
                page_number += 1
                nxt = await _fetch_page(endpoint, default_params, page_number)
                if not nxt or not nxt[0]:
                    break
                fresh = [req for req in nxt[0] if req.sys_id not in seen_sys_ids]
                if not fresh:
                    # Сервер не умеет листать — не долбим его каждую минуту
                    self._pagination_supported = False
                    log.info(
                        "Страница %d не дала новых записей (%d из %d). "
                        "Дальше берём только первую страницу.",
                        page_number,
                        len(items),
                        total,
                    )
                    break
                self._pagination_supported = True
                seen_sys_ids.update(req.sys_id for req in fresh)
                items.extend(fresh)
                page_size = len(nxt[0])
                log.info("Догружена страница %d: всего %d из %d", page_number, len(items), total)

            # Убираем возможные дубли между страницами
            unique: dict[str, SDRequest] = {}
            for req in items:
                unique.setdefault(req.sys_id, req)
            result = list(unique.values())
            if len(result) != len(items):
                log.info("После дедупликации заявок: %d", len(result))
            return result, total

        raise RuntimeError(f"Все эндпоинты вернули ошибку. Последняя: {last_error}")

    def _parse_response(self, data: dict, endpoint: str) -> list[SDRequest]:
        """Парсит ответ API в список SDRequest."""
        items = []

        # Разные форматы ответа для разных эндпоинтов
        if "result" in data and isinstance(data["result"], list):
            # Table API: { "result": [ {...}, {...} ] }
            raw_items = data["result"]
        elif "data" in data and "items" in data["data"]:
            # list.do JSON: { "data": { "items": [ {...}, {...} ] } }
            raw_items = data["data"]["items"]
        elif "items" in data:
            raw_items = data["items"]
        else:
            log.warning("Неизвестный формат ответа от %s: keys=%s", endpoint, list(data.keys()))
            return []

        for item in raw_items:
            try:
                req = self._parse_item(item)
                if req:
                    items.append(req.with_base_url(self.base_url))
            except Exception:
                log.exception("Ошибка парсинга заявки: %s", item.get("sys_id", "unknown"))

        log.info("Получено заявок: %d (эндпоинт: %s)", len(items), endpoint)
        return items

    def _parse_item(self, item: dict) -> SDRequest | None:
        """Парсит один элемент в SDRequest."""
        # Поля приходят в обёртке SimpleOne:
        #   {"value": {"database_value": ..., "display_value": ...}, "color": ...}
        # или {"value": "текст", ...}. Раньше разворачивался только один
        # уровень, и в уведомление попадал дамп внутреннего словаря.
        def get_val(key: str, default: str = "") -> str:
            return unwrap_value(item.get(key), default)

        def get_date(key: str, default: str = "") -> str:
            return format_sd_datetime(unwrap_value(item.get(key), default), default)

        sys_id = item.get("sys_id", "")
        number = get_val("number")
        if not number or not sys_id:
            return None

        deadline_display, deadline_at = normalize_deadline(
            unwrap_value(item.get("multicard_deadline"))
        )

        return SDRequest(
            sys_id=sys_id,
            number=number,
            short_description=get_val("short_description"),
            assigned_user=get_val("assigned_user"),
            sys_created_at=get_date("sys_created_at"),
            state=get_val("state"),
            multicard_comment_ing=get_val("multicard_comment_ing"),
            multicard_request_type=get_val("multicard_request_type") or None,
            multicard_name_client=get_val("multicard_name_client") or None,
            multicard_pos=get_val("multicard_pos") or None,
            multicard_terminal_address=get_val("multicard_terminal_address") or None,
            multicard_deadline=deadline_display,
            resolved_at=get_date("resolved_at") or None,
            planning_state=get_val("planning_state") or None,
            ab_spot_address=get_val("ab_spot_address") or None,
            hold_code=get_val("hold_code") or None,
            table=get_val("__table_name") or "itsm_request",
            deadline_at=deadline_at,
        )


async def test_sd_client(cfg: Config) -> None:
    """Тестовый запуск клиента."""
    print(f"Подключение к {cfg.sd_base_url}...")
    print(f"Логин: {cfg.sd_login}")

    async with SDClient(cfg) as client:
        requests, total = await client.fetch_requests(
            view=cfg.sd_view,
            condition=cfg.sd_condition,
            limit=10,
        )

        print(f"\nНайдено заявок: {len(requests)} (total={total})")
        for req in requests[:5]:
            print(f"\n--- {req.number} ---")
            print(req.format_notification(total_count=total))


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from config import load_config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

    try:
        cfg = load_config()
        if not cfg.sd_enabled:
            print("SD_ENABLED=false в .env — включи и заполни SD_LOGIN/SD_PASSWORD")
            sys.exit(1)
        asyncio.run(test_sd_client(cfg))
    except Exception as e:
        log.exception("Ошибка: %s", e)
        sys.exit(1)