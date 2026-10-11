"""Тесты работают с настоящим PostgreSQL: триггеры, блокировки и уникальные ключи — часть логики.

TEST_DATABASE_URL по умолчанию: postgresql+asyncpg://bugalter:bugalter@localhost:5432/bugalter_test
"""

import dataclasses
import datetime as dt
import itertools
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bugalter.db.base import Base
from bugalter.db.models import AccessGrant
from bugalter.services import access, org, reference, tariffs
from bugalter.services.settings import get_settings

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://bugalter:bugalter@localhost:5432/bugalter_test")
TODAY = dt.date(2026, 10, 26)


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    env = {**os.environ, "DATABASE_URL": TEST_DB}
    for args in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run([sys.executable, "-m", "alembic", *args], env=env, cwd=ROOT, check=True, capture_output=True)


@pytest.fixture
async def engine():
    eng = create_async_engine(TEST_DB, poolclass=NullPool)
    async with eng.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(sessionmaker):
    async with sessionmaker() as s:
        await reference.ensure_reference_data(s)
        await s.commit()
        yield s


_keys = itertools.count(1)


def key(prefix: str = "t") -> str:
    return f"{prefix}:{next(_keys)}"


class World:
    """Условные данные из примеров проектирования: подразделения «Север» и «Юг», Иван, Пётр, Мария."""

    def __init__(self, session, settings):
        self.session = session
        self.settings = settings

    async def actor_for(self, telegram_id: int, name: str):
        return await access.load_actor(self.session, telegram_id, None, name)

    async def grant(self, actor, role: str, dept=None):
        self.session.add(AccessGrant(user_id=actor.user.id, role_code=role, department_id=dept.id if dept else None))
        await self.session.flush()
        return await self.reload(actor)

    async def reload(self, actor):
        return await access.actor_for_user(self.session, actor.user)

    async def build(self):
        s = self.session
        self.owner = await self.actor_for(1, "Владелец")
        await access.ensure_owner(s, 1)
        self.owner = await self.reload(self.owner)
        self.north = await org.create_department(s, self.owner, "north", "Север")
        self.south = await org.create_department(s, self.owner, "south", "Юг")
        self.ivan = await org.create_employee(s, self.owner, "ivan", "Иван Иванов", "курьер")
        self.petr = await org.create_employee(s, self.owner, "petr", "Пётр Петров", "фасовщик")
        self.maria = await org.create_employee(s, self.owner, "maria", "Мария", "курьер")
        self.anna_emp = await org.create_employee(s, self.owner, "anna", "Анна", "руководитель")
        for emp, dept in (
            (self.ivan, self.north),
            (self.petr, self.north),
            (self.maria, self.south),
            (self.ivan, self.south),
            (self.anna_emp, self.north),
        ):
            await org.attach_to_department(s, self.owner, emp, dept)

        self.ivan_actor = await self.actor_for(10, "Иван")
        await org.link_user(s, self.owner, self.ivan, self.ivan_actor.user)
        self.ivan_actor = await self.reload(self.ivan_actor)
        self.manager = await self.actor_for(20, "Анна")
        await org.link_user(s, self.owner, self.anna_emp, self.manager.user)
        self.manager = await self.grant(await self.reload(self.manager), "manager", self.north)
        self.operator = await self.grant(await self.actor_for(30, "Ольга"), "operator", self.north)
        self.accountant = await self.grant(await self.actor_for(40, "Бухгалтер"), "accountant")
        self.admin = await self.grant(await self.actor_for(50, "Админ"), "admin")

        self.delivery = await tariffs.create_work_type(s, self.owner, "доставка", "заказ", "per_unit", "Доставка")
        self.packing = await tariffs.create_work_type(s, self.owner, "фасовка", "кг", "per_unit", "Фасовка")
        self.processing = await tariffs.create_work_type(s, self.owner, "обработка", "заказ", "per_unit", "Обработка")
        await s.flush()
        return self

    async def tariff(self, work_type, rate, valid_from, dept=None, employees=None, actor=None):
        return await tariffs.create_tariffs(
            self.session,
            actor or self.owner,
            self.settings,
            work_type=work_type,
            rate=Decimal(rate),
            currency="USD",
            department=dept,
            employees=employees,
            valid_from=valid_from,
        )


@pytest.fixture
async def settings(session):
    base = await get_settings(session)
    return dataclasses.replace(base, today_override=TODAY)


@pytest.fixture
async def world(session, settings):
    w = await World(session, settings).build()
    await session.commit()
    return w
