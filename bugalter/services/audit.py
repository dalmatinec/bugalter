import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AuditLog


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v) for v in value]
    return value


async def audit(
    session: AsyncSession,
    actor_user_id: int | None,
    action: str,
    entity_type: str,
    entity_id: Any = None,
    data: dict | None = None,
    chat_id: int | None = None,
) -> None:
    """Запись в журнал изменений. Пишется в той же транзакции, что и само изменение."""
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            data=_jsonable(data) if data else None,
            chat_id=chat_id,
        )
    )
