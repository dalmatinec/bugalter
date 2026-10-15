"""Настройки: валюта, часовой пояс, периоды, округление, лимиты и переключатели правил."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from ....db.models import Tariff
from ....domain.money import currency_label, fmt_number
from ....services import reference
from ....services import settings as settings_service
from ....services.settings import SPECS
from ...ui import h
from .base import ask, cancel_kb, form, kb, show

router = Router(name="panel.settings")

TIMEZONES = [
    ("Москва", "Europe/Moscow"),
    ("Киев", "Europe/Kyiv"),
    ("Минск", "Europe/Minsk"),
    ("Алматы", "Asia/Almaty"),
    ("Ташкент", "Asia/Tashkent"),
    ("Бишкек", "Asia/Bishkek"),
    ("Дубай", "Asia/Dubai"),
    ("Стамбул", "Europe/Istanbul"),
    ("Лондон", "Europe/London"),
    ("Нью-Йорк", "America/New_York"),
    ("UTC", "UTC"),
]
ROUNDING_TITLES = {
    "HALF_UP": "по правилам математики",
    "HALF_EVEN": "банковское",
    "DOWN": "всегда вниз",
    "UP": "всегда вверх",
}
# Переключатель: (что значит, коротко для кнопки).
TOGGLES = {
    "confirm_owner_only": (
        "✅ Работу подтверждает только владелец. Если выключить, подтверждают ещё операторы и руководители",
        "Работу подтв. владелец",
    ),
    "payout_confirm_owner_only": (
        "💸 Выплату подтверждает только владелец. Если выключить, ещё руководитель и бухгалтер",
        "Выплату подтв. владелец",
    ),
    "payout_requires_confirmation": (
        "🧾 Выплата засчитывается только после подтверждения. Если выключить, сразу при записи",
        "Подтверждать выплаты",
    ),
    "allow_self_confirm": (
        "✍️ Руководитель может сам подтвердить то, что сам внёс (но не в свою пользу)",
        "Своё подтверждать",
    ),
    "group_show_amounts": ("👁 Суммы видны в рабочих группах", "Суммы в группах"),
    "deduction_requires_consent": ("🤝 Штраф и удержание только с согласия сотрудника", "Согласие на удержание"),
}


async def settings_view(settings):
    tz_title = next((t for t, z in TIMEZONES if z == settings.timezone), settings.timezone)
    limit = (
        f"{fmt_number(settings.deduction_limit_percent)} % от начислений периода"
        if settings.deduction_limit_percent is not None
        else "нет"
    )
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        f"💱 Валюта: <b>{currency_label(settings.currency)} ({settings.currency})</b>",
        f"🕐 Часовой пояс: <b>{h(tz_title)}</b>",
        f"📅 Расчётные периоды: <b>1-{settings.payroll_split_day} и {settings.payroll_split_day + 1}-конец месяца</b>",
        f"🔢 Округление: <b>{ROUNDING_TITLES.get(settings.rounding_mode, settings.rounding_mode)}</b>",
        f"📉 Лимит удержаний: <b>{limit}</b>",
    ]
    lines.append("")
    for key, (title, _short) in TOGGLES.items():
        lines.append(f"{title}: <b>{'да' if getattr(settings, key) else 'нет'}</b>")
    rows = [
        [("💱 Валюта", "p:s:cur"), ("🕐 Часовой пояс", "p:s:tz")],
        [("📅 Периоды", "p:s:split"), ("🔢 Округление", "p:s:rnd")],
        [("📉 Лимит удержаний", "p:s:lim")],
    ]
    toggles = [
        (f"{'✅' if getattr(settings, key) else '⬜️'} {short}", f"p:s:t:{key}") for key, (_, short) in TOGGLES.items()
    ]
    rows += [toggles[i : i + 2] for i in range(0, len(toggles), 2)]
    rows.append([("⬅️ Назад", "p:more")])
    return "\n".join(lines), kb(rows)


async def _set(session, actor, key: str, value: str):
    await settings_service.set_setting(session, actor, key, value)
    await session.flush()
    return await settings_service.get_settings(session)


@router.callback_query(F.data == "p:s")
async def settings_section(callback: CallbackQuery, state: FSMContext, settings, notifier):
    await state.clear()
    show(callback, notifier, *await settings_view(settings))


@router.callback_query(F.data.startswith("p:s:t:"))
async def settings_toggle(callback: CallbackQuery, session, actor, settings, notifier):
    key = callback.data.split(":")[3]
    new = await _set(session, actor, key, "false" if getattr(settings, key) else "true")
    show(callback, notifier, *await settings_view(new))


@router.callback_query(F.data == "p:s:cur")
async def settings_currency(callback: CallbackQuery, session, settings, notifier):
    codes = sorted(await reference.currency_codes(session))
    buttons = [
        (
            ("✅ " if c == settings.currency else "") + (f"{currency_label(c)} {c}" if currency_label(c) != c else c),
            f"p:s:cur:{c}",
        )
        for c in codes
    ]
    grid = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    grid.append([("⬅️ Назад", "p:s")])
    show(callback, notifier, "💱 Валюта для новых ставок и выплат. Уже внесённые суммы не меняются.", kb(grid))


@router.callback_query(F.data.startswith("p:s:cur:"))
async def settings_currency_set(callback: CallbackQuery, session, actor, notifier):
    new = await _set(session, actor, "currency", callback.data.split(":")[3])
    text, markup = await settings_view(new)
    other = await session.scalar(select(func.count()).select_from(Tariff).where(Tariff.currency != new.currency))
    if other:
        text = (
            f"⚠️ Цены, заданные раньше, остались в старой валюте ({other} шт.). "
            "Задайте их заново в Работах и ценах, чтобы считать в новой.\n\n" + text
        )
    show(callback, notifier, text, markup)


@router.callback_query(F.data == "p:s:tz")
async def settings_tz(callback: CallbackQuery, settings, notifier):
    buttons = [
        (("✅ " if z == settings.timezone else "") + title, f"p:s:tz:{i}") for i, (title, z) in enumerate(TIMEZONES)
    ]
    grid = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    grid.append([("✏️ Другой", "p:s:in:timezone"), ("⬅️ Назад", "p:s")])
    show(callback, notifier, "🕐 Часовой пояс: по нему считаются сегодня и границы периодов.", kb(grid))


@router.callback_query(F.data.startswith("p:s:tz:"))
async def settings_tz_set(callback: CallbackQuery, session, actor, notifier):
    new = await _set(session, actor, "timezone", TIMEZONES[int(callback.data.split(":")[3])][1])
    show(callback, notifier, *await settings_view(new))


@router.callback_query(F.data == "p:s:split")
async def settings_split(callback: CallbackQuery, notifier):
    grid = [[(f"1-{d} и {d + 1}-конец", f"p:s:v:payroll_split_day:{d}")] for d in (10, 14, 15, 20)]
    grid.append([("✏️ Другой день", "p:s:in:payroll_split_day"), ("⬅️ Назад", "p:s")])
    show(callback, notifier, "📅 Выплаты два раза в месяц: где граница первой половины?", kb(grid))


@router.callback_query(F.data == "p:s:rnd")
async def settings_rounding(callback: CallbackQuery, notifier):
    grid = [[(title, f"p:s:v:rounding_mode:{code}")] for code, title in ROUNDING_TITLES.items()]
    grid.append([("⬅️ Назад", "p:s")])
    show(callback, notifier, "🔢 Как округлять суммы до центов?", kb(grid))


@router.callback_query(F.data == "p:s:lim")
async def settings_limit(callback: CallbackQuery, notifier):
    grid = [[(f"{p} %", f"p:s:v:deduction_limit_percent:{p}") for p in (10, 20, 30, 50)]]
    grid.append([("Без лимита", "p:s:v:deduction_limit_percent:-"), ("✏️ Другой", "p:s:in:deduction_limit_percent")])
    grid.append([("⬅️ Назад", "p:s")])
    show(
        callback,
        notifier,
        "📉 Сколько процентов от начислений периода можно удержать штрафами/удержаниями? "
        "Остальное удержится из следующих начислений.",
        kb(grid),
    )


@router.callback_query(F.data.startswith("p:s:v:"))
async def settings_value(callback: CallbackQuery, session, actor, notifier):
    _, _, _, key, value = callback.data.split(":", 4)
    new = await _set(session, actor, key, value)
    show(callback, notifier, *await settings_view(new))


@router.callback_query(F.data.startswith("p:s:in:"))
async def settings_input(callback: CallbackQuery, state: FSMContext, notifier):
    key = callback.data.split(":")[3]
    await ask(state, "setting", key=key)
    show(callback, notifier, f"✏️ {h(SPECS[key].title)}\nНапишите значение:", cancel_kb("p:s"))


@form("setting")
async def form_setting(message, state, session, actor, notifier, data, **_):
    new = await _set(session, actor, data["key"], message.text.strip())
    await state.clear()
    notifier.answer(message, *await settings_view(new))
