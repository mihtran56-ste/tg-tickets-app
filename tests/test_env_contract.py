"""Согласованность переменных окружения между кодом, примером и деплоем.

Опечатка в имени переменной при деплое не даёт ошибки: просто настройка
молча не применяется (например, SD_VERIFY_TLS вместо SD_VERIFY_SSL).

Запуск:  python tests/test_env_contract.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "config.py"
ENV_EXAMPLE = ROOT / ".env.example"
RENDER = ROOT / "render.yaml"

# Переменные, которые читает config.py
READER_RE = re.compile(r'_(?:env|int_env|float_env|bool_env)\(\s*"([A-Z0-9_]+)"')
# Переменные, объявленные в .env.example
EXAMPLE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)
# Переменные, объявленные в render.yaml
RENDER_RE = re.compile(r"^\s*-\s*key:\s*([A-Z][A-Z0-9_]*)\s*$", re.MULTILINE)

# Переменные платформы, а не приложения: их читает не наш код
PLATFORM_VARS = {"PYTHON_VERSION"}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def scenario_all_read_are_documented() -> None:
    """Каждая переменная из config.py описана в .env.example."""
    used = set(READER_RE.findall(read(CONFIG)))
    documented = set(EXAMPLE_RE.findall(read(ENV_EXAMPLE)))

    assert used, "не удалось найти ни одной переменной в config.py — сломался разбор"
    missing = used - documented
    assert not missing, f"в .env.example нет переменных, которые читает код: {sorted(missing)}"
    print(f"  OK: все {len(used)} переменных из config.py описаны в .env.example")


def scenario_no_typos_in_example() -> None:
    """В .env.example нет переменных, которые код не читает."""
    used = set(READER_RE.findall(read(CONFIG)))
    documented = set(EXAMPLE_RE.findall(read(ENV_EXAMPLE)))

    extra = documented - used
    assert not extra, f"в .env.example есть переменные, которые код не читает: {sorted(extra)}"
    print("  OK: в .env.example нет лишних переменных")


def scenario_render_matches_config() -> None:
    """render.yaml задаёт только те переменные, которые читает код."""
    used = set(READER_RE.findall(read(CONFIG)))
    render_keys = set(RENDER_RE.findall(read(RENDER)))

    assert render_keys, "не удалось разобрать render.yaml — сломался разбор"
    unknown = render_keys - used - PLATFORM_VARS
    assert not unknown, f"render.yaml задаёт неизвестные переменные: {sorted(unknown)}"
    print(f"  OK: все {len(render_keys)} переменных render.yaml известны коду"
          f" (платформенных: {sorted(render_keys & PLATFORM_VARS) or 'нет'})")


def scenario_critical_present() -> None:
    """Ключевые переменные есть во всех трёх местах."""
    used = set(READER_RE.findall(read(CONFIG)))
    documented = set(EXAMPLE_RE.findall(read(ENV_EXAMPLE)))
    render_keys = set(RENDER_RE.findall(read(RENDER)))

    critical = {"BOT_TOKEN", "ALLOWED_USER_IDS", "SD_ENABLED", "SD_BASE_URL", "SD_LOGIN", "SD_PASSWORD"}
    for name in sorted(critical):
        assert name in used, f"{name} не читается в config.py"
        assert name in documented, f"{name} нет в .env.example"
        assert name in render_keys, f"{name} не передаётся в render.yaml"

    # SD_CONDITION нужен для фильтра, но в render.yaml он секрет — тоже должен быть
    for name in ("SD_CONDITION", "SD_NOTIFY_CHAT_IDS", "HEALTH_TOKEN"):
        assert name in render_keys, f"{name} не передаётся в render.yaml"
    print(f"  OK: критичные переменные присутствуют во всех трёх файлах ({len(critical) + 3})")


def scenario_secrets_marked() -> None:
    """Секреты в render.yaml помечены sync: false, а не лежат в репозитории."""
    lines = read(RENDER).splitlines()
    found: dict[str, str] = {}
    for index, line in enumerate(lines):
        match = re.match(r"\s*-\s*key:\s*([A-Z][A-Z0-9_]*)\s*$", line)
        if match:
            # значение или sync идут следующими строками
            found[match.group(1)] = " ".join(lines[index + 1:index + 3])

    secret_names = {"BOT_TOKEN", "SD_LOGIN", "SD_PASSWORD", "SD_CONDITION", "HEALTH_TOKEN"}
    for name in sorted(secret_names):
        assert name in found, f"{name} отсутствует в render.yaml"
        assert "sync: false" in found[name], f"{name} должен быть секретом (sync: false), а не значением"
    print(f"  OK: секреты помечены sync: false ({len(secret_names)})")


def main() -> None:
    print("Проверка согласованности переменных окружения")
    for title, scenario in (
        ("[все читаемые переменные описаны]", scenario_all_read_are_documented),
        ("[нет опечаток в примере]", scenario_no_typos_in_example),
        ("[render.yaml соответствует коду]", scenario_render_matches_config),
        ("[критичные переменные]", scenario_critical_present),
        ("[секреты помечены]", scenario_secrets_marked),
    ):
        print(f"\n{title}")
        scenario()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    main()
