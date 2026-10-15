"""Товары: номенклатура для склада, единица учёта и фасовки (уп, коробка)."""

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
    units = [(f"{u.code}", f"p:pr:u:{u.code}") for u in await reference.list_units(session)]
    grid = [units[i : i + 3] for i in range(0, len(units), 3)]
    grid.append([("✖️ Отмена", "p:pr")])
    notifier.answer(message, f"{h(name)}: в чём ведём учёт на складе?", kb(grid))


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
    units = [(u.code, f"p:pr:pu:{product_id}:{u.code}") for u in await reference.list_units(session)]
    grid = [units[i : i + 3] for i in range(0, len(units), 3)]
    grid.append([("⬅️ Назад", f"p:pr:v:{product_id}")])
    show(callback, notifier, "Как называется фасовка?", kb(grid))


@router.callback_query(F.data.startswith("p:pr:pu:"))
async def product_pack_unit(callback: CallbackQuery, state: FSMContext, session, notifier):
    _, _, _, product_id, unit = callback.data.split(":", 4)
    product = await session.get(Product, int(product_id))
    await ask(state, "product_pack", product_id=product.id, unit=unit)
    show(
        callback,
        notifier,
        f"Сколько {h(product.base_unit)} в 1 {h(unit)}? Например <b>0.25</b>",
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
