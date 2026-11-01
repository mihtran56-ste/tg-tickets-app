"""Проверка аккуратной остановки: сигнал должен поднимать событие остановки.

Запуск:  python tests/test_shutdown.py
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import install_stop_handlers  # noqa: E402


async def scenario_sigint_sets_event() -> None:
    """SIGINT (Ctrl+C) поднимает событие остановки."""
    stop = asyncio.Event()
    install_stop_handlers(stop)
    assert not stop.is_set(), "событие должно быть сброшено до сигнала"

    signal.raise_signal(signal.SIGINT)
    for _ in range(20):
        if stop.is_set():
            break
        await asyncio.sleep(0.01)

    assert stop.is_set(), "SIGINT не привёл к остановке"
    print("  OK: SIGINT поднимает событие остановки")


async def scenario_sigterm_sets_event() -> None:
    """SIGTERM (хостинг) поднимает событие остановки."""
    stop = asyncio.Event()
    install_stop_handlers(stop)

    if hasattr(signal, "SIGTERM"):
        signal.raise_signal(signal.SIGTERM)
        for _ in range(20):
            if stop.is_set():
                break
            await asyncio.sleep(0.01)
        assert stop.is_set(), "SIGTERM не привёл к остановке"
        print("  OK: SIGTERM поднимает событие остановки")
    else:
        print("  OK: SIGTERM в этой системе недоступен — пропущено")


async def scenario_repeated_signal_is_safe() -> None:
    """Повторный сигнал не ломает обработчик."""
    stop = asyncio.Event()
    install_stop_handlers(stop)
    signal.raise_signal(signal.SIGINT)
    await asyncio.sleep(0.05)
    second_raised = False
    try:
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0.05)
    except Exception:
        second_raised = True
    assert not second_raised, "повторный сигнал вызвал исключение"
    assert stop.is_set()
    print("  OK: повторный сигнал не вызывает ошибок")


async def main() -> None:
    print("Проверка обработки сигналов остановки")
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM) if hasattr(signal, "SIGTERM") else None
    try:
        for title, scenario in (
            ("[SIGINT]", scenario_sigint_sets_event),
            ("[SIGTERM]", scenario_sigterm_sets_event),
            ("[повторный сигнал]", scenario_repeated_signal_is_safe),
        ):
            print(f"\n{title}")
            await scenario()
    finally:
        signal.signal(signal.SIGINT, previous_int)
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
