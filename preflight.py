"""Проверка готовности окружения к запуску/деплою.

Запуск:  python preflight.py

Проверяет:
1. настройки (токен, список пользователей, обязательные параметры SD);
2. вход в ServiceDesk и получение заявок;
3. валидность токена бота (getMe) через ту же сеть, что и бот.

Возвращает код 0, если всё в порядке, и 1, если есть проблемы.
В Telegram ничего не отправляет.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from config import Config, load_config  # noqa: E402
from sd_settings import missing_sd_settings  # noqa: E402


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = True


def config_checks(cfg: Config) -> list[Check]:
    """Проверки, для которых не нужна сеть."""
    checks: list[Check] = []

    token_ok = ":" in cfg.bot_token and len(cfg.bot_token) > 20
    checks.append(
        Check(
            "BOT_TOKEN",
            token_ok,
            "похож на токен BotFather" if token_ok else "не похож на токен (вид 123456:AA...)",
        )
    )

    if cfg.allowed_user_ids:
        checks.append(Check("ALLOWED_USER_IDS", True, f"{len(cfg.allowed_user_ids)} ID"))
    else:
        checks.append(
            Check(
                "ALLOWED_USER_IDS",
                False,
                "пусто — бот ответит любому, кто его найдёт",
                critical=False,
            )
        )

    if not cfg.sd_enabled:
        checks.append(Check("SD_ENABLED", True, "SD-мониторинг выключен", critical=False))
        return checks

    missing = missing_sd_settings(cfg)
    checks.append(
        Check(
            "Настройки SD",
            not missing,
            "заполнены" if not missing else f"не заданы: {', '.join(missing)}",
        )
    )

    method_ok = cfg.sd_auth_method in {
        "simpleone", "api_json", "auth_do", "form", "get_params", "auto"}
    checks.append(Check(
        "SD_AUTH_METHOD",
        method_ok and cfg.sd_auth_method != "auto",
        f"{cfg.sd_auth_method}" + (
            " — перебор 5 методов при каждом логине; "
            "зафиксируй рабочий метод для боевого режима"
            if cfg.sd_auth_method == "auto" else " (зафиксирован)"),
        critical=False,
    ))

    try:
        port_ok = 0 <= cfg.health_port <= 65535
    except TypeError:
        port_ok = False
    checks.append(
        Check(
            "PORT",
            port_ok,
            f"{cfg.health_port}" + ("" if cfg.health_port else " (health-эндпоинт выключен)"),
            critical=False,
        )
    )

    checks.append(
        Check(
            "Проверка сертификата SD",
            cfg.sd_verify_ssl,
            "включена" if cfg.sd_verify_ssl else "ОТКЛЮЧЕНА — логин идёт по непроверенному TLS",
            critical=False,
        )
    )

    checks.append(
        Check(
            "Прокси",
            not (cfg.proxy_url or cfg.telegram_api_ip),
            "не используется (нормально для хостинга за рубежом)"
            if not (cfg.proxy_url or cfg.telegram_api_ip)
            else f"используется: {cfg.proxy_url or cfg.telegram_api_ip}",
            critical=False,
        )
    )
    return checks


def _free_port() -> int:
    """Свободный локальный порт для временного SD-прокси."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def sd_check(cfg: Config, fetch=None) -> Check:
    """Живая проверка: вход в SD и получение списка заявок."""
    if not cfg.sd_enabled:
        return Check("ServiceDesk", True, "пропущено: SD_ENABLED=false", critical=False)

    proxy_runner = None
    client_cfg = cfg
    if fetch is None:
        if cfg.sd_proxy_port:
            from sd_proxy import start_sd_proxy

            # Отдельный временный порт: preflight может работать, пока бот
            # уже держит свой прокси на основном порту.
            proxy_port = _free_port()
            try:
                proxy_runner = await start_sd_proxy(
                    cfg.sd_base_url,
                    proxy_port,
                    verify_ssl=cfg.sd_verify_ssl,
                )
            except Exception as exc:  # noqa: BLE001
                return Check(
                    "ServiceDesk",
                    False,
                    f"не удалось запустить SD proxy: {type(exc).__name__}: {exc}",
                )
            client_cfg = replace(cfg, sd_proxy_port=proxy_port)

        from sd_client import SDClient

        async def fetch() -> tuple[int, int]:
            client = SDClient(client_cfg)
            try:
                items, total = await client.fetch_requests(
                    view=cfg.sd_view, condition=cfg.sd_condition, limit=20
                )
                return len(items), total
            finally:
                await client.close()

    try:
        count, total = await fetch()
    except Exception as exc:  # noqa: BLE001 — показываем причину, а не падаем
        return Check("ServiceDesk", False, f"ошибка: {type(exc).__name__}: {exc}")
    finally:
        if proxy_runner is not None:
            await proxy_runner.cleanup()

    detail = f"заявок получено {count} из {total}"
    if total == 0:
        return Check(
            "ServiceDesk",
            False,
            f"вход выполнен, но фильтр ничего не вернул ({detail}) — проверь SD_CONDITION",
        )
    return Check("ServiceDesk", True, detail)


