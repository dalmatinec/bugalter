from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ...domain.money import currency_label
from ...domain.permissions import Perm
from .. import ui
from ..ui import h

router = Router(name="common")

HELP_EMPLOYEE = """<b>Ваше меню</b> (в личке с ботом)
📊 Моя работа: что сделано и сколько начислено
💰 Мой расчёт: зарплата, премии, удержания, выплаты, остаток
💸 Запросить выплату: в пределах заработанного (и аванса в долг, если разрешён)
🧾 Мои выплаты: история
{stock}
<b>Отчёт о работе</b> пишите в своей рабочей группе одной строкой на каждую работу:
<code>#доставка 3</code>, <code>#фасовка05 20</code>. Число после тега: сколько сделали (без числа значит 1).
Бот посчитает по цене из настроек и отправит на подтверждение."""

HELP_STOCK = "📦 Мои товары: что сейчас числится на вас\n"

HELP_GROUP = """<b>В рабочей группе</b>
/op: внести работу кнопками
Или строками с тегами в группе сотрудника: <code>#доставка 3</code>
/pending: ждут подтверждения, /ops [сегодня|неделя|месяц]: сводка
/cancel_op &lt;№&gt; &lt;причина&gt;, /reject_op &lt;№&gt; &lt;причина&gt;
<b>Склад</b>: /receive &lt;товар&gt; &lt;кол-во&gt; [ед.] [cost=цена], /issue /move /send /return /sold /writeoff /inv /stock /transit /cases"""

HELP_MANAGER = """<b>Деньги</b> (только в личке), суммы в {cur}
/bonus /penalty /deduct &lt;сотрудник&gt; &lt;сумма&gt; &lt;основание&gt;: выписать
/adjs: на подпись, /adj_cancel &lt;№&gt; &lt;причина&gt;
/payout &lt;сотрудник&gt; &lt;сумма&gt; [аванс|зп] [комментарий], /payouts, /payout_cancel &lt;№&gt; &lt;причина&gt;
/requests: запросы выплат, /balance &lt;сотрудник&gt; [прошлый|период]
/case &lt;№&gt; employee cost: недостача по себестоимости, /batches &lt;товар&gt;: партии
/periods, /period_close &lt;№&gt;, /period_reopen &lt;№&gt; &lt;причина&gt; (владелец)
<b>Отчёты</b>: /report emp|dept|ops|stock|moves|payouts|adj|debts … [период] [xlsx|csv]
<b>Настройка</b>: кнопка ⚙️ Управление"""


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
    if actor.can_anywhere(Perm.WORK_CREATE) or actor.can_anywhere(Perm.STOCK_OPERATE):
        parts.append(HELP_GROUP)
    if actor.can_anywhere(Perm.PAYROLL_ADJUST) or actor.can_anywhere(Perm.PAYROLL_PAYOUT) or actor.is_owner:
        parts.append(HELP_MANAGER.format(cur=f"{currency_label(settings.currency)} ({settings.currency})"))
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
