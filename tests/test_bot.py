"""Сквозные сценарии через настоящий диспетчер aiogram. Telegram подменён: запросы записываются, а не уходят."""

import datetime as dt
import itertools
from decimal import Decimal

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import func, select

from bugalter.bot.app import bootstrap, build_dispatcher
from bugalter.bot.handlers import admin, common, me, work
from bugalter.bot.handlers import payroll as payroll_h
from bugalter.bot.handlers import reports as reports_h
from bugalter.bot.handlers import stock as stock_h
from bugalter.config import Config
from bugalter.db.models import Adjustment, Employee, Payout, StockDocument, WorkEntry, WorkType
from bugalter.services import ledger, stock

OWNER, IVAN, OPERATOR = 1, 10, 30
GROUP = -100777


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self._ids = itertools.count(5000)

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 — сигнатура aiogram
        self.calls.append(method)
        if isinstance(method, SendMessage | SendDocument):
            chat_type = "private" if int(method.chat_id) > 0 else "supergroup"
            return Message(
                message_id=next(self._ids),
                date=dt.datetime.now(dt.UTC),
                chat=Chat(id=int(method.chat_id), type=chat_type),
                text=getattr(method, "text", None),
            )
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def texts(self, chat_id=None) -> list[str]:
        result = []
        for call in self.calls:
            if isinstance(call, SendMessage | EditMessageText) and (chat_id is None or call.chat_id == chat_id):
                result.append(call.text)
        return result

    def alerts(self) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, AnswerCallbackQuery) and c.text]

    def last_markup(self):
        for call in reversed(self.calls):
            if getattr(call, "reply_markup", None) is not None and hasattr(call.reply_markup, "inline_keyboard"):
                return call.reply_markup
        return None


class Harness:
    def __init__(self, sessionmaker):
        self.sessionmaker = sessionmaker
        self.tg = FakeSession()
        self.bot = Bot("42:TEST", session=self.tg, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.config = Config(BOT_TOKEN="42:TEST", OWNER_TELEGRAM_IDS="1")
        # Роутеры — модульные объекты и подключаются к диспетчеру один раз; в тестах диспетчер свой на каждый тест.
        for router in (
            common.router,
            work.router,
            me.router,
            admin.router,
            stock_h.router,
            payroll_h.router,
            reports_h.router,
        ):
            router._parent_router = None
        self.dp = build_dispatcher(sessionmaker, self.config)
        self.ids = itertools.count(1)

    def _user(self, uid: int) -> User:
        names = {OWNER: "Владелец", IVAN: "Иван", OPERATOR: "Ольга"}
        return User(id=uid, is_bot=False, first_name=names.get(uid, f"user{uid}"), username=f"u{uid}")

    def _chat(self, chat_id: int) -> Chat:
        return Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup", title=None if chat_id > 0 else "Север")

    async def say(self, uid: int, text: str, chat_id: int | None = None):
        chat_id = chat_id or uid
        mark = len(self.tg.calls)
        msg = Message(
            message_id=next(self.ids),
            date=dt.datetime.now(dt.UTC),
            chat=self._chat(chat_id),
            from_user=self._user(uid),
            text=text,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self.ids), message=msg))
        return self.tg.calls[mark:]

    async def press(self, uid: int, data: str, chat_id: int | None = None):
        chat_id = chat_id or uid
        mark = len(self.tg.calls)
        msg = Message(message_id=4242, date=dt.datetime.now(dt.UTC), chat=self._chat(chat_id), text="card")
        cb = CallbackQuery(
            id=str(next(self.ids)), from_user=self._user(uid), chat_instance="ci", data=data, message=msg
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self.ids), callback_query=cb))
        return self.tg.calls[mark:]


def texts(calls) -> str:
    return "\n".join(getattr(c, "text", "") or "" for c in calls)


@pytest.fixture
async def h(sessionmaker, session):
    harness = Harness(sessionmaker)
    await bootstrap(sessionmaker, harness.config)
    yield harness
    everything = texts(harness.tg.calls) + "\n".join(harness.tg.alerts())
    assert "Внутренняя ошибка" not in everything, everything


