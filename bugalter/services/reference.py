"""Справочники: роли по умолчанию, единицы, валюты. Заводятся идемпотентно при старте."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Currency, Role, Unit
from ..domain.errors import ValidationError
from ..domain.permissions import DEFAULT_ROLES, Perm
from .audit import audit

DEFAULT_UNITS = (
    ("шт", "штука", 0),
    ("заказ", "заказ", 0),
    ("адрес", "адрес", 0),
    ("заявка", "заявка", 0),
    ("кг", "килограмм", 3),
    ("г", "грамм", 0),
    ("л", "литр", 3),
    ("ч", "час", 2),
    ("уп", "упаковка", 0),
    ("кор", "коробка", 0),
    ("мешок", "мешок", 0),
    ("пачка", "пачка", 0),
    ("сумма", "сумма (база для процента)", 2),
    ("мес", "месяц (для оклада)", 1),
)

DEFAULT_CURRENCIES = (
    ("USD", "Доллар США", 2),
    ("EUR", "Евро", 2),
    ("RUB", "Российский рубль", 2),
    ("KZT", "Тенге", 2),
    ("UZS", "Узбекский сум", 2),
    ("KGS", "Сом", 2),
)


async def ensure_reference_data(session: AsyncSession) -> None:
    for code, (name, perms) in DEFAULT_ROLES.items():
        stmt = insert(Role).values(code=code, name=name, permissions=sorted(perms), is_system=True)
        # Системные роли обновляются при выходе новых версий (появились новые права).
        stmt = stmt.on_conflict_do_update(
            index_elements=[Role.code],
            set_={"name": name, "permissions": sorted(perms)},
            where=Role.is_system,
        )
        await session.execute(stmt)
    for code, name, scale in DEFAULT_UNITS:
        await session.execute(insert(Unit).values(code=code, name=name, scale=scale).on_conflict_do_nothing())
    for code, name, minor in DEFAULT_CURRENCIES:
        await session.execute(insert(Currency).values(code=code, name=name, minor_units=minor).on_conflict_do_nothing())


async def add_unit(session: AsyncSession, actor, code: str, scale: int, name: str) -> Unit:
    actor.require(Perm.STOCK_MANAGE)
    if not 0 <= scale <= 6:
        raise ValidationError("Точность единицы: от 0 до 6 знаков")
    if await session.get(Unit, code) is not None:
        raise ValidationError(f"Единица {code} уже есть. Единицы не изменяются, чтобы не исказить историю")
    unit = Unit(code=code, name=name or code, scale=scale)
    session.add(unit)
    await audit(session, actor.user.id, "unit.add", "unit", code, {"scale": scale, "name": name})
    return unit


async def add_currency(session: AsyncSession, actor, code: str, minor_units: int, name: str) -> Currency:
    actor.require(Perm.SETTINGS_MANAGE)
    code = code.upper()
    if not 0 <= minor_units <= 4:
        raise ValidationError("Знаков после запятой у валюты: от 0 до 4")
    if await session.get(Currency, code) is not None:
        raise ValidationError(f"Валюта {code} уже есть")
    currency = Currency(code=code, name=name or code, minor_units=minor_units)
    session.add(currency)
    await audit(session, actor.user.id, "currency.add", "currency", code, {"minor_units": minor_units})
    return currency


async def get_unit(session: AsyncSession, code: str) -> Unit:
    unit = await session.get(Unit, code)
    if unit is None:
        raise ValidationError(f"Нет такой единицы измерения: {code}")
    return unit


async def get_currency(session: AsyncSession, code: str) -> Currency:
    currency = await session.get(Currency, code.upper())
    if currency is None:
        raise ValidationError(f"Валюта {code.upper()} не поддерживается. Доступны: USD, EUR, RUB, KZT, UZS, KGS")
    return currency


async def list_units(session: AsyncSession) -> list[Unit]:
    return list((await session.execute(select(Unit).order_by(Unit.code))).scalars())


async def currency_codes(session: AsyncSession) -> set[str]:
    return set((await session.execute(select(Currency.code))).scalars())
