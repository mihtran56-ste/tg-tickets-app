"""Проверка хранилища состояния SD: лимит памяти, восстановление, статистика.

Запуск:  python tests/test_state.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sd_state import MAX_SEEN, SDState, SDStateStore  # noqa: E402


def fresh(tmp: Path) -> Path:
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    return tmp


def scenario_seen_logic() -> None:
    state = SDState()
    assert not state.is_seen("REQ1", "abc"), "пустое состояние ничего не видело"

    state.mark_seen("REQ1", "abc")
    assert state.is_seen("REQ1", "zzz"), "поиск по номеру не работает"
    assert state.is_seen("REQ9", "abc"), "поиск по sys_id не работает"
    assert not state.is_seen("REQ9", "zzz")
    print("  OK: заявка считается виденной по номеру или по sys_id")


def scenario_lru_limit() -> None:
    state = SDState()
    for i in range(MAX_SEEN + 50):
        state.mark_seen(f"REQ{i}", f"id{i}")

    assert len(state.seen_numbers) == MAX_SEEN, f"номеров: {len(state.seen_numbers)}, ожидали {MAX_SEEN}"
    assert len(state.seen_sys_ids) == MAX_SEEN, f"sys_id: {len(state.seen_sys_ids)}"
    assert "REQ0" not in state.seen_numbers, "самые старые записи должны вытесняться"
    assert f"REQ{MAX_SEEN + 49}" in state.seen_numbers, "свежая запись должна остаться"
    print(f"  OK: память ограничена {MAX_SEEN} записями, старые вытесняются")


def scenario_persistence(tmp: Path) -> None:
    store = SDStateStore(tmp)
    store.mark_seen("REQ1", "a1")
    store.record_notification()
    store.record_error("сбой сети")
    store.update_last_check()

    reloaded = SDStateStore(tmp).load()
    assert reloaded.seen_numbers == ["REQ1"]
    assert reloaded.total_notifications_sent == 1
    assert reloaded.last_error == "сбой сети", f"ошибка не сохранилась: {reloaded.last_error}"
    assert reloaded.last_check > 0
    print("  OK: состояние, счётчик и ошибка переживают перезапуск")

    store.clear_error()
    assert SDStateStore(tmp).load().last_error == "", "ошибка не очищается"
    print("  OK: ошибка очищается и это сохраняется")


def scenario_stats(tmp: Path) -> None:
    empty = SDStateStore(fresh(tmp / "empty"))
    stats = empty.get_stats()
    assert stats["last_check_str"] == "никогда", f"без проверок: {stats['last_check_str']}"
    assert stats["seen_count"] == 0
    print("  OK: до первой проверки статистика показывает «никогда»")

    store = SDStateStore(fresh(tmp / "filled"))
    store.mark_seen("REQ1", "a1")
    store.update_last_check()
    stats = store.get_stats()
    assert stats["seen_count"] == 1
    assert stats["total_sent"] == 0
    assert stats["last_check_str"] != "никогда", "время проверки не отформатировано"
    assert len(stats["last_check_str"]) == 19, f"формат времени: {stats['last_check_str']}"
    print(f"  OK: статистика показывает время проверки ({stats['last_check_str']})")


def scenario_corrupt_file(tmp: Path) -> None:
    data_dir = fresh(tmp / "corrupt")
    path = data_dir / "sd_state.json"
    path.write_text("{это не json", encoding="utf-8")

    store = SDStateStore(data_dir)
    state = store.load()
    assert state.seen_numbers == [], "из повреждённого файла не должно быть записей"

    backups = list(data_dir.glob("sd_state.json.corrupt-*"))
    assert backups, "повреждённый файл не сохранён — данные о рассылке потеряны"
    assert "{это не json" in backups[0].read_text(encoding="utf-8")
    print(f"  OK: повреждённый файл отложен как {backups[0].name}")


def scenario_wrong_structure(tmp: Path) -> None:
    data_dir = fresh(tmp / "wrong")
    path = data_dir / "sd_state.json"
    path.write_text(json.dumps({"last_check": "не число", "seen_numbers": "не список"}), encoding="utf-8")

    store = SDStateStore(data_dir)
    state = store.load()
    assert isinstance(state, SDState)
    backups = list(data_dir.glob("sd_state.json.corrupt-*"))
    assert backups, "файл с неверной структурой должен откладываться, а не затираться"
    print("  OK: файл с неверной структурой не роняет запуск")


def scenario_labels(tmp: Path) -> None:
    """Рабочий код заявки запоминается и переживает перезапуск."""
    store = SDStateStore(fresh(tmp / "labels"))
    store.mark_seen("REQ1", "a1", "SUTSPROD-17353334")
    store.mark_seen("REQ2", "a2")  # без кода

    state = SDStateStore(fresh(tmp / "labels2")).load()
    assert state.seen_numbers == [], "новый каталог — пустое состояние"

    reloaded = SDStateStore(store.path.parent).load()
    assert reloaded.label_for("REQ1") == "SUTSPROD-17353334", "код не сохранился"
    assert reloaded.label_for("REQ2") == "", "у заявки без кода подписи быть не должно"
    assert reloaded.label_for("REQ404") == "", "неизвестная заявка — без подписи"
    print("  OK: код заявки сохраняется и доступен после перезапуска")

    bad = tmp / "bad_labels"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "sd_state.json").write_text(
        json.dumps({"seen_labels": "не объект"}), encoding="utf-8"
    )
    broken = SDStateStore(bad)
    assert broken.load().seen_numbers == [], "файл с неверным seen_labels должен откладываться"
    assert list(bad.glob("sd_state.json.corrupt-*")), "повреждённый файл не сохранён"
    print("  OK: неверный тип seen_labels не роняет запуск")


def scenario_sla_state(tmp: Path) -> None:
    """Флаги SLA переживают перезапуск, а перенос дедлайна их сбрасывает."""
    store = SDStateStore(fresh(tmp / "sla"))
    state = store.load()
    state.sla_initialized = True
    state.sync_sla("a1", "2026-11-01 18:00:00", 1000.0)
    state.mark_sla("a1", "warn", "2026-11-01 18:00:00", 1000.0)
    state.record_sla_sent("warn")
    store.save()

    reloaded = SDStateStore(store.path.parent).load()
    assert reloaded.sla_initialized is True
    assert reloaded.sla_events["a1"]["warn"] == 1000.0, "флаг предупреждения не сохранился"
    assert reloaded.sla_warn_sent == 1, "счётчик предупреждений не сохранился"
    print("  OK: состояние SLA и счётчики переживают перезапуск")

    entry = reloaded.sync_sla("a1", "2026-11-01 20:00:00", 2000.0)
    assert entry["warn"] == 0.0 and entry["expired"] == 0.0, (
        "перенос дедлайна не сбросил флаги — алерты по новому сроку не придут"
    )
    reloaded.mark_sla("a1", "expired", "2026-11-01 20:00:00", 3000.0)
    assert reloaded.sla_events["a1"]["expired"] == 3000.0
    assert reloaded.sla_events["a1"]["warn"] == 3000.0, "просрочка должна закрывать предупреждение"
    print("  OK: перенос дедлайна сбрасывает флаги, просрочка закрывает предупреждение")


def scenario_sla_prune() -> None:
    """Старые записи SLA убираются, свежие остаются."""
    state = SDState()
    state.sync_sla("old", "d", 0.0)
    state.sync_sla("fresh", "d", 863000.0)
    state.prune_sla(now_ts=864000.0)

    assert "old" not in state.sla_events, "запись без обновлений больше недели не удалена"
    assert "fresh" in state.sla_events, "свежая запись удалена"

    state = SDState()
    for index in range(3):
        state.sync_sla(f"id{index}", "d", 1000.0 + index)
    state.prune_sla(now_ts=1000.0, limit=2)
    assert set(state.sla_events) == {"id1", "id2"}, f"лимит не применён: {list(state.sla_events)}"
    print("  OK: записи SLA чистятся по возрасту и ограничиваются по количеству")


def scenario_old_state_without_sla(tmp: Path) -> None:
    """Старый sd_state.json без полей SLA читается без потери истории."""
    data_dir = fresh(tmp / "old_sla")
    (data_dir / "sd_state.json").write_text(
        json.dumps(
            {
                "last_check": 1.0,
                "seen_numbers": ["REQ1"],
                "seen_sys_ids": ["a1"],
                "total_notifications_sent": 5,
            }
        ),
        encoding="utf-8",
    )

    state = SDStateStore(data_dir).load()

    assert state.seen_numbers == ["REQ1"], "история увиденных заявок потерялась"
    assert state.sla_initialized is False, "SLA должен инициализироваться тихо при обновлении"
    assert state.sla_events == {}
    assert not list(data_dir.glob("sd_state.json.corrupt-*")), "старый файл не должен считаться битым"
    print("  OK: старый файл состояния без SLA читается как есть")


def scenario_atomic_save(tmp: Path) -> None:
    data_dir = fresh(tmp / "atomic")
    store = SDStateStore(data_dir)
    store.mark_seen("REQ1", "a1")
    assert not (data_dir / "sd_state.tmp").exists(), "временный файл остался после записи"
    print("  OK: запись атомарна, временный файл не остаётся")


def main() -> None:
    base = ROOT / ".tmp-test-state"
    print("Проверка хранилища состояния SD")
    for title, scenario in (
        ("[что считается виденным]", lambda: scenario_seen_logic()),
        ("[лимит памяти]", lambda: scenario_lru_limit()),
        ("[сохранение]", lambda: scenario_persistence(fresh(base / "persist"))),
        ("[статистика]", lambda: scenario_stats(base)),
        ("[повреждённый файл]", lambda: scenario_corrupt_file(base)),
        ("[неверная структура]", lambda: scenario_wrong_structure(base)),
        ("[код заявки]", lambda: scenario_labels(base)),
        ("[состояние SLA]", lambda: scenario_sla_state(base)),
        ("[чистка SLA]", lambda: scenario_sla_prune()),
        ("[старый файл без SLA]", lambda: scenario_old_state_without_sla(base)),
        ("[атомарность]", lambda: scenario_atomic_save(base)),
    ):
        print(f"\n{title}")
        scenario()
    shutil.rmtree(base, ignore_errors=True)
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    main()
