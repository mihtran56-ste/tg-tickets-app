"""SD Watcher — фоновая задача проверки новых заявок и отправки уведомлений."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from sd_client import SDClient, SDRequest
from sd_sla import (
    SLA_EXPIRED,
    SLA_WARN,
    deadline_key,
    format_sla_notification,
    localize,
    now_in,
    sla_stage,
)
from sd_state import SDStateStore

log = logging.getLogger("sd_watcher")


@dataclass
class WatchResult:
    """Результат одной проверки."""

    checked_at: float
    new_requests: list[SDRequest]
    errors: list[str]
    duration_ms: int
    # Сколько заявок запомнено молча при первом запуске (без рассылки)
    seeded: int = 0
    # Сколько SLA-сообщений ушло за эту проверку
    sla_warned: int = 0
    sla_expired: int = 0


class SDWatcher:
    """Периодически проверяет SD на новые заявки и шлёт уведомления."""

    def __init__(
        self,
        cfg: Config,
        bot: Bot,
        state_store: SDStateStore,
        client: SDClient,
    ) -> None:
        self.cfg = cfg
        self.bot = bot
        self.state_store = state_store
        self.client = client
        self._task: asyncio.Task | None = None
        self._running = False
        self._paused = False
        self._last_result: WatchResult | None = None

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True
        log.info("SD Watcher: пауза")

    def resume(self) -> None:
        self._paused = False
        log.info("SD Watcher: возобновлено")

    async def start(self) -> None:
        """Запускает фоновый цикл проверки."""
        if self.is_running:
            log.warning("SD Watcher уже запущен")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="sd_watcher")
        log.info("SD Watcher запущен (интервал: %s сек)", self.cfg.sd_check_interval)

    async def stop(self) -> None:
        """Останавливает фоновый цикл."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("SD Watcher остановлен")

    async def _run_loop(self) -> None:
        # Небольшая задержка при старте, чтобы бот успел инициализироваться
        await asyncio.sleep(5)

        while self._running:
            if self._paused:
                await asyncio.sleep(5)
                continue

            start = time.time()
            try:
                result = await self._check_once()
                self._last_result = result
                self.state_store.clear_error()
            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                log.exception("Ошибка в цикле SD Watcher")
                self.state_store.record_error(error_msg)
                self._last_result = WatchResult(
                    checked_at=time.time(),
                    new_requests=[],
                    errors=[error_msg],
                    duration_ms=int((time.time() - start) * 1000),
                )

            # Ждём до следующей проверки
            elapsed = time.time() - start
            sleep_time = max(1, self.cfg.sd_check_interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def _check_once(self, now: datetime | None = None) -> WatchResult:
        """Одна проверка новых заявок и SLA.

        ``now`` передаётся только тестами: в бою время берётся в часовом поясе
        SD, а не сервера (на Render сервер живёт в UTC).
        """
        start = time.time()
        moment = now or now_in(self.cfg.sd_timezone)
        errors: list[str] = []
        new_requests: list[SDRequest] = []
        sla_warned = 0
        sla_expired = 0

        try:
            # Получаем заявки из SD (возвращает tuple: items, total)
            requests, total_count = await self.client.fetch_requests(
                view=self.cfg.sd_view,
                condition=self.cfg.sd_condition,
                limit=100,  # достаём больше, чем нужно, на случай пагинации
            )

            # Важно: определяем первый запуск ДО фиксации времени проверки,
            # иначе условие last_check == 0 уже не выполнится.
            first_run = self._is_first_run()

            # Проверка состоялась — фиксируем время. Это же не даёт «тихому
            # первому запуску» сработать второй раз, если первая рассылка
            # не удалась: last_check уже не ноль.
            self.state_store.update_last_check()

            # Фильтруем новые
            state = self.state_store.state

            # Первый запуск (в т.ч. переезд на чистый диск): запоминаем текущие
            # заявки молча, чтобы не присылать историю заново.
            if first_run:
                for req in requests:
                    state.mark_seen(req.number, req.sys_id, req.code)
                sla_counts = None
                if self.cfg.sd_sla_enabled:
                    # Старые сроки берём под наблюдение молча: иначе первый же
                    # запуск пришлёт пачку «сгоревших» за всю историю.
                    sla_counts = self._init_sla(requests, moment)
                    state.sla_initialized = True
                self.state_store.save()
                log.info(
                    "Первый запуск: запомнено %d заявок без уведомлений (total=%d)",
                    len(requests),
                    total_count,
                )
                await self._send_startup_summary(len(requests), total_count, sla_counts)
                return WatchResult(
                    checked_at=time.time(),
                    new_requests=[],
                    errors=[],
                    duration_ms=int((time.time() - start) * 1000),
                    seeded=len(requests),
                )

            # Первое появление SLA-контроля на работающем боте (обновление версии):
            # молча берём текущие сроки под наблюдение и один раз сообщаем сводку.
            # На пустом состоянии (SD_SEED_ON_FIRST_RUN=false) сводка не нужна:
            # заявки всё равно придут обычными уведомлениями о новых.
            if self.cfg.sd_sla_enabled and not state.sla_initialized:
                had_history = bool(state.seen_numbers or state.seen_sys_ids)
                sla_counts = self._init_sla(requests, moment)
                state.sla_initialized = True
                self.state_store.save()
                if had_history:
                    await self._send_sla_summary(*sla_counts)

            for req in requests:
                if not state.is_seen(req.number, req.sys_id):
                    new_requests.append(req)

            # Сортируем по дате создания (старые сначала)
            new_requests.sort(key=lambda r: r.sys_created_at)

            log.info("Проверка SD: всего %d (total=%d), новых %d", len(requests), total_count, len(new_requests))

            # Шлём уведомления о новых заявках
            if new_requests:
                await self._send_notifications(new_requests, total_count=total_count)

            if self.cfg.sd_sla_enabled:
                # У только что объявленной заявки дедлайн уже есть в сообщении,
                # поэтому отдельный SLA-алерт по ней не шлём — только ставим флаги.
                silent = {req.sys_id for req in new_requests}
                sla_warned, sla_expired = await self._process_sla(requests, moment, silent)

        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
            log.exception("Ошибка при проверке SD")

        duration_ms = int((time.time() - start) * 1000)
        return WatchResult(
            checked_at=time.time(),
            new_requests=new_requests,
            errors=errors,
            duration_ms=duration_ms,
            sla_warned=sla_warned,
            sla_expired=sla_expired,
        )

    def _is_first_run(self) -> bool:
        """Первый запуск: состояние пустое, но заявки уже есть."""
        if not self.cfg.sd_seed_on_first_run:
            return False
        state = self.state_store.state
        return (
            state.last_check == 0.0
            and not state.seen_numbers
            and not state.seen_sys_ids
        )

    def _warn_label(self) -> str:
        """«2 ч» / «1,5 ч» для текстов."""
        value = f"{self.cfg.sd_sla_warn_hours:g}".replace(".", ",")
        return f"{value} ч"

    async def _send_startup_summary(
        self,
        seeded: int,
        total_count: int,
        sla_counts: tuple[int, int, int] | None = None,
    ) -> None:
        """Одно сообщение при первом запуске: мониторинг работает, история не льётся."""
        chat_ids = list(self.cfg.sd_notify_chat_ids or self.cfg.allowed_user_ids)
        if not chat_ids:
            return

        text = (
            "🚀 <b>SD-мониторинг запущен</b>\n"
            f"📊 Заявок по фильтру сейчас: <b>{total_count or seeded}</b>\n"
            f"🗂 Запомнено без рассылки: {seeded}\n"
            "🔔 Дальше буду присылать только новые заявки."
        )
        if sla_counts is not None:
            tracked, expired, warning = sla_counts
            text += (
                f"\n⏰ SLA: под наблюдением {tracked}, уже просрочено {expired}, "
                f"в окне {self._warn_label()}: {warning}"
            )

        for chat_id in chat_ids:
            try:
                await self.bot.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                log.info("Стартовое сообщение отправлено в chat_id=%s", chat_id)
            except TelegramUnauthorizedError:
                log.error("Бот не авторизован в Telegram (неверный токен)")
                break
            except TelegramNetworkError as e:
                log.warning("Сеть Telegram для chat_id=%s: %s", chat_id, e)
            except Exception:
                log.exception("Ошибка отправки стартового сообщения в chat_id=%s", chat_id)

    async def _send_sla_summary(self, tracked: int, expired: int, warning: int) -> None:
        """Одноразовая сводка при первом включении SLA-контроля на живом боте."""
        chat_ids = list(self.cfg.sd_notify_chat_ids or self.cfg.allowed_user_ids)
        if not chat_ids:
            return

        text = (
            "⏰ <b>SLA-контроль включён</b>\n"
            f"👁 Под наблюдением: <b>{tracked}</b>\n"
            f"🔥 Уже просрочено: {expired} · ⏳ в окне {self._warn_label()}: {warning}\n"
            f"Дальше предупрежу за {self._warn_label()} до дедлайна и в момент истечения."
        )
        for chat_id in chat_ids:
            try:
                await self.bot.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                log.info("Сводка о включении SLA отправлена в chat_id=%s", chat_id)
            except TelegramUnauthorizedError:
                log.error("Бот не авторизован в Telegram (неверный токен)")
                break
            except TelegramNetworkError as e:
                log.warning("Сеть Telegram для chat_id=%s: %s", chat_id, e)
            except Exception:
                log.exception("Ошибка отправки сводки SLA в chat_id=%s", chat_id)

    async def _send_notifications(self, requests: list[SDRequest], total_count: int = 0) -> None:
        """Отправляет уведомления о новых заявках."""
        # Определяем получателей
        chat_ids = list(self.cfg.sd_notify_chat_ids or self.cfg.allowed_user_ids)
        if not chat_ids:
            log.warning("Нет получателей для уведомлений SD (пусты ALLOWED_USER_IDS и SD_NOTIFY_CHAT_IDS)")
            return

        state = self.state_store.state

        for req in requests:
            text = req.format_notification(total_count)
            delivered = False

            for chat_id in chat_ids:
                try:
                    await self.bot.send_message(
                        chat_id,
                        text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    delivered = True
                    log.info("Уведомление отправлено в chat_id=%s: %s", chat_id, req.number)
                except TelegramUnauthorizedError:
                    log.error("Бот не авторизован в Telegram (неверный токен)")
                    break
                except TelegramNetworkError as e:
                    log.warning("Сеть Telegram для chat_id=%s: %s", chat_id, e)
                except Exception:
                    log.exception("Ошибка отправки в chat_id=%s", chat_id)

            if delivered:
                state.mark_seen(req.number, req.sys_id, req.code)
                state.record_notification_sent()
            else:
                # Не отмечаем как отправленное — иначе уведомление потеряется
                # навсегда. На следующем цикле попробуем снова.
                log.error(
                    "Уведомление по %s не доставлено — повторю на следующей проверке",
                    req.number,
                )

        self.state_store.save()

    def _init_sla(
        self, requests: list[SDRequest], moment: datetime
    ) -> tuple[int, int, int]:
        """Молча ставит текущие сроки под наблюдение (без сообщений).

        Возвращает (под наблюдением, уже просрочено, в окне предупреждения).
        Нужно на первом запуске и при первом включении SLA: иначе бот пришлёт
        пачку «сгоревших» заявок за всю историю.
        """
        state = self.state_store.state
        warn_delta = timedelta(hours=self.cfg.sd_sla_warn_hours)
        tracked = expired = warning = 0

        for req in requests:
            if req.deadline_at is None or req.resolved_at:
                continue
            tracked += 1
            key = deadline_key(req)
            state.sync_sla(req.sys_id, key, moment.timestamp())
            stage = sla_stage(
                localize(req.deadline_at, self.cfg.sd_timezone), moment, warn_delta
            )
            if stage == SLA_EXPIRED:
                expired += 1
                state.mark_sla(req.sys_id, SLA_EXPIRED, key, moment.timestamp())
            elif stage == SLA_WARN:
                warning += 1
                state.mark_sla(req.sys_id, SLA_WARN, key, moment.timestamp())

        return tracked, expired, warning

    async def _process_sla(
        self, requests: list[SDRequest], moment: datetime, silent: set[str]
    ) -> tuple[int, int]:
        """Шлёт предупреждения и сообщения о просрочке. Возвращает (warn, expired)."""
        state = self.state_store.state
        warn_delta = timedelta(hours=self.cfg.sd_sla_warn_hours)
        warned = expired = 0

        for req in requests:
            if req.deadline_at is None or req.resolved_at:
                continue

            key = deadline_key(req)
            entry = state.sync_sla(req.sys_id, key, moment.timestamp())
            stage = sla_stage(
                localize(req.deadline_at, self.cfg.sd_timezone), moment, warn_delta
            )
            if stage is None:
                continue

            # Уже отправляли это событие по текущему дедлайну — не дублируем.
            if stage == SLA_EXPIRED:
                if entry.get("expired"):
                    continue
            elif entry.get("warn") or entry.get("expired"):
                continue

            # Заявка только что объявлена как новая: дедлайн уже был в том письме.
            if req.sys_id in silent:
                state.mark_sla(req.sys_id, stage, key, moment.timestamp())
                continue

            if await self._send_sla_notification(req, stage, moment):
                state.mark_sla(req.sys_id, stage, key, moment.timestamp())
                state.record_sla_sent(stage)
                if stage == SLA_EXPIRED:
                    expired += 1
                else:
                    warned += 1

        state.prune_sla(moment.timestamp())
        self.state_store.save()
        return warned, expired

    async def _send_sla_notification(
        self, req: SDRequest, stage: str, moment: datetime
    ) -> bool:
        """Одно SLA-сообщение со ссылкой и кнопкой «Открыть в SD»."""
        chat_ids = list(self.cfg.sd_notify_chat_ids or self.cfg.allowed_user_ids)
        if not chat_ids:
            log.warning("Нет получателей для SLA-уведомлений (пусты ALLOWED_USER_IDS и SD_NOTIFY_CHAT_IDS)")
            return False

        text = format_sla_notification(
            req,
            stage,
            moment,
            self.cfg.sd_timezone,
            self.cfg.sd_sla_warn_hours,
        )
        if not text:
            log.error("Пустой текст SLA-уведомления по %s — пропускаю", req.number)
            return False

        # Кнопку добавляем только при абсолютной ссылке: относительный URL
        # Telegram отвергает вместе со всем сообщением (BUTTON_URL_INVALID).
        keyboard = None
        if req.url.startswith(("http://", "https://")):
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть в SD", url=req.url)]
                ]
            )

        delivered = False

        for chat_id in chat_ids:
            try:
                payload: dict[str, object] = {
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                if keyboard is not None:
                    payload["reply_markup"] = keyboard
                await self.bot.send_message(chat_id, text, **payload)
                delivered = True
                log.info(
                    "SLA-уведомление (%s) отправлено в chat_id=%s: %s",
                    stage,
                    chat_id,
                    req.number,
                )
            except TelegramUnauthorizedError:
                log.error("Бот не авторизован в Telegram (неверный токен)")
                break
            except TelegramNetworkError as e:
                log.warning("Сеть Telegram для chat_id=%s: %s", chat_id, e)
            except Exception:
                log.exception("Ошибка отправки SLA-уведомления в chat_id=%s", chat_id)

        if not delivered:
            # Не отмечаем событие — на следующей проверке попробуем снова.
            log.error(
                "SLA-уведомление по %s не доставлено — повторю на следующей проверке",
                req.number,
            )
        return delivered

    def get_status(self) -> dict[str, Any]:
        """Возвращает статус для команды /sd_status."""
        state = self.state_store.state
        stats = self.state_store.get_stats()

        return {
            "running": self.is_running,
            "paused": self.is_paused,
            "interval_sec": self.cfg.sd_check_interval,
            "last_check": stats["last_check_str"],
            "seen_count": stats["seen_count"],
            "total_sent": stats["total_sent"],
            "last_error": stats["last_error"] or "нет",
            "last_error_time": (
                time.strftime("%d.%m %H:%M", time.localtime(stats["last_error_time"]))
                if stats["last_error_time"]
                else "—"
            ),
            "sla": {
                "enabled": self.cfg.sd_sla_enabled,
                "warn_hours": self.cfg.sd_sla_warn_hours,
                "initialized": state.sla_initialized,
                "tracked": len(state.sla_events),
                "warn_sent": state.sla_warn_sent,
                "expired_sent": state.sla_expired_sent,
            },
            "last_result": {
                "checked_at": time.strftime("%d.%m %H:%M:%S", time.localtime(self._last_result.checked_at))
                if self._last_result
                else "—",
                "new_count": len(self._last_result.new_requests) if self._last_result else 0,
                "seeded": self._last_result.seeded if self._last_result else 0,
                "errors": self._last_result.errors if self._last_result else [],
                "duration_ms": self._last_result.duration_ms if self._last_result else 0,
                "sla_warned": self._last_result.sla_warned if self._last_result else 0,
                "sla_expired": self._last_result.sla_expired if self._last_result else 0,
            } if self._last_result else None,
        }

    async def check_now(self) -> WatchResult:
        """Принудительная проверка прямо сейчас (для команды /sd_test)."""
        log.info("SD Watcher: принудительная проверка")
        return await self._check_once()

    def get_recent_notifications(self, limit: int = 10) -> list[str]:
        """Последние уведомлённые заявки (для /sd_last).

        Показываем только рабочий код из темы (SUTSPROD-…), если он известен:
        REQ-номер пользователю не нужен. Если кода нет — показываем REQ.
        """
        state = self.state_store.state
        # seen_numbers хранится в порядке добавления (старые в начале)
        return [state.label_for(number) or number for number in state.seen_numbers[-limit:]]