async def setup_company(h: Harness):
    for cmd in (
        "/dept_add north Север",
        "/emp_add ivan Иван Иванов",
        "/emp_dept ivan north",
        "/wt_add доставка заказ per_unit Доставка",
        "/wt_add фасовка кг per_unit Фасовка",
        "/tariff доставка 400 dept=north",
        "/tariff фасовка 12.35 USD dept=north",
        "/product_add coffee кг Кофе зерно",
        "/product_unit coffee уп 0.25",
    ):
        out = texts(await h.say(OWNER, cmd))
        assert "⚠️" not in out and "❌" not in out, (cmd, out)
    await h.say(IVAN, "/start")
    assert "привязан" in texts(await h.say(OWNER, "/emp_link ivan 10"))
    await h.say(OPERATOR, "/start")
    out = texts(await h.say(OWNER, "/grant 30 operator north"))
    assert "Выдача" in out, out
    out = texts(await h.say(OWNER, "/bind north", GROUP))
    assert "привязана" in out, out


async def test_full_operation_flow(h, session):
    await setup_company(h)
    # Быстрый ввод в группе оператором
    out = await h.say(OPERATOR, "/op ivan доставка 3 #A-1052 срочно", GROUP)
    card = texts(out)
    assert "ОП-000001" in card and "ожидает подтверждения" in card
    assert "USD" not in card  # суммы в группе не показываются
    entry = (await session.execute(select(WorkEntry))).scalar_one()
    assert entry.order_ref == "A-1052" and entry.comment == "срочно" and entry.message_id is not None

    # Повторная доставка того же сообщения (Telegram переслал update) — дубля нет
    # (ключ строится из chat_id и message_id; эмулируем тем же message_id)
    msg = Message(
        message_id=999,
        date=dt.datetime.now(dt.UTC),
        chat=h._chat(GROUP),
        from_user=h._user(OPERATOR),
        text="/op ivan доставка 1",
    )
    for _ in range(2):
        await h.dp.feed_update(h.bot, Update(update_id=next(h.ids), message=msg))
    count = (await session.execute(select(func.count()).select_from(WorkEntry))).scalar_one()
    assert count == 2

    # Оператор не может подтвердить
    await h.press(OPERATOR, f"w:c:{entry.id}", GROUP)
    assert any("Недостаточно прав" in a for a in h.tg.alerts())

    # Владелец подтверждает дважды — начисление одно, Ивану уходит уведомление в личку
    out = await h.press(OWNER, f"w:c:{entry.id}", GROUP)
    assert any(isinstance(c, SendMessage) and c.chat_id == IVAN and "подтверждена" in c.text for c in out)
    await h.press(OWNER, f"w:c:{entry.id}", GROUP)
    assert (await ledger.balances(session, entry.employee_id)) == {"USD": Decimal("1200.0000")}

    # Иван смотрит расчёт в личке — суммы видны
    out = texts(await h.say(IVAN, "💰 Мой расчёт"))
    assert "1 200,00 USD" in out and "Доставка" in out

    # В группе личные данные не показываются
    out = texts(await h.say(IVAN, "/my_salary", GROUP))
    assert "только в личном чате" in out

    # Иван не может вводить операции
    out = texts(await h.say(IVAN, "/op ivan доставка 1", GROUP))
    assert "Недостаточно прав" in out


async def test_wizard_flow(h, session):
    await setup_company(h)
    emp = (await session.execute(select(Employee).where(Employee.code == "ivan"))).scalar_one()
    wt = (await session.execute(select(WorkType).where(WorkType.code == "фасовка"))).scalar_one()
    out = await h.say(OPERATOR, "/op", GROUP)
    assert "Кто выполнил работу" in texts(out)
    # чужой пользователь не может нажимать кнопки мастера
    await h.press(OWNER, f"opw:e:{emp.id}", GROUP)
    assert any("другой пользователь" in a for a in h.tg.alerts())
    await h.press(OPERATOR, f"opw:e:{emp.id}", GROUP)
    await h.press(OPERATOR, f"opw:t:{wt.id}", GROUP)
    out = texts(await h.say(OPERATOR, "1,255 #Z-7", GROUP))
    assert "черновик" in out
    entry = (await session.execute(select(WorkEntry))).scalar_one()
    assert entry.status == "draft" and entry.amount == Decimal("15.5000")
    await h.press(OPERATOR, f"w:d:{entry.id}:1", GROUP)  # вчерашним числом
    await h.press(OPERATOR, f"w:s:{entry.id}", GROUP)
    await session.refresh(entry)
    assert entry.status == "pending"
    # слишком точное количество
    await h.say(OPERATOR, "/op", GROUP)
    await h.press(OPERATOR, f"opw:e:{emp.id}", GROUP)
    await h.press(OPERATOR, f"opw:t:{wt.id}", GROUP)
    assert "Слишком точное" in texts(await h.say(OPERATOR, "1.2555", GROUP))


