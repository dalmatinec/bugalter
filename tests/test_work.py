import asyncio
import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from bugalter.db.models import LedgerEntry, WorkEntry
from bugalter.domain.errors import AccessDenied, ValidationError
from bugalter.services import access, ledger, tariffs, work

from .conftest import key

D = Decimal


def d(day: int, month: int = 10) -> dt.date:
    return dt.date(2026, month, day)


async def setup_delivery_tariffs(w):
    # T1..T4 из примера проектирования
    await w.tariff(w.delivery, "300", d(1))
    await w.tariff(w.delivery, "350", d(1), dept=w.north)
    await w.tariff(w.delivery, "400", d(15), dept=w.north, employees=[w.ivan])
    await w.tariff(w.delivery, "420", d(20), dept=w.north, employees=[w.ivan])


async def test_tariff_priority_example(world, session):
    w = world
    await setup_delivery_tariffs(w)
    cases = [
        (w.ivan, w.north, d(10), "350"),
        (w.ivan, w.north, d(16), "400"),
        (w.ivan, w.north, d(21), "420"),
        (w.petr, w.north, d(16), "350"),
        (w.maria, w.south, d(16), "300"),
        (w.ivan, w.south, d(16), "300"),
    ]
    for emp, dept, day, rate in cases:
        tariff = await tariffs.resolve_tariff(session, w.delivery.id, dept.id, emp.id, day)
        assert tariff.rate == D(rate), (emp.code, dept.code, day)
    assert await tariffs.resolve_tariff(session, w.delivery.id, w.north.id, w.ivan.id, d(30, 9)) is None


async def test_employee_wide_tariff_beats_department(world, session):
    w = world
    await w.tariff(w.delivery, "350", d(1), dept=w.north)
    await w.tariff(w.delivery, "500", d(1), employees=[w.ivan])
    tariff = await tariffs.resolve_tariff(session, w.delivery.id, w.north.id, w.ivan.id, d(5))
    assert tariff.rate == D("500")


async def test_backdated_tariff_only_owner_or_full_admin(world, session, settings):
    w = world
    with pytest.raises(AccessDenied):
        await w.tariff(w.delivery, "999", d(1), dept=w.north, actor=w.manager)
    await w.tariff(w.delivery, "300", d(26), dept=w.north, actor=w.manager)  # с сегодняшнего дня — можно
    await w.tariff(w.delivery, "310", d(1), actor=w.admin)
    await w.tariff(w.delivery, "320", d(1), actor=w.owner)


async def create(w, settings, emp, wt, qty, day, actor=None, dept=None, submit=True, k=None):
    return await work.create_entry(
        w.session,
        actor or w.operator,
        settings,
        idempotency_key=k or key("op"),
        department_id=(dept or w.north).id,
        employee_id=emp.id,
        work_type_id=wt.id,
        quantity=D(qty),
        performed_on=day,
        submit=submit,
    )


async def test_operation_amount_snapshot_survives_tariff_change(world, session, settings):
    w = world
    await setup_delivery_tariffs(w)
    res = await create(w, settings, w.ivan, w.delivery, "3", d(16))
    assert res.entry.rate == D("400") and res.entry.amount == D("1200.00")
    await work.confirm_entry(session, w.owner, settings, res.entry.id)
    await w.tariff(w.delivery, "999", d(15), dept=w.north, employees=[w.ivan], actor=w.owner)
    await session.refresh(res.entry)
    assert res.entry.amount == D("1200.00")
    led = await ledger.find_entry(session, "work_entry", res.entry.id, "work")
    assert led.amount == D("1200.00")


async def test_backdated_tariff_reprices_only_unconfirmed(world, session, settings):
    w = world
    await w.tariff(w.packing, "20", d(1), dept=w.north)
    confirmed = (await create(w, settings, w.petr, w.packing, "2", d(10))).entry
    await work.confirm_entry(session, w.owner, settings, confirmed.id)
    pending = (await create(w, settings, w.petr, w.packing, "2", d(12))).entry
    _, repriced = await work.add_tariffs(
        session,
        w.owner,
        settings,
        work_type=w.packing,
        rate=D("25"),
        currency="USD",
        department=w.north,
        employees=None,
        valid_from=d(5),
    )
    assert repriced == 1
    await session.refresh(confirmed)
    await session.refresh(pending)
    assert confirmed.amount == D("40.00")
    assert pending.amount == D("50.00")


