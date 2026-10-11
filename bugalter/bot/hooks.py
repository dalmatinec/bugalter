"""Действия после отправки сообщения (в отдельной короткой транзакции)."""

from sqlalchemy import update

from ..db.models import StockDocument, WorkEntry


def remember_card(sessionmaker, model: type[WorkEntry] | type[StockDocument], obj_id: int):
    """Запоминает, где лежит карточка, чтобы обновлять её статус при действиях из другого чата."""

    async def hook(sent):
        async with sessionmaker() as session, session.begin():
            await session.execute(
                update(model).where(model.id == obj_id).values(chat_id=sent.chat.id, message_id=sent.message_id)
            )

    return hook
