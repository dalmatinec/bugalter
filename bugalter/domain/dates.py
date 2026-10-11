"""Даты: расчётные периоды и диапазоны отчётов."""

import calendar
import datetime as dt
import re

from .errors import ValidationError


def semi_monthly_bounds(day: dt.date, split_day: int) -> tuple[dt.date, dt.date]:
    """Период «два раза в месяц»: 1..split_day и split_day+1..конец месяца."""
    last = calendar.monthrange(day.year, day.month)[1]
    split = min(split_day, last - 1)
    if day.day <= split:
        return day.replace(day=1), day.replace(day=split)
    return day.replace(day=split + 1), day.replace(day=last)


_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), ("y", "m", "d")),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$"), ("d", "m", "y")),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})$"), ("d", "m")),
)


def parse_date(raw: str, today: dt.date) -> dt.date:
    token = raw.strip().lower()
    if token in ("сегодня", "today"):
        return today
    if token in ("вчера", "yesterday"):
        return today - dt.timedelta(days=1)
    if token in ("позавчера",):
        return today - dt.timedelta(days=2)
    for pattern, order in _DATE_PATTERNS:
        match = pattern.match(token)
        if not match:
            continue
        parts = dict(zip(order, (int(g) for g in match.groups()), strict=True))
        year = parts.get("y", today.year)
        try:
            value = dt.date(year, parts["m"], parts["d"])
        except ValueError as exc:
            raise ValidationError(f"Нет такой даты: {raw}") from exc
        # «31.12» в январе — это прошлый год.
        if "y" not in parts and value > today:
            value = value.replace(year=year - 1)
        return value
    raise ValidationError(f"Не понимаю дату «{raw}». Формат: ДД.ММ, ДД.ММ.ГГГГ или ГГГГ-ММ-ДД")


def looks_like_date(token: str) -> bool:
    token = token.strip().lower()
    return token in ("сегодня", "вчера", "позавчера", "today", "yesterday") or any(
        p.match(token) for p, _ in _DATE_PATTERNS
    )


RANGE_WORDS = {
    "today": "сегодня",
    "сегодня": "сегодня",
    "yesterday": "вчера",
    "вчера": "вчера",
    "week": "неделя",
    "неделя": "неделя",
    "month": "месяц",
    "месяц": "месяц",
    "prev_month": "прошлый месяц",
    "прошлый_месяц": "прошлый месяц",
}


def parse_range(raw: str | None, today: dt.date) -> tuple[dt.date, dt.date]:
    """Диапазон отчёта: сегодня, вчера, неделя, месяц, прошлый_месяц, ДД.ММ, ДД.ММ-ДД.ММ, A..B."""
    if not raw:
        return today, today
    token = raw.strip().lower()
    word = RANGE_WORDS.get(token)
    if word == "сегодня":
        return today, today
    if word == "вчера":
        day = today - dt.timedelta(days=1)
        return day, day
    if word == "неделя":
        start = today - dt.timedelta(days=today.weekday())
        return start, today
    if word == "месяц":
        return today.replace(day=1), today
    if word == "прошлый месяц":
        last_prev = today.replace(day=1) - dt.timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    for sep in ("..", "—", "–", "-"):
        if sep in token and not re.match(r"^\d{4}-\d{2}-\d{2}$", token):
            left, _, right = token.partition(sep) if sep != "-" else _split_dash(token)
            if left and right:
                start, end = parse_date(left, today), parse_date(right, today)
                if start > end:
                    raise ValidationError("Начало периода позже конца")
                return start, end
    day = parse_date(token, today)
    return day, day


def _split_dash(token: str) -> tuple[str, str, str]:
    # «01.10-15.10» или «2026-10-01-2026-10-15»
    iso = re.match(r"^(\d{4}-\d{2}-\d{2})-(\d{4}-\d{2}-\d{2})$", token)
    if iso:
        return iso.group(1), "-", iso.group(2)
    return token.partition("-")


def looks_like_range(token: str) -> bool:
    token = token.strip().lower()
    if token in RANGE_WORDS or looks_like_date(token):
        return True
    return bool(re.match(r"^[\d.\-]+(\.\.|—|–|-)[\d.\-]+$", token))