async def test_stock_flow_with_receipt_confirmation(h, session):
    await setup_company(h)
    assert "⚠️" not in texts(await h.say(OPERATOR, "/receive coffee 100", GROUP))
    out = await h.say(OPERATOR, "/issue ivan coffee 40 уп", GROUP)
    assert any(isinstance(c, SendMessage) and c.chat_id == IVAN and "Подтвердите получение" in c.text for c in out)
    doc = (await session.execute(select(StockDocument).where(StockDocument.doc_type == "issue"))).scalar_one()
    assert doc.status == "in_transit" and doc.message_id is not None
    out = await h.press(IVAN, f"sd:a:{doc.id}")
    # операторов уведомили, карточка в группе обновлена
    assert any(isinstance(c, SendMessage) and c.chat_id == OPERATOR and "получен" in c.text for c in out)
    assert any(isinstance(c, EditMessageText) and c.chat_id == GROUP for c in out)
    loc = await stock.location(session, "employee", doc.department_id, doc.target_employee_id)
    assert await stock.balance(session, loc.id, (await stock.get_product(session, "coffee")).id) == Decimal("10")
    out = texts(await h.say(OPERATOR, "/issue ivan coffee 1000", GROUP))
    assert "Недостаточно" in out
    out = texts(await h.say(IVAN, "📦 Товар на руках"))
    assert "Кофе зерно: 10 кг" in out


async def test_money_flow_private_only(h, session):
    await setup_company(h)
    await h.say(OPERATOR, "/op ivan доставка 5", GROUP)
    entry = (await session.execute(select(WorkEntry))).scalar_one()
    await h.press(OWNER, f"w:c:{entry.id}", GROUP)

    assert "только в личном чате" in texts(await h.say(OWNER, "/bonus ivan 500 план", GROUP))
    out = texts(await h.say(OWNER, "/bonus ivan 500 выполнение плана"))
    assert "ПР-00001" in out
    adj = (await session.execute(select(Adjustment))).scalar_one()
    await h.press(OWNER, f"adj:s:{adj.id}")
    await session.refresh(adj)
    assert adj.status == "approved"

    out = texts(await h.say(OWNER, "/payout ivan 9999 аванс"))
    assert "Доступно к выплате" in out
    out = texts(await h.say(OWNER, "/payout ivan 1000 аванс наличными"))
    assert "ожидает подтверждения" in out
    payout = (await session.execute(select(Payout))).scalar_one()
    out = await h.press(OWNER, f"po:c:{payout.id}")
    assert any(isinstance(c, SendMessage) and c.chat_id == IVAN and "выплата" in c.text for c in out)
    assert (await ledger.balances(session, entry.employee_id)) == {"USD": Decimal("1500.0000")}

    # запрос выплаты от сотрудника
    out = texts(await h.say(IVAN, "/request_payout 300 на карту"))
    assert "ЗВ-00001" in out
    out = texts(await h.say(OWNER, "/requests"))
    assert "ЗВ-00001" in out

    out = await h.say(OWNER, "/report emp ivan xlsx")
    assert any(isinstance(c, SendDocument) for c in out)
    out = texts(await h.say(OWNER, "/report dept @north"))
    assert "Иван Иванов" in out
    out = texts(await h.say(OWNER, "/balance ivan"))
    assert "Остаток" in out
    out = texts(await h.say(IVAN, "/balance ivan"))
    assert "Остаток" in out  # себя смотреть можно
