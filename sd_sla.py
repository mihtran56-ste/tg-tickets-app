"""SLA-логика заявок: когда предупреждать и как это показывать.

Модуль намеренно не знает ни про Telegram, ни про Config: на вход — заявка,
текущее время и часовой пояс, на выход — строка сообщения или список карточек.
Так границы («ровно 2 часа») проверяются тестами без сети и без бота.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:  # импорт только для аннотаций — без цикла config -> sd_sla
    from sd_client import SDRequest

log = logging.getLogger("sd_sla")

SLA_WARN = "warn"
SLA_EXPIRED = "expired"

# Москва с 2014 года живёт в UTC+3 без перехода на летнее время. Фолбэк нужен
# на Windows без базы IANA (пакет tzdata): иначе ZoneInfo('Europe/Moscow')
# падает, и SLA посчитать нельзя. На Linux (Render) база зон обычно есть.
MSK = timezone(timedelta(hours=3), "MSK")
_FALLBACK_ZONES = {"Europe/Moscow": MSK, "MSK": MSK, "UTC+3": MSK}


@lru_cache(maxsize=16)
def resolve_timezone(name: str):
    """Возвращает tzinfo по IANA-имени; для Москвы работает и без tzdata."""
    value = (name or "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        fallback = _FALLBACK_ZONES.get(value)
        if fallback is None:
            raise ValueError(
                f"SD_TIMEZONE: неизвестная зона «{value}». Укажи IANA-имя "
                "(например Europe/Moscow) или установи пакет tzdata."
            ) from None
        log.warning(
            "База часовых зон недоступна — для %s использую фиксированный UTC+3", value
        )
        return fallback


def now_in(tz_name: str) -> datetime:
    """Текущее время в часовом поясе SD (а не в часовом поясе сервера)."""
    return datetime.now(resolve_timezone(tz_name))


def localize(value: datetime | None, tz_name: str) -> datetime | None:
    """Переводит время из SD в часовой пояс SD.

    SimpleOne отдаёт время в UTC без зоны, поэтому наивное значение сначала
    помечается как UTC и только потом переводится в ``tz_name``. Раньше зона
    просто «приклеивалась» (replace), из-за чего дедлайн уезжал на 3 часа.
    """
    if value is None:
        return None
    tz = resolve_timezone(tz_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def deadline_key(req: "SDRequest") -> str:
    """Ключ дедлайна для состояния: по нему видно, что срок в SD перенесли."""
    if req.deadline_at is None:
        return ""
    return req.deadline_at.strftime("%Y-%m-%d %H:%M:%S")


def address_of(req: "SDRequest") -> str:
    """Адрес заявки: адрес терминала, иначе адрес точки."""
    return (req.multicard_terminal_address or req.ab_spot_address or "").strip()


def request_title(req: "SDRequest") -> str:
    """Рабочий код и REQ-номер рядом, если код есть."""
    code = (req.code or "").strip()
    if code and code != req.number:
        return f"{code} ({req.number})"
    return req.display_name or req.number


def sla_stage(
    deadline: datetime | None, now: datetime, warn_delta: timedelta
) -> str | None:
    """Что наступило по дедлайну: просрочка, предупреждение или ничего."""
    if deadline is None:
        return None
    if now >= deadline:
        return SLA_EXPIRED
    if warn_delta > timedelta(0) and now >= deadline - warn_delta:
        return SLA_WARN
    return None


def human_left(delta: timedelta) -> str:
    """«2 дн 4 ч», «1 ч 52 мин», «12 мин», «41 с» — без отрицательных значений."""
    total = max(0, int(delta.total_seconds()))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days} дн {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин"
    return f"{seconds} с"


def _esc(value: object) -> str:
    return html.escape(str(value)) if value is not None else ""


def _deadline_text(req: "SDRequest", tz_name: str) -> str:
    display = (req.multicard_deadline or "").strip()
    if display:
        return display
    deadline = localize(req.deadline_at, tz_name)
    return deadline.strftime("%d.%m.%Y %H:%M") if deadline else "—"


def _hours_label(hours: float) -> str:
    text = f"{hours:g}"
    return text.replace(".", ",") + " ч"


def deadline_details(req: "SDRequest", tz_name: str) -> list[str]:
    """Общий блок реквизитов: тип, клиент, адрес, дедлайн СУТС."""
    return [
        f"🏷 <b>Тип:</b> {_esc(req.multicard_request_type or '—')}",
        f"🏢 <b>Клиент:</b> {_esc(req.multicard_name_client or '—')}",
        f"📍 <b>Адрес:</b> {_esc(address_of(req) or '—')}",
        f"⏰ <b>Дедлайн СУТС:</b> {_esc(_deadline_text(req, tz_name))}",
    ]


def format_sla_notification(
    req: "SDRequest",
    kind: str,
    now: datetime,
    tz_name: str,
    warn_hours: float = 2.0,
) -> str:
    """Сообщение о входе в окно предупреждения или о просрочке SLA."""
    deadline = localize(req.deadline_at, tz_name)
    now = localize(now, tz_name) or now
    if deadline is None:
        return ""

    if kind == SLA_EXPIRED:
        header = "🔥 <b>Заявка сгорела по SLA</b>"
        title = f"🚨 <b>{_esc(request_title(req))}</b>"
        tail = f"⌛ <b>Просрочено:</b> {human_left(now - deadline)}"
    else:
        header = f"⏳ <b>До конца SLA {_hours_label(warn_hours)}</b>"
        title = f"🔥 <b>{_esc(request_title(req))}</b>"
        tail = f"⌛ <b>Осталось:</b> {human_left(deadline - now)}"

    lines = [header, "", title]
    lines.extend(deadline_details(req, tz_name))
    lines.append(tail)
    lines.append(f'🔗 <a href="{_esc(req.url)}">Открыть в SD</a>')
    return "\n".join(lines)


def today_blocks(
    requests: list["SDRequest"], now: datetime, tz_name: str
) -> list[str]:
    """Карточки заявок с дедлайном СУТС сегодня (в часовом поясе SD).

    Возвращает список блоков, а не готовый текст: команда режет его на
    сообщения по границам карточек, чтобы карточка не разрывалась.
    """
    items: list[tuple[datetime, SDRequest]] = []
    now = localize(now, tz_name) or now
    for req in requests:
        if req.deadline_at is None or req.resolved_at:
            continue
        deadline = localize(req.deadline_at, tz_name)
        if deadline is None or deadline.date() != now.date():
            continue
        items.append((deadline, req))

    items.sort(key=lambda pair: pair[0])
    burned = sum(1 for deadline, _ in items if deadline <= now)
    header = (
        f"🔥 <b>Сгорят сегодня, {now.strftime('%d.%m.%Y')} — {len(items)}</b>"
    )
    if burned:
        header += f"\n❗ из них уже сгорели: {burned}"

    blocks = [header]
    if not items:
        blocks.append("Сегодня по фильтру ничего не сгорает.")
        return blocks

    for index, (deadline, req) in enumerate(items, 1):
        if deadline <= now:
            state = "🔥 сгорела"
        else:
            state = f"⏳ осталось {human_left(deadline - now)}"
        blocks.append(
            "\n".join(
                [
                    f"{index}. <b>{_esc(request_title(req))}</b> — {state}",
                    f"   🏷 {_esc(req.multicard_request_type or '—')}"
                    f" · 🏢 {_esc(req.multicard_name_client or '—')}",
                    f"   📍 {_esc(address_of(req) or '—')}",
                    f"   ⏰ Дедлайн СУТС: {_esc(_deadline_text(req, tz_name))}",
                    f'   🔗 <a href="{_esc(req.url)}">Открыть в SD</a>',
                ]
            )
        )
    return blocks


DIGEST_LIMIT = 20


def digest_line(req: "SDRequest", tz_name: str) -> str:
    """Одна строка дайджеста: ссылка, когда создана, дедлайн."""
    return (
        f'• <a href="{_esc(req.url)}">{_esc(request_title(req))}</a>'
        f" · создана {_esc(req.sys_created_at or '—')}"
        f" · дедлайн {_esc(_deadline_text(req, tz_name))}"
    )


def format_digest(
    requests: list["SDRequest"],
    label: str,
    tz_name: str,
    limit: int = DIGEST_LIMIT,
) -> str:
    """Короткий дайджест пропущенных/последних заявок одним текстом."""
    if not requests:
        return ""

    lines = [f"🆕 <b>{_esc(label)}: {len(requests)}</b>"]
    for req in requests[:limit]:
        lines.append(digest_line(req, tz_name))
    if len(requests) > limit:
        lines.append(f"… и ещё {len(requests) - limit} — /sd_dump")
    return "\n".join(lines)
