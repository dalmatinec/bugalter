"""Выполненные операции: черновик → ожидает подтверждения → подтверждено / отклонено / отменено."""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Currency, Employee, LedgerEntry, Tariff, Unit, WorkEntry, WorkType
from ..domain.errors import AccessDenied, Conflict, NotFound, ValidationError
from ..domain.money import currency_step, round_to_step, scale_of
from ..domain.permissions import Perm
from . import ledger, org, payroll, tariffs
from .audit import audit
from .settings import Settings

STATUS_TITLES = {
    "draft": "черновик",
    "pending": "ожидает подтверждения",
    "confirmed": "подтверждено",
    "rejected": "отклонено",
    "cancelled": "отменено",
}

MAX_BACKDATE_DAYS = 62


HUNDRED = Decimal(100)


def _plain(value: Decimal) -> str:
    """Decimal без экспоненты: 400, 0.25 (а не 4E+2)."""
    return format(value.normalize(), "f")


def compute_pieces(
    work_type: WorkType,
    pieces_raw: list[tuple[Decimal, Decimal]],
    coefficient: Decimal,
    minor_units: int,
    settings: Settings,
) -> tuple[Decimal, list[dict]]:
    """Сумма операции из кусков (кол-во, ставка). Округляется один раз; куски получают округлённые доли,
    последний — остаток, поэтому сумма строк расшифровки всегда равна итогу."""
    step = work_type.rounding_step or currency_step(minor_units)
    mode = work_type.rounding_mode or settings.rounding_mode

    def raw_amount(qty: Decimal, rate: Decimal) -> Decimal:
        if work_type.calc_method == "fixed":
            return rate
        if work_type.calc_method == "percent":
            return qty * rate / HUNDRED
        return qty * rate

    raws = [raw_amount(q, r) * coefficient for q, r in pieces_raw]
    total = round_to_step(sum(raws, Decimal(0)), step, mode)
    pieces = []
    assigned = Decimal(0)
    for index, ((qty, rate), raw) in enumerate(zip(pieces_raw, raws, strict=True)):
        part = total - assigned if index == len(raws) - 1 else round_to_step(raw, step, mode)
        assigned += part
        pieces.append({"qty": _plain(qty), "rate": _plain(rate), "amount": _plain(part)})
    return total, pieces


def compute_amount(
    work_type: WorkType, quantity: Decimal, rate: Decimal, minor_units: int, settings: Settings
) -> Decimal:
    """Сумма операции без ступеней и коэффициентов."""
    return compute_pieces(work_type, [(quantity, rate)], Decimal(1), minor_units, settings)[0]


@dataclass
class EntryResult:
    entry: WorkEntry
    created: bool


@dataclass
class Price:
    tariff: Tariff
    amount: Decimal
    coefficient: Decimal
    coefficient_codes: list[str]
    details: dict


async def confirmed_volume(
    session: AsyncSession,
    settings: Settings,
    *,
    employee_id: int,
    work_type_id: int,
    department_id: int,
    on_date: dt.date,
    exclude_entry_id: int | None = None,
) -> Decimal:
    """Подтверждённый объём сотрудника по виду работ в расчётном периоде даты on_date (для ступеней)."""
    period = await ledger.period_for_date(session, on_date, settings)
    query = select(func.coalesce(func.sum(WorkEntry.quantity), 0)).where(
        WorkEntry.employee_id == employee_id,
        WorkEntry.work_type_id == work_type_id,
        WorkEntry.department_id == department_id,
        WorkEntry.status == "confirmed",
        WorkEntry.performed_on.between(period.date_from, period.date_to),
    )
    if exclude_entry_id is not None:
        query = query.where(WorkEntry.id != exclude_entry_id)
    return (await session.execute(query)).scalar_one()