async def telegram_check(cfg: Config, get_me=None) -> Check:
    """Живая проверка токена бота через ту же сеть, что использует бот."""
    if get_me is None:
        from aiogram import Bot

        from telegram_net import build_session

        async def get_me() -> str:
            session = build_session(cfg.proxy_url, cfg.telegram_api_ip)
            bot = Bot(token=cfg.bot_token, session=session)
            try:
                me = await bot.get_me()
                return me.username or str(me.id)
            finally:
                await bot.session.close()

    try:
        username = await get_me()
    except Exception as exc:  # noqa: BLE001
        return Check("Telegram", False, f"ошибка: {type(exc).__name__}: {exc}")

    return Check("Telegram", True, f"бот @{username}")


def print_report(checks: list[Check]) -> bool:
    """Печатает таблицу и возвращает True, если критичных проблем нет."""
    print("\nПроверка готовности к запуску")
    print("-" * 60)
    for check in checks:
        mark = "OK  " if check.ok else ("ПРОВАЛ" if check.critical else "ВНИМАНИЕ")
        print(f"[{mark:8}] {check.name}: {check.detail}")
    print("-" * 60)

    critical_failed = [c for c in checks if c.critical and not c.ok]
    warnings = [c for c in checks if not c.critical and not c.ok]

    if critical_failed:
        print(f"Итог: есть проблемы — {len(critical_failed)}")
        return False
    if warnings:
        print(f"Итог: запускать можно, но есть предупреждения — {len(warnings)}")
    else:
        print("Итог: всё в порядке")
    return True


def state_checks(cfg: Config) -> list[Check]:
    """Проверки локальных файлов состояния: свежесть, очередь, отметка о жизни."""
    checks: list[Check] = []

    state_path = cfg.data_dir / "sd_state.json"
    if cfg.sd_enabled:
        if not state_path.exists():
            checks.append(
                Check("Состояние SD", True, "файла нет — будет создан при первом запуске", critical=False)
            )
        else:
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                checks.append(Check("Состояние SD", True, f"файл не читается ({exc}) — будет создан заново", critical=False))
            else:
                last_check = float(data.get("last_check") or 0)
                if not last_check:
                    checks.append(Check("Состояние SD", True, "проверок ещё не было", critical=False))
                else:
                    age = time.time() - last_check
                    limit = max(300, cfg.sd_check_interval * 5)
                    checks.append(
                        Check(
                            "Состояние SD",
                            age <= limit,
                            f"последняя проверка {_format_age(age)} назад",
                            critical=False,
                        )
                    )

    pending_path = cfg.data_dir / "pending_events.json"
    if pending_path.exists():
        try:
            events = json.loads(pending_path.read_text(encoding="utf-8"))
            count = len(events) if isinstance(events, list) else 0
        except (OSError, json.JSONDecodeError):
            count = 0
        checks.append(
            Check(
                "Недоставленные сообщения",
                count == 0,
                "очередь пуста" if count == 0 else f"{count} ждут отправки — связи не было",
                critical=False,
            )
        )

    runtime_path = cfg.data_dir / "runtime.json"
    if runtime_path.exists():
        try:
            mark = json.loads(runtime_path.read_text(encoding="utf-8"))
            clean = bool(mark.get("clean_stop"))
            last_alive = float(mark.get("last_alive") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            clean, last_alive = True, 0.0

        if last_alive:
            age = time.time() - last_alive
            if clean:
                checks.append(
                    Check("Бот сейчас", True, f"остановлен штатно ({_format_age(age)} назад)", critical=False)
                )
            elif age <= 90:
                checks.append(
                    Check("Бот сейчас", True, f"работает (сердцебиение {_format_age(age)} назад)", critical=False)
                )
            else:
                # Процесс умер и его никто не поднял — самый важный случай
                checks.append(
                    Check(
                        "Бот сейчас",
                        False,
                        f"НЕ работает: последнее сердцебиение {_format_age(age)} назад. "
                        f"Запусти start_bot_watchdog.cmd или проверь сервис на хостинге",
                        critical=False,
                    )
                )
    return checks


def _format_age(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total} с"
    if total < 3600:
        return f"{total // 60} мин"
    return f"{total // 3600} ч {total % 3600 // 60} мин"


async def run(cfg: Config) -> bool:
    checks = config_checks(cfg)
    checks.extend(state_checks(cfg))

    has_critical_config_problem = any(c.critical and not c.ok for c in checks)
    if not has_critical_config_problem:
        checks.append(await sd_check(cfg))
        checks.append(await telegram_check(cfg))

    return print_report(checks)


def main() -> None:
    try:
        cfg = load_config()
    except ValueError as exc:
        print("Настройки не читаются:")
        print(exc)
        sys.exit(1)

    ok = asyncio.run(run(cfg))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