async def test_rounding_per_operation(world, session, settings):
    w = world
    await w.tariff(w.packing, "12.35", d(1), dept=w.north)
    res = await create(w, settings, w.petr, w.packing, "1.255", d(10))
    assert res.entry.amount == D("15.50")
    with pytest.raises(ValidationError):
        await create(w, settings, w.petr, w.packing, "1.2555", d(10))


async def test_no_tariff_no_operation(world, settings):
    w = world
    with pytest.raises(ValidationError, match="Нет цены"):
        await create(w, settings, w.ivan, w.delivery, "1", d(10))


async def test_idempotent_create(world, session, settings):
    w = world
    await setup_delivery_tariffs(w)
    first = await create(w, settings, w.ivan, w.delivery, "1", d(16), k="same")
    second = await create(w, settings, w.ivan, w.delivery, "1", d(16), k="same")
    assert first.created and not second.created
    assert first.entry.id == second.entry.id
    count = (await session.execute(select(func.count()).select_from(WorkEntry))).scalar_one()
    assert count == 1


async def test_repeated_operations_same_order_allowed(world, session, settings):
    """По одному заказу сотрудник может получить несколько одинаковых операций — дубли гасит только ключ запроса."""
    w = world
    await setup_delivery_tariffs(w)
    for _ in range(2):
        await work.create_entry(
            session,
            w.operator,
            settings,
            idempotency_key=key(),
            department_id=w.north.id,
            employee_id=w.ivan.id,
            work_type_id=w.delivery.id,
            quantity=D(1),
            performed_on=d(16),
            order_ref="A-1052",
        )
    count = (await session.execute(select(func.count()).select_from(WorkEntry))).scalar_one()
    assert count == 2


async def test_validation_rules(world, settings):
    w = world
    await setup_delivery_tariffs(w)
    with pytest.raises(ValidationError, match="не состоит"):
        await create(w, settings, w.maria, w.delivery, "1", d(16))
    with pytest.raises(ValidationError, match="будущем"):
        await create(w, settings, w.ivan, w.delivery, "1", d(27))
    with pytest.raises(ValidationError):
        await create(w, settings, w.ivan, w.delivery, "1.5", d(16))  # «заказ» — только целые
    with pytest.raises(AccessDenied):
        await create(w, settings, w.ivan, w.delivery, "1", d(16), actor=w.ivan_actor)


async def test_confirm_posts_once(world, session, settings):
    w = world
    await setup_delivery_tariffs(w)
    entry = (await create(w, settings, w.ivan, w.delivery, "2", d(16))).entry
    _, posted, changed = await work.confirm_entry(session, w.owner, settings, entry.id)
    assert changed and posted.amount == D("800.00")
    _, posted2, changed2 = await work.confirm_entry(session, w.owner, settings, entry.id)
    assert not changed2 and posted2 is None
    assert (await ledger.balances(session, w.ivan.id)) == {"USD": D("800.00")}


async def test_concurrent_confirm_single_accrual(world, sessionmaker, session, settings):
    """Два владельца (их может быть несколько в OWNER_TELEGRAM_IDS) одновременно жмут «Подтвердить»: начисление одно."""
    w = world
    await setup_delivery_tariffs(w)
    entry = (await create(w, settings, w.ivan, w.delivery, "1", d(16))).entry
    await access.ensure_owner(session, 21)
    owner2 = await w.actor_for(21, "Второй владелец")
    await session.commit()

    async def confirm(user):
        async with sessionmaker() as s, s.begin():
            actor = await access.actor_for_user(s, user)
            _, _, changed = await work.confirm_entry(s, actor, settings, entry.id)
        return changed

    results = await asyncio.gather(confirm(w.owner.user), confirm(owner2.user))
    assert sorted(results) == [False, True]
    count = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one()
    assert count == 1