async def price_entry(
    session: AsyncSession,
    settings: Settings,
    wt: WorkType,
    *,
    department_id: int,
    employee_id: int,
    performed_on: dt.date,
    quantity: Decimal,
    coefficient_codes: list[str] | None = None,
    tariff: Tariff | None = None,
    exclude_entry_id: int | None = None,
) -> Price:
    if tariff is None:
        tariff = await tariffs.resolve_tariff(session, wt.id, department_id, employee_id, performed_on)
    if tariff is None:
        raise ValidationError(
            f"Нет цены на {wt.name} на {performed_on:%d.%m.%Y} для этого сотрудника и подразделения. "
            "Без цены операцию провести нельзя: задайте её в Управлении, Работы и цены"
        )
    currency = await session.get(Currency, tariff.currency)
    coefficient, codes = await tariffs.resolve_coefficients(session, coefficient_codes)
    tiers = await tariffs.tiers_of(session, tariff.id) if wt.calc_method == "per_unit" else []
    details: dict = {"method": wt.calc_method}
    if tiers:
        prior = await confirmed_volume(
            session,
            settings,
            employee_id=employee_id,
            work_type_id=wt.id,
            department_id=department_id,
            on_date=performed_on,
            exclude_entry_id=exclude_entry_id,
        )
        pieces_raw = tariffs.split_by_tiers(prior, quantity, tariff.rate, tiers)
        details["tiered"] = True
        details["prior_volume"] = _plain(prior)
    else:
        pieces_raw = [(quantity, tariff.rate)]
    amount, pieces = compute_pieces(wt, pieces_raw, coefficient, currency.minor_units, settings)
    details["pieces"] = pieces
    if codes:
        details["coefficient"] = _plain(coefficient)
        details["coefficients"] = codes
    return Price(tariff, amount, coefficient, codes, details)


def _apply_price(entry: WorkEntry, price: Price) -> None:
    entry.tariff_id = price.tariff.id
    entry.rate = price.tariff.rate
    entry.currency = price.tariff.currency
    entry.amount = price.amount
    entry.coefficient = price.coefficient
    entry.coefficient_codes = price.coefficient_codes or None
    entry.calc_details = price.details


