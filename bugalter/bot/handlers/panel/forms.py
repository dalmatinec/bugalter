"""Ответ текстом на вопрос панели уходит в обработчик формы, зарегистрированный через @form."""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from .base import FORMS, AdminForm

router = Router(name="panel.forms")


@router.message(AdminForm.input, F.text & ~F.text.startswith("/"))
async def form_input(message: Message, state: FSMContext, session, actor, settings, notifier, bot: Bot):
    data = await state.get_data()
    handler = FORMS.get(data.get("form"))
    if handler is None:
        await state.clear()
        return
    await handler(
        message, state=state, session=session, actor=actor, settings=settings, notifier=notifier, data=data, bot=bot
    )
