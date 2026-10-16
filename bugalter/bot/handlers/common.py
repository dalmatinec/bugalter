from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ...domain.money import currency_label
from ...domain.permissions import Perm
from .. import ui
from ..ui import h

router = Router(name="common")

# Отчёт о работе: одинаково объясняется в помощи, в группе и при подключении сотрудника.
REPORT_HOWTO = """<b>Как отчитаться о работе</b> (в своей рабочей группе)
Пишите: товар, работа, вес одной штуки, сколько штук
<code>Крам факт 0.5 13</code>  значит 13 шт крама по 0,5, работа Факт
<code>Трюфель готовые 1 4</code>  4 шт трюфеля по 1
<code>Доставка 3</code>  работа без веса: просто сколько раз
Можно с решёткой: <code>#фасовка крам 1 40</code>
Несколько строк в одном сообщении, каждая строка отдельно.
Можно столбиком: <code>Крам факт</code>, ниже <code>13х05</code> и <code>4х1</code> (штук х вес).
Вес 0,5 можно писать как 0.5, 0,5 или 05.
За вчера или другой день допишите строку <code>вчера</code> или <code>14.10</code>.
Бот пришлёт карточку с расчётом, оператор проверит и подтвердит. Только подтверждённое идёт в зарплату.
Названия работ (Факт, Готовые, Доставка) такие, как в списке у руководителя.
Ошиблись и отправили не то? Под карточкой нажмите 🗑 Ошибся, удалить и напишите заново.
Готовый товар на склад записывает оператор, вам ничего писать не нужно."""

HELP_EMPLOYEE = (
    """<b>Ваше меню</b> (кнопки внизу, здесь в личке с ботом)
📊 Моя работа: что вы сделали и что уже подтверждено
💰 Мой расчёт: сколько заработали, аванс, сколько вам положено сейчас
💸 Попросить аванс: напишите сумму, просьба уйдёт владельцу. Одобрят, и бот напишет вам.
   Аванс потом вычитается из зарплаты
🧾 Мои выплаты: что вам уже выплатили
{stock}
"""
    + REPORT_HOWTO
)

HELP_STOCK = """📦 Мои товары: что сейчас числится на вас.
   Когда вам выдают товар, приходит сообщение с кнопкой ✅ Получено. Нажмите её, только если товар правда у вас.
"""

HELP_OPERATOR = """<b>Вы оператор</b>
✅ В группах сотрудников приходят карточки отчётов. Проверьте, что работа правда сделана, и нажмите
Подтвердить. Если нет, Отклонить. Свои отчёты вы не подтверждаете.
📦 Принять готовый товар: в группе сотрудника напишите
<code>#принял
Крам 05 13
Трюфель 1 17</code>
(товар, вес одной штуки, сколько штук). Бот покажет, что понял, и запишет только после кнопки
Записать на склад. Ошиблись после записи: кнопка ↩️ Отменить приём.
📦 Склад: кнопка ⚙️ Управление, Склад. Там приход, выдача сотрудникам, приём готового, возврат.
   Внутри есть кнопка ❓ Как работать со складом."""

HELP_MANAGER = """<b>Управление</b> (кнопка ⚙️ Управление), суммы в {cur}
👥 Сотрудники: добавить человека. Бот даст ссылку, перешлите её сотруднику.
💰 Работы и цены: за что и сколько платим. Название работы сотрудник пишет в отчёте.
💸 Выплаты: выдать аванс, выплатить зарплату с вычетом аванса или без, просьбы об авансе.
📦 Склад: приход, выдача, приём готового, остатки по каждому сотруднику.
🏷 Товары: что есть в наличии и статистика, сколько сделано и продано по весам.
📊 Отчёты: Срок 1, Срок 2, месяц, квартал или свои даты. Есть кнопка скачать Excel.
🧾 Расходы: реклама, сотрудничество, аренда и другие траты, итоги по месяцам.
💬 Группы: как подключить рабочую группу сотрудника.
➕ Ещё: доплаты, товары, настройки (валюта, сроки выплат, бэкап), доступ, журнал.

<b>Обычный порядок</b>
1. Добавьте сотрудника и перешлите ему ссылку.
2. Заведите работы и цены.
3. Создайте группу, добавьте туда сотрудника и бота, сделайте бота администратором.
4. Сотрудник пишет отчёты, оператор подтверждает.
5. В день зарплаты: Выплаты, сотрудник, С вычетом или Без вычета аванса."""


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, session, actor, is_private: bool, notifier):
    from .panel import accept_invite_start, can_admin

    if not is_private:
        return
    if command.args and command.args.startswith("inv_"):
        await accept_invite_start(message, command.args[4:], session, actor, notifier)
        return
    admin = can_admin(actor)
    lines = []
    if actor.employee:
        lines.append(f"Вы: <b>{h(actor.employee.full_name)}</b>. Меню внизу.")
    if admin:
        lines.append("Настройка: кнопка ⚙️ Управление внизу.")
    notifier.answer(message, "\n".join(lines) or "Меню внизу.", await ui.main_menu_for(session, actor, admin))


@router.message(Command("help"))
@router.message(F.text == ui.BTN_HELP)
async def help_cmd(message: Message, session, actor, settings, notifier):
    parts = []
    if actor.employee:
        stock = HELP_STOCK if await ui.has_stock(session, actor.employee.id) else ""
        parts.append(HELP_EMPLOYEE.format(stock=stock))
    manager = actor.is_owner or actor.can_anywhere(Perm.PAYROLL_PAYOUT) or actor.can_anywhere(Perm.ORG_MANAGE)
    if manager:
        parts.append(HELP_MANAGER.format(cur=f"{currency_label(settings.currency)} ({settings.currency})"))
    elif actor.can_anywhere(Perm.WORK_CONFIRM) or actor.can_anywhere(Perm.STOCK_OPERATE):
        parts.append(HELP_OPERATOR)
    if not parts:
        parts.append("Меню внизу. Если его нет, нажмите /start.")
    notifier.answer(message, "\n\n".join(parts))


@router.message(Command("id"))
async def my_id(message: Message, notifier):
    text = f"Ваш Telegram ID: <code>{message.from_user.id}</code>"
    if message.chat.type != "private":
        text += f"\nID этой группы: <code>{message.chat.id}</code>"
    notifier.answer(message, text)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, notifier):
    await state.clear()
    notifier.answer(message, "Действие отменено.")
