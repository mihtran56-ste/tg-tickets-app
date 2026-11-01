"""Развёртывание бота на Render через API (без кликов в панели).

Запуск:
    set RENDER_API_KEY=rnd_xxx        (Windows)
    python scripts/render_deploy.py --repo-url https://github.com/user/tg-tickets

Что делает:
1. проверяет ключ и находит workspace (ownerId);
2. создаёт (или находит существующий) web service на free-тарифе;
3. заливает переменные окружения из .env.server;
4. запускает деплой и показывает статус.

Скрипт идемпотентный: повторный запуск не создаёт второй сервис.
Ошибки печатаются с телом ответа Render — по нему видно, какое поле не так.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.render.com/v1"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.server"

SERVICE_NAME = "tg-tickets"
REGION = "frankfurt"
PLAN = "free"


def call(
    method: str,
    path: str,
    api_key: str,
    payload: dict | list | None = None,
    quiet: bool = False,
) -> tuple[int, object]:
    """Запрос к API Render. Возвращает (код, разобранный ответ)."""
    url = f"{API}{path}"
    data = None
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(body) if body.strip() else {}
            if not quiet:
                print(f"  {method} {path} -> {response.status}")
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"  {method} {path} -> HTTP {exc.code}")
        print(f"  ответ Render: {body[:800]}")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}
    except urllib.error.URLError as exc:
        print(f"  {method} {path} -> сеть недоступна: {exc.reason}")
        return 0, {"error": str(exc.reason)}


def read_env_file(path: Path) -> list[dict[str, str]]:
    """Читает KEY=VALUE из файла, пропуская комментарии и пустые строки."""
    if not path.exists():
        raise SystemExit(f"нет файла с переменными: {path}")

    # На Render эти переменные задаёт платформа: PORT подставляет сам Render,
    # а каталога /data (Docker-том) там нет — иначе сервис не поднимется.
    skip = {"PORT", "DATA_DIR"}

    result: list[dict[str, str]] = []
    skipped: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in skip:
            skipped.append(key)
            continue
        result.append({"key": key, "value": value.strip()})

    if skipped:
        print(f"  пропущены платформенные переменные: {', '.join(sorted(skipped))}")
    return result


def find_owner(api_key: str) -> str:
    status, owners = call("GET", "/owners?limit=20", api_key)
    if status != 200 or not isinstance(owners, list) or not owners:
        raise SystemExit("не удалось получить workspace (ownerId) — проверь ключ")

    for entry in owners:
        owner = entry.get("owner", entry) if isinstance(entry, dict) else {}
        if owner.get("id"):
            name = owner.get("name", "?")
            print(f"  workspace: {name} ({owner['id']})")
            return owner["id"]
    raise SystemExit("в ответе /owners нет id")


def find_service(api_key: str, repo_url: str) -> dict | None:
    status, services = call("GET", f"/services?name={urllib.parse.quote(SERVICE_NAME)}&limit=20", api_key)
    if status != 200 or not isinstance(services, list):
        return None

    for entry in services:
        service = entry.get("service", entry) if isinstance(entry, dict) else {}
        if not isinstance(service, dict):
            continue
        if service.get("name") == SERVICE_NAME:
            print(f"  сервис уже существует: {service.get('id')} ({service.get('serviceDetails', {}).get('url', '')})")
            return service
    return None


def create_service(api_key: str, owner_id: str, repo_url: str, env_vars: list[dict], root_dir: str = "") -> dict:
    payload = {
        "type": "web_service",
        "name": SERVICE_NAME,
        "ownerId": owner_id,
        "repo": repo_url,
        "branch": "main",
        "autoDeploy": "yes",
        "envVars": env_vars,
        "serviceDetails": {
            "env": "python",
            "plan": PLAN,
            "region": REGION,
            "buildCommand": "pip install -r requirements.txt",
            "startCommand": "python bot.py",
            "healthCheckPath": "/health",
            "envSpecificDetails": {
                "buildCommand": "pip install -r requirements.txt",
                "startCommand": "python bot.py",
            },
        },
    }
    if root_dir:
        payload["rootDir"] = root_dir

    status, created = call("POST", "/services", api_key, payload)
    if status not in (200, 201) or not isinstance(created, dict):
        raise SystemExit("не удалось создать сервис — смотри ответ Render выше")

    service = created.get("service", created)
    print(f"  сервис создан: {service.get('id')}")
    return service


def update_env_vars(api_key: str, service_id: str, env_vars: list[dict]) -> None:
    status, _ = call("PUT", f"/services/{service_id}/env-vars", api_key, env_vars)
    if status not in (200, 201):
        print("  ПРЕДУПРЕЖДЕНИЕ: переменные не залились — впиши их в панели Render вручную")
    else:
        print(f"  переменных залито: {len(env_vars)}")


def trigger_deploy(api_key: str, service_id: str) -> str | None:
    status, deploy = call("POST", f"/services/{service_id}/deploys", api_key, {"clearCache": "do_not_clear"})
    if status not in (200, 201) or not isinstance(deploy, dict):
        print("  деплой не запустился — можно нажать Manual Deploy в панели")
        return None
    deploy_id = deploy.get("id") or (deploy.get("deploy") or {}).get("id")
    print(f"  деплой запущен: {deploy_id}")
    return deploy_id


def wait_deploy(api_key: str, service_id: str, deploy_id: str, timeout_sec: int = 600) -> str:
    started = time.time()
    last = ""
    while time.time() - started < timeout_sec:
        status, deploy = call("GET", f"/services/{service_id}/deploys/{deploy_id}", api_key, quiet=True)
        if status == 200 and isinstance(deploy, dict):
            state = deploy.get("status", "?")
            if state != last:
                print(f"  статус деплоя: {state} ({int(time.time() - started)} с)")
                last = state
            if state in ("live", "build_failed", "update_failed", "canceled", "pre_deploy_failed"):
                return state
        time.sleep(15)
    return "timeout"


def main() -> None:
    parser = argparse.ArgumentParser(description="Деплой TG_Tickets на Render через API")
    parser.add_argument("--repo-url", required=True, help="https://github.com/user/tg-tickets")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--no-wait", action="store_true", help="не ждать окончания деплоя")
    args = parser.parse_args()

    api_key = os.getenv("RENDER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("не задан RENDER_API_KEY (Settings -> API Keys в панели Render)")

    env_vars = read_env_file(Path(args.env_file))
    print(f"Переменных из {Path(args.env_file).name}: {len(env_vars)}")

    print("\n1) workspace")
    owner_id = find_owner(api_key)

    print("\n2) сервис")
    service = find_service(api_key, args.repo_url)
    if service is None:
        service = create_service(api_key, owner_id, args.repo_url, env_vars)
    service_id = service.get("id")
    if not service_id:
        raise SystemExit("у сервиса нет id — смотри ответ Render")

    print("\n3) переменные окружения")
    update_env_vars(api_key, service_id, env_vars)

    print("\n4) деплой")
    deploy_id = trigger_deploy(api_key, service_id)
    if deploy_id and not args.no_wait:
        state = wait_deploy(api_key, service_id, deploy_id)
        print(f"\nИтог деплоя: {state}")

    status, fresh = call("GET", f"/services/{service_id}", api_key, quiet=True)
    if status == 200 and isinstance(fresh, dict):
        service = fresh.get("service", fresh)
    url = (service.get("serviceDetails") or {}).get("url", "")
    print(f"\nАдрес сервиса: {url or 'смотри панель Render'}")
    print(f"Проверка живости: {url}/health" if url else "")
    print("Дальше: python preflight.py в консоли сервиса Render и тест в Telegram (/sd_status)")


if __name__ == "__main__":
    main()