async def create_entry(
    session: AsyncSession,
    actor,
    settings: Settings,
    *,
    idempotency_key: str,
    department_id: int,
    employee_id: int,
    work_type_id: int,
    quantity: Decimal,
    performed_on: dt.date | None = None,
    order_ref: str | None = None,
    comment: str | None = None,
    submit: bool = True,
    chat_id: int | None = None,
    coefficient_codes: list[str] | None = None,
    self_report: bool = False,
) -> EntryResult:
    """Создаёт операцию. Повтор с тем же idempotency_key возвращает уже созданную запись.

    self_report: сотрудник сам отчитывается о своей работе (теги в группе). Права на ввод ему не нужно,
    запись всегда уходит на подтверждение.
    """
    existing = await _by_key(session, idempotency_key)
    if existing:
        return EntryResult(existing, False)
    if self_report and actor.is_self(employee_id):
        submit = True
    else:
        actor.require(Perm.WORK_CREATE, department_id)
    employee = await session.get(Employee, employee_id)
    if employee is None or not employee.is_active:
        raise NotFound("Сотрудник не найден или уволен")
    if not await org.is_member(session, employee_id, department_id):
        raise ValidationError(f"{employee.full_name} не состоит в этом подразделении")
    wt = await session.get(WorkType, work_type_id)
    if wt is None or not wt.is_active:
        raise NotFound("Вид работ не найден или отключён")
    if not await tariffs.work_type_allowed(session, wt, department_id):
        raise ValidationError(f"{wt.name} недоступен в этом подразделении")
    unit = await session.get(Unit, wt.unit_code)
    if quantity <= 0:
        raise ValidationError("Количество должно быть больше нуля")
    if scale_of(quantity) > unit.scale:
        raise ValidationError(f"Для {unit.code} допускается не больше {unit.scale} знаков после запятой")
    today = settings.today()
    performed_on = performed_on or today
    if performed_on > today:
        raise ValidationError("Дата выполнения не может быть в будущем")
    if (today - performed_on).days > MAX_BACKDATE_DAYS and not actor.can(Perm.TARIFF_BACKDATE):
        raise ValidationError(f"Операции старше {MAX_BACKDATE_DAYS} дней вводит только владелец/администратор")
    price = await price_entry(
        session,
        settings,
        wt,
        department_id=department_id,
        employee_id=employee_id,
        performed_on=performed_on,
        quantity=quantity,
        coefficient_codes=coefficient_codes,
    )
    tariff, amount = price.tariff, price.amount
    now = func.now()
    stmt = (
        insert(WorkEntry)
        .values(
            idempotency_key=idempotency_key,
            department_id=department_id,
            employee_id=employee_id,
            work_type_id=wt.id,
            order_ref=(order_ref or None),
            performed_on=performed_on,
            quantity=quantity,
            unit_code=wt.unit_code,
            tariff_id=tariff.id,
            rate=tariff.rate,
            currency=tariff.currency,
            amount=amount,
            coefficient=price.coefficient,
            coefficient_codes=price.coefficient_codes or None,
            calc_details=price.details,
            status="pending" if submit else "draft",
            comment=comment or None,
            created_by=actor.user.id,
            submitted_at=now if submit else None,
            chat_id=chat_id,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(WorkEntry.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is None:  # параллельный запрос с тем же ключом успел первым
        return EntryResult(await _by_key(session, idempotency_key), False)
    entry = await session.get(WorkEntry, new_id)
    await audit(
        session,
        actor.user.id,
        "work.create",
        "work_entry",
        entry.id,
        {
            "employee_id": employee_id,
            "work_type": wt.code,
            "quantity": quantity,
            "rate": tariff.rate,
            "tariff_id": tariff.id,
            "amount": amount,
            "status": entry.status,
            "order_ref": order_ref,
            "calc": price.details,
        },
        chat_id,
    )
    return EntryResult(entry, True)


async def _by_key(session: AsyncSession, key: str) -> WorkEntry | None:
    return (await session.execute(select(WorkEntry).where(WorkEntry.idempotency_key == key))).scalar_one_or_none()


async def entries_by_key_prefix(session: AsyncSession, prefix: str) -> list[WorkEntry]:
    """Все операции одного сообщения-отчёта (ключи вида tag:<чат>:<сообщение>:<строка>)."""
    query = select(WorkEntry).where(WorkEntry.idempotency_key.startswith(prefix, autoescape=True))
    return list((await session.execute(query.order_by(WorkEntry.id))).scalars())


async def _locked(session: AsyncSession, entry_id: int) -> WorkEntry:
    entry = await session.get(WorkEntry, entry_id, with_for_update=True, populate_existing=True)
    if entry is None:
        raise NotFound(f"Операция #{entry_id} не найдена")
    return entry


async def update_draft(
    session: AsyncSession,
    actor,
    settings: Settings,
    entry_id: int,
    *,
    performed_on: dt.date | None = None,
    order_ref: str | None = None,
    comment: str | None = None,
) -> WorkEntry:
    entry = await _locked(session, entry_id)
    if entry.status != "draft":
        raise Conflict("Изменять можно только черновик")
    if entry.created_by != actor.user.id:
        raise AccessDenied("Черновик может менять только его автор")
    if performed_on is not None and performed_on != entry.performed_on:
        if performed_on > settings.today():
            raise ValidationError("Дата выполнения не может быть в будущем")
        wt = await session.get(WorkType, entry.work_type_id)
        price = await price_entry(
            session,
            settings,
            wt,
            department_id=entry.department_id,
            employee_id=entry.employee_id,
            performed_on=performed_on,
            quantity=entry.quantity,
            coefficient_codes=entry.coefficient_codes,
        )
        entry.performed_on = performed_on
        _apply_price(entry, price)
    if order_ref is not None:
        entry.order_ref = order_ref or None
    if comment is not None:
        entry.comment = comment or None
    await audit(
        session, actor.user.id, "work.update_draft", "work_entry", entry.id, {"performed_on": entry.performed_on}
    )
    return entry


async def submit_entry(session: AsyncSession, actor, entry_id: int) -> tuple[WorkEntry, bool]:
    entry = await _locked(session, entry_id)
    if entry.status != "draft":
        return entry, False
    if entry.created_by != actor.user.id:
        raise AccessDenied("Отправить черновик может только его автор")
    entry.status = "pending"
    entry.submitted_at = func.now()
    await audit(session, actor.user.id, "work.submit", "work_entry", entry.id)
    return entry, True


OWNER_ONLY_CONFIRM = "Подтверждает только владелец. Разрешить операторам: Управление, Ещё, Настройки"


def _check_decider(actor, entry: WorkEntry, settings: Settings) -> None:
    """Кто может подтвердить или отклонить операцию."""
    actor.require(Perm.WORK_CONFIRM, entry.department_id)
    if actor.is_owner:
        return
    if settings.confirm_owner_only:
        raise AccessDenied(OWNER_ONLY_CONFIRM)


def _check_confirmer(actor, entry: WorkEntry, settings: Settings) -> None:
    _check_decider(actor, entry, settings)
    if actor.is_owner:
        return
    if actor.is_self(entry.employee_id):
        raise AccessDenied("Нельзя подтверждать операции в свою пользу")
    if entry.created_by == actor.user.id and not settings.allow_self_confirm:
        raise AccessDenied("Автор операции не может сам её подтвердить (настройка allow_self_confirm)")


async def confirm_entry(
    session: AsyncSession, actor, settings: Settings, entry_id: int
) -> tuple[WorkEntry, LedgerEntry | None, bool]:
    """Подтверждение и начисление в одной транзакции. Повторное нажатие ничего не начисляет."""
    entry = await _locked(session, entry_id)
    if entry.status == "confirmed":
        return entry, None, False
    if entry.status != "pending":
        raise Conflict(f"{entry.number}: подтвердить нельзя, статус {STATUS_TITLES[entry.status]}")
    _check_confirmer(actor, entry, settings)
    wt = await session.get(WorkType, entry.work_type_id)
    if (entry.calc_details or {}).get("tiered"):
        # Ступени считаются от подтверждённого объёма: блокируем сотрудника, чтобы две операции,
        # подтверждаемые одновременно, не посчитались по одной и той же ступени.
        await session.execute(select(Employee.id).where(Employee.id == entry.employee_id).with_for_update())
        tariff = await session.get(Tariff, entry.tariff_id)
        price = await price_entry(
            session,
            settings,
            wt,
            department_id=entry.department_id,
            employee_id=entry.employee_id,
            performed_on=entry.performed_on,
            quantity=entry.quantity,
            coefficient_codes=entry.coefficient_codes,
            tariff=tariff,
            exclude_entry_id=entry.id,
        )
        _apply_price(entry, price)
    entry.status = "confirmed"
    entry.decided_by = actor.user.id
    entry.decided_at = func.now()
    posted = None
    if entry.amount != 0:
        posted, _ = await ledger.post(
            session,
            settings,
            employee_id=entry.employee_id,
            department_id=entry.department_id,
            currency=entry.currency,
            amount=entry.amount,
            entry_type="work",
            source_type="work_entry",
            source_id=entry.id,
            business_date=entry.performed_on,
            posted_by=actor.user.id,
            comment=f"{wt.name} × {entry.quantity.normalize():f} {entry.unit_code}",
        )
    await audit(session, actor.user.id, "work.confirm", "work_entry", entry.id, {"amount": entry.amount})
    if posted is not None and entry.amount > 0:
        # Выросли начисления периода — можно удержать очередную часть отложенных удержаний.
        await payroll.apply_withholdings(session, settings, entry.employee_id, entry.currency, actor.user.id)
    return entry, posted, True


async def reject_entry(
    session: AsyncSession, actor, settings: Settings, entry_id: int, comment: str | None
) -> WorkEntry:
    entry = await _locked(session, entry_id)
    if entry.status == "rejected":
        return entry
    if entry.status != "pending":
        raise Conflict(f"{entry.number}: отклонить нельзя, статус {STATUS_TITLES[entry.status]}")
    _check_decider(actor, entry, settings)
    entry.status = "rejected"
    entry.decided_by = actor.user.id
    entry.decided_at = func.now()
    entry.decision_comment = comment
    await audit(session, actor.user.id, "work.reject", "work_entry", entry.id, {"comment": comment})
    return entry


async def cancel_entry(
    session: AsyncSession, actor, settings: Settings, entry_id: int, reason: str | None
) -> tuple[WorkEntry, LedgerEntry | None]:
    """Черновик/ожидающую отменяет автор или руководитель; подтверждённую — только с правом work.cancel
    и с причиной: создаётся сторно в текущем открытом периоде."""
    entry = await _locked(session, entry_id)
    if entry.status == "cancelled":
        return entry, None
    if entry.status == "rejected":
        raise Conflict(f"{entry.number} уже отклонена")
    reversal = None
    if entry.status in ("draft", "pending"):
        if entry.created_by != actor.user.id:
            actor.require(Perm.WORK_CONFIRM, entry.department_id)
    else:  # confirmed
        actor.require(Perm.WORK_CANCEL, entry.department_id)
        if not reason or not reason.strip():
            raise ValidationError("Для отмены подтверждённой операции нужна причина")
        if actor.is_self(entry.employee_id) and not actor.is_owner:
            raise AccessDenied("Нельзя отменять операции в свою пользу")
        original = await ledger.find_entry(session, "work_entry", entry.id, "work")
        if original is not None:
            reversal = await ledger.reverse(
                session, settings, original, posted_by=actor.user.id, comment=f"Отмена {entry.number}: {reason}"
            )
    entry.status = "cancelled"
    entry.cancelled_by = actor.user.id
    entry.cancelled_at = func.now()
    entry.cancel_reason = reason
    await audit(
        session,
        actor.user.id,
        "work.cancel",
        "work_entry",
        entry.id,
        {"reason": reason, "reversal_id": reversal.id if reversal else None},
    )
    return entry, reversal


async def reprice_open_entries(
    session: AsyncSession, actor, settings: Settings, work_type_id: int, from_date: dt.date
) -> int:
    """После тарифа задним числом пересчитывает черновики и ожидающие (подтверждённые не трогаем)."""
    entries = (
        await session.execute(
            select(WorkEntry)
            .where(
                WorkEntry.work_type_id == work_type_id,
                WorkEntry.status.in_(("draft", "pending")),
                WorkEntry.performed_on >= from_date,
            )
            .with_for_update()
        )
    ).scalars()
    wt = await session.get(WorkType, work_type_id)
    changed = 0
    for entry in entries:
        price = await price_entry(
            session,
            settings,
            wt,
            department_id=entry.department_id,
            employee_id=entry.employee_id,
            performed_on=entry.performed_on,
            quantity=entry.quantity,
            coefficient_codes=entry.coefficient_codes,
            exclude_entry_id=entry.id,
        )
        tariff, amount = price.tariff, price.amount
        if tariff.id != entry.tariff_id or amount != entry.amount:
            old = {"tariff_id": entry.tariff_id, "amount": entry.amount}
            _apply_price(entry, price)
            changed += 1
            await audit(
                session,
                actor.user.id,
                "work.reprice",
                "work_entry",
                entry.id,
                {"old": old, "new": {"tariff_id": tariff.id, "amount": amount}},
            )
    return changed


async def get_entry(session: AsyncSession, entry_id: int) -> WorkEntry:
    entry = await session.get(WorkEntry, entry_id)
    if entry is None:
        raise NotFound(f"Операция #{entry_id} не найдена")
    return entry


async def list_entries(
    session: AsyncSession,
    *,
    department_id: int | None = None,
    employee_id: int | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    statuses: tuple[str, ...] | None = None,
    limit: int | None = None,
) -> list[tuple[WorkEntry, WorkType, Employee]]:
    query = (
        select(WorkEntry, WorkType, Employee)
        .join(WorkType, WorkType.id == WorkEntry.work_type_id)
        .join(Employee, Employee.id == WorkEntry.employee_id)
        .order_by(WorkEntry.performed_on.desc(), WorkEntry.id.desc())
    )
    if department_id is not None:
        query = query.where(WorkEntry.department_id == department_id)
    if employee_id is not None:
        query = query.where(WorkEntry.employee_id == employee_id)
    if date_from is not None:
        query = query.where(WorkEntry.performed_on >= date_from)
    if date_to is not None:
        query = query.where(WorkEntry.performed_on <= date_to)
    if statuses:
        query = query.where(WorkEntry.status.in_(statuses))
    if limit:
        query = query.limit(limit)
    return [tuple(r) for r in (await session.execute(query)).all()]


async def add_tariffs(session: AsyncSession, actor, settings: Settings, **kwargs) -> tuple[list, int]:
    """Создаёт тариф(ы); если дата начала в прошлом — пересчитывает непроведённые операции."""
    created = await tariffs.create_tariffs(session, actor, settings, **kwargs)
    repriced = 0
    valid_from = created[0].valid_from
    if valid_from < settings.today():
        repriced = await reprice_open_entries(session, actor, settings, created[0].work_type_id, valid_from)
    return created, repriced
