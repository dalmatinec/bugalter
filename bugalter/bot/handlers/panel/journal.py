"""Журнал: последние изменения, кто что сделал."""

import datetime as dt

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from ....db.models import AuditLog, User
from ....domain.errors import AccessDenied
from ....domain.permissions import Perm
from ...ui import h
from .base import kb, show

router = Router(name="panel.journal")

ACTION_TITLES = {
    "work.create": "внесена операция",
    "work.confirm": "подтверждена операция",
    "work.reject": "отклонена операция",
    "work.cancel": "отменена операция",
    "tariff.create": "новая ставка",
    "payroll.bonus_create": "выписана премия",
    "payroll.penalty_create": "выписан штраф",
    "payroll.deduction_create": "выписано удержание",
    "payroll.sign": "подписан документ",
    "payroll.payout_register": "зарегистрирована выплата",
    "payroll.payout_confirm": "подтверждена выплата",
    "payroll.withhold": "удержана часть",
    "period.close": "закрыт период",
    "period.reopen": "переоткрыт период",
    "org.employee_create": "добавлен сотрудник",
    "org.department_create": "добавлено подразделение",
    "org.chat_bind": "привязана группа",
    "org.invite_accept": "сотрудник подключился по ссылке",
    "access.grant": "выдана роль",
    "access.revoke": "отозвана роль",
    "settings.set": "изменена настройка",
    "stock.receipt": "поступление товара",
    "stock.issue": "выдача товара",
    "stock.accept": "товар получен",
    "stock.inventory": "инвентаризация",
}


@router.callback_query(F.data == "p:j")
async def journal(callback: CallbackQuery, session, actor, notifier):
    if not (actor.is_owner or actor.can(Perm.SETTINGS_MANAGE)):
        raise AccessDenied("Журнал доступен владельцу и администратору")
    rows = (
        await session.execute(
            select(AuditLog, User)
            .outerjoin(User, User.id == AuditLog.actor_user_id)
            .order_by(AuditLog.id.desc())
            .limit(20)
        )
    ).all()
    lines = ["📜 <b>Последние изменения</b>", ""]
    for entry, user in rows:
        who = (user.full_name or f"@{user.username}") if user else "система"
        what = ACTION_TITLES.get(entry.action, entry.action)
        lines.append(f"{entry.at.astimezone(dt.UTC):%d.%m %H:%M} · {h(who)} · {h(what)} #{h(entry.entity_id or '')}")
    show(callback, notifier, "\n".join(lines), kb([[("⬅️ Назад", "p:more")]]))
