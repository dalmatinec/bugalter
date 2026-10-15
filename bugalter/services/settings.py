"""Системные настройки бота: меняются кнопками в Управлении, Настройки."""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Currency, Setting
from ..domain.errors import ValidationError
from ..domain.money import ROUNDING_MODES, parse_decimal
from .audit import audit


@dataclass(frozen=True)
class SettingSpec:
    default: str
    kind: str  # bool | int | str | currency | rounding | timezone | percent | decimal
    title: str


SPECS: dict[str, SettingSpec] = {
    "currency": SettingSpec("USD", "currency", "Валюта по умолчанию для новых тарифов и выплат"),
    "rounding_mode": SettingSpec("HALF_UP", "rounding", "Режим округления сумм (HALF_UP, HALF_EVEN, DOWN, UP)"),
    "timezone": SettingSpec("UTC", "timezone", "Часовой пояс компании: граница дня и периода"),
    "payroll_split_day": SettingSpec("15", "int", "Последний день первой половины месяца (периоды 1-N и N+1-конец)"),
    "group_show_amounts": SettingSpec("false", "bool", "Показывать суммы начислений в рабочих группах"),
    "confirm_owner_only": SettingSpec(
        "true", "bool", "Работу подтверждает только владелец (если нет, то ещё и руководители)"
    ),
    "payout_confirm_owner_only": SettingSpec(
        "true", "bool", "Выплату подтверждает только владелец (если нет, то ещё руководитель и бухгалтер)"
    ),
    "allow_self_confirm": SettingSpec(
        "true", "bool", "Кто внёс операцию или документ, может сам его подтвердить (кроме записей в свою пользу)"
    ),
    "payout_requires_confirmation": SettingSpec(
        "true", "bool", "Выплата попадает в расчёт только после отдельного подтверждения"
    ),
    "deduction_requires_consent": SettingSpec(
        "false", "bool", "Штраф/удержание проводится только после согласия сотрудника в боте"
    ),
    "deduction_limit_percent": SettingSpec(
        "-",
        "percent",
        "Лимит удержаний за период, % от начислений периода; остаток удерживается в следующих (- значит без лимита)",
    ),
}

_TRUE = {"1", "true", "yes", "on", "да", "вкл"}
_FALSE = {"0", "false", "no", "off", "нет", "выкл"}


@dataclass(frozen=True)
class Settings:
    currency: str
    rounding_mode: str
    timezone: str
    payroll_split_day: int
    group_show_amounts: bool
    confirm_owner_only: bool
    payout_confirm_owner_only: bool
    allow_self_confirm: bool
    payout_requires_confirmation: bool
    deduction_requires_consent: bool
    deduction_limit_percent: Decimal | None
    # Только для тестов: зафиксированная дата «сегодня».
    today_override: dt.date | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def today(self) -> dt.date:
        if self.today_override is not None:
            return self.today_override
        return dt.datetime.now(self.tz).date()


def _parse(key: str, raw: str):
    spec = SPECS[key]
    value = raw.strip()
    if spec.kind == "bool":
        low = value.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValidationError(f"{key}: ожидается да/нет")
    if spec.kind == "int":
        if not value.isdigit():
            raise ValidationError(f"{key}: ожидается целое число")
        number = int(value)
        if key == "payroll_split_day" and not 1 <= number <= 27:
            raise ValidationError("payroll_split_day должен быть от 1 до 27")
        return number
    if spec.kind == "rounding":
        if value.upper() not in ROUNDING_MODES:
            raise ValidationError(f"{key}: допустимо {', '.join(ROUNDING_MODES)}")
        return value.upper()
    if spec.kind == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError(f"Неизвестный часовой пояс: {value}. Пример: Europe/Moscow") from exc
        return value
    if spec.kind == "currency":
        return value.upper()
    if spec.kind == "percent":
        if value in ("-", "", "нет", "no", "off"):
            return None
        number = parse_decimal(value.rstrip("%"), allow_zero=True, max_scale=2, what=key)
        if number > 100:
            raise ValidationError(f"{key}: от 0 до 100")
        return number
    if spec.kind == "decimal":
        return parse_decimal(value, allow_zero=True, max_scale=4, what=key)
    return value


async def get_settings(session: AsyncSession) -> Settings:
    rows = dict((await session.execute(select(Setting.key, Setting.value))).all())
    values = {}
    for key, spec in SPECS.items():
        raw = rows.get(key, spec.default)
        try:
            values[key] = _parse(key, raw)
        except ValidationError:
            values[key] = _parse(key, spec.default)
    return Settings(**values)


async def set_setting(session: AsyncSession, actor, key: str, raw: str) -> str:
    from ..domain.permissions import Perm

    actor.require(Perm.SETTINGS_MANAGE)
    if key not in SPECS:
        raise ValidationError(f"Нет такой настройки: {key}")
    value = _parse(key, raw)
    if SPECS[key].kind == "currency" and await session.get(Currency, value) is None:
        raise ValidationError(f"Валюта {value} не поддерживается. Доступны: USD, EUR, RUB, KZT, UZS, KGS")
    if value is None:
        stored = "-"
    elif isinstance(value, bool):
        stored = str(value).lower()
    elif isinstance(value, Decimal):
        stored = format(value.normalize(), "f")
    else:
        stored = str(value)
    old = await session.get(Setting, key)
    stmt = insert(Setting).values(key=key, value=stored, updated_by=actor.user.id)
    stmt = stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": stored, "updated_by": actor.user.id})
    await session.execute(stmt)
    await audit(
        session, actor.user.id, "settings.set", "setting", key, {"old": old.value if old else None, "new": stored}
    )
    return stored
