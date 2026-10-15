"""Права доступа. Роль - именованный набор разрешений; выдаётся на подразделение или на все сразу."""

from enum import StrEnum


class Perm(StrEnum):
    ORG_MANAGE = "org.manage"
    ACCESS_MANAGE = "access.manage"
    ROLE_MANAGE = "role.manage"
    SETTINGS_MANAGE = "settings.manage"
    WORKTYPE_MANAGE = "worktype.manage"
    TARIFF_MANAGE = "tariff.manage"
    TARIFF_BACKDATE = "tariff.backdate"
    WORK_CREATE = "work.create"
    WORK_CONFIRM = "work.confirm"
    WORK_CANCEL = "work.cancel"
    WORK_VIEW = "work.view"
    STOCK_MANAGE = "stock.manage"
    STOCK_OPERATE = "stock.operate"
    STOCK_RECEIPT_CONFIRM = "stock.receipt_confirm"
    STOCK_INVENTORY = "stock.inventory"
    STOCK_CASE_DECIDE = "stock.case_decide"
    PAYROLL_ADJUST = "payroll.adjust"
    PAYROLL_SIGN = "payroll.sign"
    PAYROLL_PAYOUT = "payroll.payout"
    PAYROLL_PAYOUT_CONFIRM = "payroll.payout_confirm"
    PERIOD_CLOSE = "period.close"
    PERIOD_REOPEN = "period.reopen"
    REPORT_VIEW = "report.view"
    REPORT_EXPORT = "report.export"


PERM_TITLES: dict[str, str] = {
    Perm.ORG_MANAGE: "подразделения, сотрудники, группы",
    Perm.ACCESS_MANAGE: "выдача и отзыв ролей",
    Perm.ROLE_MANAGE: "создание ролей (только владелец)",
    Perm.SETTINGS_MANAGE: "системные настройки",
    Perm.WORKTYPE_MANAGE: "виды работ",
    Perm.TARIFF_MANAGE: "тарифы",
    Perm.TARIFF_BACKDATE: "тарифы задним числом",
    Perm.WORK_CREATE: "ввод операций",
    Perm.WORK_CONFIRM: "подтверждение операций",
    Perm.WORK_CANCEL: "отмена подтверждённых операций",
    Perm.WORK_VIEW: "просмотр операций подразделения",
    Perm.STOCK_MANAGE: "номенклатура и единицы",
    Perm.STOCK_OPERATE: "складские движения",
    Perm.STOCK_RECEIPT_CONFIRM: "подтверждение получения товара",
    Perm.STOCK_INVENTORY: "инвентаризация",
    Perm.STOCK_CASE_DECIDE: "решения по недостачам",
    Perm.PAYROLL_ADJUST: "выписка премий, штрафов, удержаний",
    Perm.PAYROLL_SIGN: "подпись премий, штрафов, удержаний",
    Perm.PAYROLL_PAYOUT: "регистрация выплат",
    Perm.PAYROLL_PAYOUT_CONFIRM: "подтверждение выплат",
    Perm.PERIOD_CLOSE: "закрытие расчётного периода",
    Perm.PERIOD_REOPEN: "переоткрытие периода (только владелец)",
    Perm.REPORT_VIEW: "отчёты",
    Perm.REPORT_EXPORT: "выгрузка отчётов",
}

ALL_PERMS: frozenset[str] = frozenset(p.value for p in Perm)
# Эти права может иметь только роль owner: их нельзя включить в другую роль или выдать через неё.
OWNER_ONLY_PERMS: frozenset[str] = frozenset({Perm.ROLE_MANAGE, Perm.PERIOD_REOPEN})

OWNER_ROLE = "owner"

DEFAULT_ROLES: dict[str, tuple[str, frozenset[str]]] = {
    "owner": ("Владелец", ALL_PERMS),
    "admin": ("Администратор (полные права)", ALL_PERMS - OWNER_ONLY_PERMS),
    "manager": (
        "Руководитель подразделения",
        frozenset(
            {
                Perm.WORK_CREATE,
                Perm.WORK_CONFIRM,
                Perm.WORK_CANCEL,
                Perm.WORK_VIEW,
                Perm.STOCK_OPERATE,
                Perm.STOCK_RECEIPT_CONFIRM,
                Perm.STOCK_INVENTORY,
                Perm.STOCK_CASE_DECIDE,
                Perm.PAYROLL_ADJUST,
                Perm.PAYROLL_SIGN,
                Perm.PAYROLL_PAYOUT,
                Perm.PAYROLL_PAYOUT_CONFIRM,
                Perm.TARIFF_MANAGE,
                Perm.REPORT_VIEW,
                Perm.REPORT_EXPORT,
            }
        ),
    ),
    "operator": (
        "Оператор",
        frozenset(
            {
                Perm.WORK_CREATE,
                Perm.WORK_CONFIRM,
                Perm.WORK_VIEW,
                Perm.STOCK_OPERATE,
                Perm.STOCK_RECEIPT_CONFIRM,
                Perm.REPORT_VIEW,
            }
        ),
    ),
    "storekeeper": (
        "Кладовщик",
        frozenset({Perm.STOCK_OPERATE, Perm.STOCK_RECEIPT_CONFIRM, Perm.STOCK_INVENTORY, Perm.REPORT_VIEW}),
    ),
    "accountant": (
        "Бухгалтер",
        frozenset(
            {
                Perm.PAYROLL_ADJUST,
                Perm.PAYROLL_PAYOUT,
                Perm.PAYROLL_PAYOUT_CONFIRM,
                Perm.REPORT_VIEW,
                Perm.REPORT_EXPORT,
            }
        ),
    ),
}
