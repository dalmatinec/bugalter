"""Разбор аргументов команд."""

import re
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Department
from ..domain.dates import looks_like_date, parse_date
from ..domain.errors import ValidationError
from ..services import org


def tokens(args: str | None) -> list[str]:
    return (args or "").split()


def need(items: list[str], count: int, usage: str) -> None:
    if len(items) < count:
        raise ValidationError(f"Формат: {usage}")


def parse_id(raw: str) -> int:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        raise ValidationError(f"Не понимаю номер: {raw}")
    return int(digits)


@dataclass
class Extracted:
    rest: list[str]
    dept: str | None = None
    options: dict[str, str] = field(default_factory=dict)


def extract(items: list[str], option_keys: tuple[str, ...] = ()) -> Extracted:
    """Вынимает "@подразделение" и опции вида key=value."""
    rest, dept, options = [], None, {}
    for item in items:
        if item.startswith("@") and len(item) > 1 and dept is None:
            dept = item[1:]
            continue
        if "=" in item:
            key, _, value = item.partition("=")
            if key.lower() in option_keys:
                options[key.lower()] = value
                continue
        rest.append(item)
    return Extracted(rest, dept, options)


async def resolve_department(
    session: AsyncSession, actor, chat_dept: Department | None, code: str | None, perm: str
) -> Department:
    """Подразделение: явное @код → группа, к которой привязан чат → единственное доступное пользователю."""
    if code:
        return await org.get_department(session, code)
    if chat_dept is not None:
        return chat_dept
    single = await org.single_department(session)
    if single is not None:
        return single
    ids = actor.departments_with(perm)
    if ids is not None and len(ids) == 1:
        return await session.get(Department, next(iter(ids)))
    raise ValidationError("Укажите филиал: @название (например @север) или выполните команду в рабочей группе")


@dataclass
class Tail:
    order_ref: str | None = None
    date_token: str | None = None
    comment: str | None = None
    coefficients: list[str] = field(default_factory=list)


def parse_tail(items: list[str]) -> Tail:
    """Хвост операции: "#A-1052 вчера *срочно комментарий" в любом порядке (*код: коэффициент)."""
    tail = Tail()
    comment = []
    for item in items:
        if item.startswith("#") and len(item) > 1 and tail.order_ref is None:
            tail.order_ref = item[1:][:64]
        elif item.startswith("*") and len(item) > 1:
            tail.coefficients.append(item[1:].lower())
        elif tail.date_token is None and not comment and looks_like_date(item):
            tail.date_token = item
        else:
            comment.append(item)
    tail.comment = " ".join(comment) or None
    return tail


def tail_date(tail: Tail, today):
    return parse_date(tail.date_token, today) if tail.date_token else None
