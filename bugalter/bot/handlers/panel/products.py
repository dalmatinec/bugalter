"""Товары: номенклатура для склада, единица учёта (гр, кг, шт) и фасовка, например 1 шт = 20 гр."""

import calendar
import datetime as dt
import re
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ....db.models import Product
from ....domain.errors import ValidationError
from ....domain.money import fmt_number, fmt_qty, parse_decimal
from ....services import org, reference, stats, stock
from ...ui import h
from .base import ask, cancel_kb, form, kb, show

router = Router(name="panel.products")

# В чём считать товар на складе и как называть упаковку. Единицы для работ (заказ, час) здесь не нужны.
STOCK_UNITS = ("г", "кг", "шт")
UNIT_LABELS = {"г": "гр"}
# «Крам 20G», «Кофе 250 гр», «Мука 1кг»: вес одной пачки прямо в названии.
_WEIGHT_IN_NAME = re.compile(r"(\d+(?:[.,]\d+)?)\s*(g|г|гр|kg|кг)\b", re.IGNORECASE)


async def products_view(session):
    lines = ["📦 <b>Товары</b>", "Сколько есть сейчас: на складе, у сотрудников и выдано, но ещё не принято.", ""]
    rows = []
    places = await stats.stock_by_product(session)
    for product, _units in await stock.list_products(session):
        p = places.get(product.id)
        u = product.base_unit
        line = f"• <b>{h(product.name)}</b>: на складе {fmt_qty(p.warehouse if p else Decimal(0), u)}"
        if p and p.people:
            line += f", у сотрудников {fmt_qty(p.people, u)}"
        if p and p.transit:
            line += f", в пути {fmt_qty(p.transit, u)}"
        lines.append(line)
        rows.append([(f"📦 {product.name}", f"p:pr:v:{product.id}")])
    if not rows:
        lines.append("Пока нет товаров. Нажмите ➕ Новый товар.")
    rows.append([("📊 Статистика: сколько сделано и продано", f"p:pr:st:{dt.date.today():%Y%m}")])
    rows.append([("➕ Новый товар", "p:pr:add"), ("📦 Склад", "p:st")])
    rows.append([("⬅️ В меню", "p:home")])
    return "\n".join(lines), kb(rows)


async def product_view(session, product_id: int):
    product = await session.get(Product, product_id)
    u = product.base_unit
    lines = [f"📦 <b>{h(product.name)}</b>", ""]
    for row in await stock.balances(session):
        if row.product.id == product.id:
            lines.append(f"• {h(row.location_title)}: {fmt_qty(row.qty, u)}")
    if len(lines) == 2:
        lines.append("Сейчас нигде нет.")
    rows = [
        [("📦 Склад: приход, выдача", "p:st")],
        [("🗑 Удалить товар", f"p:pr:dl:{product.id}")],
        [("⬅️ Товары", "p:pr")],
    ]
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data.startswith("p:pr:dl:"))
async def product_delete_ask(callback: CallbackQuery, session, actor, notifier):
    stock.require_catalog(actor)
    product = await session.get(Product, int(callback.data.split(":")[3]))
    show(
        callback,
        notifier,
        f"🗑 Удалить товар <b>{h(product.name)}</b>?\n"
        "Если по нему уже были приходы и выдачи, он просто пропадёт из списков, история останется.",
        kb([[("🗑 Да, удалить", f"p:pr:dly:{product.id}"), ("✖️ Нет", f"p:pr:v:{product.id}")]]),
    )


@router.callback_query(F.data.startswith("p:pr:dly:"))
async def product_delete(callback: CallbackQuery, session, actor, notifier):
    product = await session.get(Product, int(callback.data.split(":")[3]))
    if product is None or not product.is_active:
        notifier.toast(callback, "Уже удалён")
        show(callback, notifier, *await products_view(session))
        return
    name = product.name
    await stock.remove_product(session, actor, product)
    await session.flush()
    notifier.toast(callback, f"Удалён: {name}")
    show(callback, notifier, *await products_view(session))


def _month(code: str) -> dt.date:
    return dt.date(int(code[:4]), int(code[4:]), 1)


