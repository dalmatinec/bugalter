"""Товары: номенклатура для склада, единица учёта (гр, кг, шт) и фасовка, например 1 шт = 20 гр."""

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ....db.models import Product
from ....domain.errors import ValidationError
from ....domain.money import fmt_number, fmt_qty, parse_decimal
from ....domain.permissions import Perm
from ....services import org, reference, stock
from ...ui import h
from .base import ask, cancel_kb, form, kb, rate_text, show

router = Router(name="panel.products")

# В чём считать товар на складе и как называть упаковку. Единицы для работ (заказ, час) здесь не нужны.
STOCK_UNITS = ("г", "кг", "шт")
UNIT_LABELS = {"г": "гр"}
# «Крам 20G», «Кофе 250 гр», «Мука 1кг»: вес одной пачки прямо в названии.
_WEIGHT_IN_NAME = re.compile(r"(\d+(?:[.,]\d+)?)\s*(g|г|гр|kg|кг)\b", re.IGNORECASE)


async def products_view(session):
    lines = ["📦 <b>Товары</b>", ""]
    rows = []
    for product, units in await stock.list_products(session):
        alt = ", ".join(f"1 {u.unit_code} = {fmt_number(u.factor)} {product.base_unit}" for u in units)
        lines.append(f"• <b>{h(product.name)}</b> ({h(product.base_unit)}){' · ' + alt if alt else ''}")
        rows.append([(f"📦 {product.name}", f"p:pr:v:{product.id}")])
    if not rows:
        lines.append("Пока нет товаров.")
    rows.append([("➕ Новый товар", "p:pr:add")])
    rows.append([("⬅️ Назад", "p:more")])
    return "\n".join(lines), kb(rows)


async def product_view(session, product_id: int):
    product = await session.get(Product, product_id)
    lines = [
        f"📦 <b>{h(product.name)}</b>  <i>артикул для команд: {h(product.sku)}</i>",
        f"Учёт в: {h(product.base_unit)}",
    ]
    for batch, remaining in (await stock.product_batches(session, product))[:10]:
        cost = rate_text(batch.unit_cost, batch.currency) if batch.unit_cost is not None else "без цены"
        lines.append(f"• партия от {batch.received_on:%d.%m}: осталось {fmt_qty(remaining, product.base_unit)}, {cost}")
    rows = [[("➕ Фасовка", f"p:pr:pk:{product.id}")], [("⬅️ Товары", "p:pr")]]
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "p:pr")
async def products(callback: CallbackQuery, state: FSMContext, session, notifier):
    await state.clear()
    show(callback, notifier, *await products_view(session))


@router.callback_query(F.data.startswith("p:pr:v:"))
async def product(callback: CallbackQuery, session, notifier):
    show(callback, notifier, *await product_view(session, int(callback.data.split(":")[3])))


@router.callback_query(F.data == "p:pr:add")
async def product_add(callback: CallbackQuery, state: FSMContext, actor, notifier):
    actor.require(Perm.STOCK_MANAGE)
    await ask(state, "product_name")
    show(callback, notifier, "✏️ Название товара, например: <b>Кофе зерно</b>", cancel_kb("p:pr"))


@form("product_name")
async def form_product_name(message, state, session, notifier, **_):
    name = " ".join(message.text.split())
    await state.update_data(product_name=name)
    units = [(UNIT_LABELS.get(code, code), f"p:pr:u:{code}") for code in await _stock_units(session)]
    grid = [units, [("✖️ Отмена", "p:pr")]]
    text = f"{h(name)}: в чём считать остаток на складе? гр, кг или шт."
    if match := _WEIGHT_IN_NAME.search(name):
        text += (
            f"\n\n💡 В названии вес {h(match.group(0))}. Если это готовая пачка такого веса, "
            "считайте в <b>шт</b>: так проще пересчитывать."
        )
    else:
        text += "\nНапример сырьё на вес в гр или кг, готовые пачки в шт."
    notifier.answer(message, text, kb(grid))


async def _stock_units(session) -> list[str]:
    known = {u.code for u in await reference.list_units(session)}
    return [code for code in STOCK_UNITS if code in known]


@router.callback_query(F.data.startswith("p:pr:u:"))
async def product_unit_choice(callback: CallbackQuery, state: FSMContext, session, actor, notifier):
    data = await state.get_data()
    if "product_name" not in data:
        raise ValidationError("Начните заново: Товары → Новый товар")
    unit = callback.data.split(":", 3)[3]
    sku = await org.unique_code(session, Product.sku, data["product_name"])
    created = await stock.create_product(session, actor, sku, unit, data["product_name"])
    await state.clear()
    await session.flush()
    show(callback, notifier, *await product_view(session, created.id))


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
