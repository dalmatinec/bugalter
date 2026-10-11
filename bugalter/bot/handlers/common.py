from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ...domain.permissions import Perm
from .. import ui
from ..ui import h

router = Router(name="common")

HELP_EMPLOYEE = """<b>Личный кабинет</b> (только в личке с ботом)
📊 Моя работа — /my_work [прошлый|месяц|01.10-15.10]
💰 Мой расчёт — /my_salary [прошлый]
📦 Товар на руках — /my_stock
💸 Запрос выплаты — /request_payout &lt;сумма&gt; [комментарий]
🧾 Мои выплаты — /my_payouts
/id — мой Telegram ID (передайте администратору)"""

HELP_GROUP = """<b>В рабочей группе</b>
/op — внести операцию (мастер с кнопками)
/op &lt;сотрудник&gt; &lt;работа&gt; &lt;кол-во&gt; [#заказ] [вчера|ДД.ММ] [комментарий] — быстрый ввод
/pending — ждут подтверждения · /ops [сегодня|неделя|месяц] — сводка
/cancel_op &lt;№&gt; &lt;причина&gt; · /reject_op &lt;№&gt; &lt;причина&gt;
<b>Склад</b>: /receive /issue /move /send /return /sold /writeoff /inv /stock /transit /cases"""

HELP_MANAGER = """<b>Деньги</b> (только в личке)
/bonus /penalty /deduct &lt;сотрудник&gt; &lt;сумма&gt; [USD] &lt;основание&gt; — выписать
/adjs — на подпись · /adj_cancel &lt;№&gt; &lt;причина&gt;
/payout &lt;сотрудник&gt; &lt;сумма&gt; [USD] [аванс|зп] [комментарий] · /payouts · /payout_cancel &lt;№&gt; &lt;причина&gt;
/requests — запросы выплат · /balance &lt;сотрудник&gt; [прошлый|период]
/periods · /period_close &lt;№&gt; · /period_reopen &lt;№&gt; &lt;причина&gt; (владелец)
<b>Отчёты</b>: /report emp|dept|ops|stock|moves|payouts|adj|debts … [период] [xlsx|csv]
<b>Настройка</b>: /admin"""


@router.message(CommandStart())
async def start(message: Message, actor, is_private: bool, notifier):
    if not is_private:
        notifier.answer(message, "Я работаю в этой группе. Команды: /help")
        return
    lines = [f"Здравствуйте, {h(message.from_user.full_name)}!"]
    if actor.employee:
        lines.append(f"Вы — сотрудник <b>{h(actor.employee.full_name)}</b> (код {h(actor.employee.code)}).")
    else:
        lines.append(
            f"Ваш Telegram ID: <code>{message.from_user.id}</code>. "
            "Передайте его администратору, чтобы он привязал вас к карточке сотрудника."
        )
    if actor.grants:
        lines.append("Ваши роли: " + ", ".join(sorted({g.role_code for g in actor.grants})) + ". Справка: /help")
    notifier.answer(message, "\n".join(lines), ui.main_menu())


@router.message(Command("help"))
@router.message(F.text == ui.BTN_HELP)
async def help_cmd(message: Message, actor, notifier):
    parts = [HELP_EMPLOYEE]
    if actor.can_anywhere(Perm.WORK_CREATE) or actor.can_anywhere(Perm.STOCK_OPERATE):
        parts.append(HELP_GROUP)
    if actor.can_anywhere(Perm.PAYROLL_ADJUST) or actor.can_anywhere(Perm.PAYROLL_PAYOUT) or actor.is_owner:
        parts.append(HELP_MANAGER)
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
