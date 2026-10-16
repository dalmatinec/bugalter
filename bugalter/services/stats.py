"""Статистика по товарам: сколько штук и какого веса сделано, продано, развезено (по отчётам о работе)."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Location, Product, StockBalance, WorkEntry, WorkType


@dataclass
class WorkLine:
    work: str
    unit: str
    pieces: Decimal = Decimal(0)
    weight: Decimal = Decimal(0)  # всего в единицах работы (г, кг), если работа по весу
    by_weight: dict[Decimal, Decimal] = field(default_factory=dict)  # вес одной штуки -> штук


@dataclass
class ProductStats:
    name: str
    works: dict[str, WorkLine] = field(default_factory=dict)


async def product_stats(session: AsyncSession, date_from: dt.date, date_to: dt.date) -> list[ProductStats]:
    """Подтверждённые отчёты с товаром за даты: по товару и виду работы, штуки и вес."""
    rows = (
        await session.execute(
            select(
                WorkEntry.product_name,
                WorkType.name,
                WorkEntry.unit_code,
                WorkEntry.piece_weight,
                func.sum(WorkEntry.pieces),
                func.sum(WorkEntry.quantity),
            )
            .join(WorkType, WorkType.id == WorkEntry.work_type_id)
            .where(
                WorkEntry.status == "confirmed",
                WorkEntry.product_name.isnot(None),
                WorkEntry.performed_on.between(date_from, date_to),
            )
            .group_by(WorkEntry.product_name, WorkType.name, WorkEntry.unit_code, WorkEntry.piece_weight)
        )
    ).all()
    result: dict[str, ProductStats] = {}
    for product, work, unit, weight, pieces, qty in rows:
        key = product.lower()
        stats = result.setdefault(key, ProductStats(product))
        line = stats.works.setdefault(work, WorkLine(work, unit))
        line.pieces += pieces or 0
        if weight is not None:
            line.weight += qty
            line.by_weight[weight] = line.by_weight.get(weight, Decimal(0)) + (pieces or 0)
    return sorted(result.values(), key=lambda s: s.name.lower())


@dataclass
class StockPlace:
    warehouse: Decimal = Decimal(0)
    transit: Decimal = Decimal(0)
    people: Decimal = Decimal(0)


async def stock_by_product(session: AsyncSession) -> dict[int, StockPlace]:
    """Остатки по товарам во всех филиалах: на складе, у сотрудников, в пути."""
    rows = (
        await session.execute(
            select(StockBalance.product_id, Location.kind, func.sum(StockBalance.qty))
            .join(Location, Location.id == StockBalance.location_id)
            .where(Location.kind.in_(("warehouse", "employee", "transit")))
            .group_by(StockBalance.product_id, Location.kind)
        )
    ).all()
    result: dict[int, StockPlace] = defaultdict(StockPlace)
    for product_id, kind, qty in rows:
        place = result[product_id]
        if kind == "warehouse":
            place.warehouse += qty
        elif kind == "employee":
            place.people += qty
        else:
            place.transit += qty
    return result


async def product_names(session: AsyncSession) -> list[str]:
    return [p.name for p in (await session.execute(select(Product).order_by(Product.name))).scalars()]