@router.callback_query(F.data.startswith("p:pr:st:"))
async def product_stats(callback: CallbackQuery, session, settings, notifier):
    first = _month(callback.data.split(":")[3])
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    lines = [f"📊 <b>Статистика по товарам</b> · {first:%m.%Y}", "Только подтверждённые отчёты сотрудников.", ""]
    for item in await stats.product_stats(session, first, last):
        lines.append(f"<b>{h(item.name)}</b>")
        for line in item.works.values():
            text = f"   {h(line.work)}: {fmt_number(line.pieces)} шт"
            if line.weight:
                parts = ", ".join(f"{fmt_number(n)} по {fmt_number(w)}" for w, n in sorted(line.by_weight.items()))
                text += f", всего {fmt_qty(line.weight, line.unit)} ({parts})"
            lines.append(text)
    if len(lines) == 3:
        lines.append("За этот месяц отчётов с товарами нет.")
    prev = (first - dt.timedelta(days=1)).replace(day=1)
    nxt = last + dt.timedelta(days=1)
    nav = [("⬅️ " + f"{prev:%m.%Y}", f"p:pr:st:{prev:%Y%m}")]
    if nxt <= settings.today():
        nav.append((f"{nxt:%m.%Y} ➡️", f"p:pr:st:{nxt:%Y%m}"))
    show(callback, notifier, "\n".join(lines), kb([nav, [("⬅️ Товары", "p:pr")]]))


@router.callback_query(F.data == "p:pr")
async def products(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    show(callback, notifier, *await products_view(session))


@router.callback_query(F.data.startswith("p:pr:v:"))
async def product(callback: CallbackQuery, session, notifier):
    show(callback, notifier, *await product_view(session, int(callback.data.split(":")[3])))


@router.callback_query(F.data == "p:pr:add")
async def product_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    stock.require_catalog(actor)
    await ask(state, "product_name")
    show(callback, notifier, "✏️ Название товара, например: <b>Крам 20г</b> или <b>Трюфель</b>", cancel_kb("p:pr"))


@form("product_name")
async def form_product_name(message, state, session, actor, notifier, **_):
    name = " ".join(message.text.split())
    await state.update_data(product_name=name)
    # Сохраняем как написали («Крам 20г», «Трюфель»): ничего больше не спрашиваем.
    await _create(message, state, session, actor, notifier, name, "ед")


async def _create(event, state, session, actor, notifier, name: str, unit: str) -> None:
    sku = await org.unique_code(session, Product.sku, name)
    created = await stock.create_product(session, actor, sku, unit, name)
    await state.clear()
    await session.flush()
    text, markup = await product_view(session, created.id)
    show(event, notifier, f"✅ Товар добавлен: <b>{h(name)}</b>\n\n" + text, markup)


async def _stock_units(session) -> list[str]:
    known = {u.code for u in await reference.list_units(session)}
    return [code for code in STOCK_UNITS if code in known]


@router.callback_query(F.data.startswith("p:pr:u:"))
async def product_unit_choice(callback: CallbackQuery, state: FSMContext, session, actor, notifier):
    data = await state.get_data()
    if "product_name" not in data:
        raise ValidationError("Начните заново: Товары → Новый товар")
    await _create(callback, state, session, actor, notifier, data["product_name"], callback.data.split(":", 3)[3])


@router.callback_query(F.data.startswith("p:pr:pk:"))
async def product_pack(callback: CallbackQuery, session, notifier):
    product_id = int(callback.data.split(":")[3])
    product = await session.get(Product, product_id)
    have = {u.unit_code for u in dict(await stock.list_products(session)).get(product, [])}
    codes = [c for c in await _stock_units(session) if c != product.base_unit and c not in have]
    units = [(UNIT_LABELS.get(code, code), f"p:pr:pu:{product_id}:{code}") for code in codes]
    if not units:
        raise ValidationError("Все фасовки для этого товара уже заданы")
    grid = [units]
    grid.append([("⬅️ Назад", f"p:pr:v:{product_id}")])
    show(
        callback,
        notifier,
        f"Фасовка нужна, только если {h(product.name)} приходит или выдаётся не в {h(product.base_unit)}.\n"
        f"Например считаете в гр, а привозят пачками по 20 гр: выберите шт и напишите 20. "
        f"Тогда можно писать не 600 гр, а 30 шт.\nНе нужно? Нажмите Назад.",
        kb(grid),
    )


@router.callback_query(F.data.startswith("p:pr:pu:"))
async def product_pack_unit(callback: CallbackQuery, state: FSMContext, session, notifier):
    _, _, _, product_id, unit = callback.data.split(":", 4)
    product = await session.get(Product, int(product_id))
    await ask(state, "product_pack", product_id=product.id, unit=unit)
    show(
        callback,
        notifier,
        f"Сколько {h(product.base_unit)} в 1 {h(unit)}? Например, если в пачке 20 {h(product.base_unit)}, "
        "напишите <b>20</b>",
        cancel_kb(f"p:pr:v:{product.id}"),
    )


@form("product_pack")
async def form_product_pack(message, state, session, actor, notifier, data, **_):
    product = await session.get(Product, data["product_id"])
    factor = parse_decimal(message.text, what="количество")
    await stock.add_product_unit(session, actor, product, data["unit"], factor)
    await state.clear()
    await session.flush()
    notifier.answer(message, *await product_view(session, product.id))