async def test_confirm_rules(world, session, settings):
    w = world
    await setup_delivery_tariffs(w)
    entry = (await create(w, settings, w.ivan, w.delivery, "1", d(16))).entry
    with pytest.raises(AccessDenied):
        await work.confirm_entry(session, w.operator, settings, entry.id)  # нет права
    # По умолчанию подтверждает только владелец
    with pytest.raises(AccessDenied, match="только владелец"):
        await work.confirm_entry(session, w.manager, settings, entry.id)
    with pytest.raises(AccessDenied, match="только владелец"):
        await work.reject_entry(session, w.manager, settings, entry.id, None)

    # Владелец разрешил руководителям в настройках
    open_ = dataclasses.replace(settings, confirm_owner_only=False)
    await work.confirm_entry(session, w.manager, open_, entry.id)
    # Руководитель — сам исполнитель операции
    await w.tariff(w.processing, "50", d(1), dept=w.north)
    own = (await create(w, settings, w.anna_emp, w.processing, "1", d(16))).entry
    with pytest.raises(AccessDenied, match="свою пользу"):
        await work.confirm_entry(session, w.manager, open_, own.id)
    await work.confirm_entry(session, w.owner, settings, own.id)  # владельцу можно
    # Автор = подтверждающий при выключенной настройке
    strict = dataclasses.replace(open_, allow_self_confirm=False)
    authored = (await create(w, settings, w.ivan, w.delivery, "1", d(16), actor=w.manager)).entry
    with pytest.raises(AccessDenied, match="allow_self_confirm"):
        await work.confirm_entry(session, w.manager, strict, authored.id)
    await work.confirm_entry(session, w.manager, open_, authored.id)


async def test_cancel_confirmed_creates_reversal(world, session, settings):
    w = world
    await setup_delivery_tariffs(w)
    entry = (await create(w, settings, w.ivan, w.delivery, "1", d(16))).entry
    await work.confirm_entry(session, w.owner, settings, entry.id)
    with pytest.raises(ValidationError, match="причина"):
        await work.cancel_entry(session, w.manager, settings, entry.id, "")
    _, reversal = await work.cancel_entry(session, w.manager, settings, entry.id, "дубль")
    assert reversal.amount == D("-400.00")
    assert (await ledger.balances(session, w.ivan.id)) == {"USD": D("0")}
    # Повторная отмена ничего не делает
    _, again = await work.cancel_entry(session, w.manager, settings, entry.id, "дубль")
    assert again is None


async def test_db_forbids_editing_confirmed_entry_and_ledger(world, session, settings):
    w = world
    await setup_delivery_tariffs(w)
    entry = (await create(w, settings, w.ivan, w.delivery, "1", d(16))).entry
    await work.confirm_entry(session, w.owner, settings, entry.id)
    await session.commit()
    with pytest.raises(DBAPIError, match="нельзя изменить"):
        await session.execute(text("UPDATE work_entries SET amount = 1 WHERE id = :id"), {"id": entry.id})
    await session.rollback()
    with pytest.raises(DBAPIError, match="только добавлением"):
        await session.execute(text("UPDATE ledger_entries SET amount = 1"))
    await session.rollback()
    with pytest.raises(DBAPIError, match="только добавлением"):
        await session.execute(text("DELETE FROM ledger_entries"))
    await session.rollback()
    with pytest.raises(DBAPIError, match="нельзя удалять"):
        await session.execute(text("DELETE FROM work_entries"))
    await session.rollback()
    with pytest.raises(DBAPIError, match="только добавлением"):
        await session.execute(text("UPDATE tariffs SET rate = 1"))
    await session.rollback()


async def test_draft_flow(world, session, settings):
    w = world
    await setup_delivery_tariffs(w)
    entry = (await create(w, settings, w.ivan, w.delivery, "1", d(16), submit=False)).entry
    assert entry.status == "draft"
    entry = await work.update_draft(session, w.operator, settings, entry.id, performed_on=d(21))
    assert entry.amount == D("420.00")
    with pytest.raises(Exception, match="статус"):
        await work.confirm_entry(session, w.manager, settings, entry.id)
    entry, changed = await work.submit_entry(session, w.operator, entry.id)
    assert changed and entry.status == "pending"
    entry, changed = await work.submit_entry(session, w.operator, entry.id)
    assert not changed
