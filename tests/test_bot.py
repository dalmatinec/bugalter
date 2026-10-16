"""Сквозные сценарии через настоящий диспетчер aiogram. Telegram подменён: запросы записываются, а не уходят."""

import datetime as dt
import itertools
from decimal import Decimal

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import func, select

from bugalter.bot.app import ROUTERS, bootstrap, build_dispatcher
from bugalter.config import Config
from bugalter.db.models import Adjustment, Department, Employee, Payout, StockDocument, Tariff, WorkEntry, WorkType
from bugalter.services import access, ledger, org, payroll, stock, tariffs, work
from bugalter.services.settings import get_settings

OWNER, IVAN, OPERATOR, ACCOUNTANT = 1, 10, 30, 40
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
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="Бухгалтер", username="bugalter_test_bot")
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
        for router in ROUTERS:
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


async def setup_company(h: Harness, session):
    """Готовая компания для сквозных тестов.

    Заводится напрямую через сервисный слой, а не текстовыми командами — так, как это теперь
    реально происходит через кнопочную панель: сотрудник, работы, тарифы, товар, группа.
    """
    owner = await access.load_actor(session, OWNER, None, "Владелец")
    dept = (await session.execute(select(Department))).scalar_one()  # создаётся сама при старте бота

    emp = await org.create_employee(session, owner, "ivan", "Иван Иванов")
    await org.attach_to_department(session, owner, emp, dept)

    delivery = await tariffs.create_work_type(session, owner, "доставка", "заказ", "per_unit", "Доставка")
    packing = await tariffs.create_work_type(session, owner, "фасовка", "кг", "per_unit", "Фасовка")
    settings = await get_settings(session)
    await work.add_tariffs(
        session,
        owner,
        settings,
        work_type=delivery,
        rate=Decimal("400"),
        currency="USD",
        department=dept,
        employees=None,
        valid_from=None,
    )
    await work.add_tariffs(
        session,
        owner,
        settings,
        work_type=packing,
        rate=Decimal("12.35"),
        currency="USD",
        department=dept,
        employees=None,
        valid_from=None,
    )

    coffee = await stock.create_product(session, owner, "coffee", "кг", "Кофе зерно")
    await stock.add_product_unit(session, owner, coffee, "уп", Decimal("0.25"))
    await session.commit()

    await h.say(IVAN, "/start")
    ivan_user = await access.find_user(session, str(IVAN))
    await org.link_user(session, owner, emp, ivan_user)

    await h.say(OPERATOR, "/start")
    operator_user = await access.find_user(session, str(OPERATOR))
    await access.grant_role(session, owner, operator_user, "operator", dept)

    await org.bind_chat(session, owner, GROUP, "Север", dept)
    await session.commit()
    return dept, emp


async def test_full_operation_flow(h, session):
    await setup_company(h, session)
    # Быстрый ввод в группе оператором
    out = await h.say(OPERATOR, "/op ivan доставка 3 #A-1052 срочно", GROUP)
    card = texts(out)
    assert "ОП-000001" in card and "ожидает подтверждения" in card
    assert "$" not in card  # суммы в группе не показываются
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

    # Сотрудник не может подтвердить
    await h.press(IVAN, f"w:c:{entry.id}", GROUP)
    assert any("Недостаточно прав" in a for a in h.tg.alerts())

    # Владелец подтверждает дважды — начисление одно, Ивану уходит уведомление в личку
    out = await h.press(OWNER, f"w:c:{entry.id}", GROUP)
    assert any(isinstance(c, SendMessage) and c.chat_id == IVAN and "подтверждена" in c.text for c in out)
    await h.press(OWNER, f"w:c:{entry.id}", GROUP)
    assert (await ledger.balances(session, entry.employee_id)) == {"USD": Decimal("1200.0000")}

    # Иван смотрит расчёт в личке — суммы видны
    out = texts(await h.say(IVAN, "💰 Мой расчёт"))
    assert "$1 200,00" in out and "Доставка" in out

    # В группе личные данные не показываются
    out = texts(await h.say(IVAN, "/my_salary", GROUP))
    assert "только в личном чате" in out

    # Иван не может вводить операции
    out = texts(await h.say(IVAN, "/op ivan доставка 1", GROUP))
    assert "Недостаточно прав" in out


async def test_wizard_flow(h, session):
    await setup_company(h, session)
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
    await setup_company(h, session)
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
    out = texts(await h.say(IVAN, "📦 Мои товары"))
    assert "Кофе зерно: 10 кг" in out


async def test_money_flow_private_only(h, session):
    await setup_company(h, session)
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

    out = texts(await h.say(OWNER, "/payout ivan 9999 зп"))
    assert "Зарплатой можно выплатить" in out  # зарплатой больше заработанного нельзя, авансом можно
    out = texts(await h.say(OWNER, "/payout ivan 1000 аванс наличными"))
    assert "ожидает подтверждения" in out
    payout = (await session.execute(select(Payout))).scalar_one()
    out = await h.press(OWNER, f"po:c:{payout.id}")
    assert any(isinstance(c, SendMessage) and c.chat_id == IVAN and "Аванс $1\u202f000,00 выдан" in c.text for c in out)
    assert (await ledger.balances(session, entry.employee_id)) == {"USD": Decimal("1500.0000")}

    # запрос выплаты от сотрудника
    out = texts(await h.say(IVAN, "/request_payout 300 на карту"))
    assert "ЗВ-00001" in out
    out = texts(await h.say(OWNER, "/requests"))
    assert "ЗВ-00001" in out

    out = await h.say(OWNER, "/report emp ivan xlsx")
    assert any(isinstance(c, SendDocument) for c in out)
    out = texts(await h.say(OWNER, "/report dept"))
    assert "Иван Иванов" in out
    out = texts(await h.say(OWNER, "/balance ivan"))
    assert "Остаток" in out
    out = texts(await h.say(IVAN, "/balance ivan"))
    assert "Остаток" in out  # себя смотреть можно


async def test_wizard_survives_restart(h, session, sessionmaker):
    """Состояние мастера хранится в PostgreSQL: новый экземпляр бота продолжает начатый ввод."""
    await setup_company(h, session)
    emp = (await session.execute(select(Employee).where(Employee.code == "ivan"))).scalar_one()
    wt = (await session.execute(select(WorkType).where(WorkType.code == "доставка"))).scalar_one()
    await h.say(OPERATOR, "/op", GROUP)
    await h.press(OPERATOR, f"opw:e:{emp.id}", GROUP)
    await h.press(OPERATOR, f"opw:t:{wt.id}", GROUP)
    restarted = Harness(sessionmaker)  # «перезапуск»: новый диспетчер и новое хранилище
    out = texts(await restarted.say(OPERATOR, "2 #R-1", GROUP))
    assert "черновик" in out, out
    entry = (await session.execute(select(WorkEntry))).scalar_one()
    assert entry.order_ref == "R-1" and entry.quantity == Decimal(2)


async def test_new_pricing_and_stock_commands(h, session):
    dept, emp = await setup_company(h, session)
    owner = await access.load_actor(session, OWNER, None, "Владелец")
    settings = await get_settings(session)
    delivery = (await session.execute(select(WorkType).where(WorkType.code == "доставка"))).scalar_one()

    await tariffs.create_coefficient(session, owner, "срочно", Decimal("1.5"), "Срочный заказ")
    await work.add_tariffs(
        session,
        owner,
        settings,
        work_type=delivery,
        rate=Decimal("400"),
        currency="USD",
        department=dept,
        employees=None,
        valid_from=None,
        tiers=[(Decimal(10), Decimal(450))],
    )
    await session.commit()

    tariff_row = (
        (await session.execute(select(Tariff).where(Tariff.work_type_id == delivery.id).order_by(Tariff.id.desc())))
        .scalars()
        .first()
    )
    assert await tariffs.tiers_of(session, tariff_row.id) == [(Decimal(10), Decimal(450))]

    out = texts(await h.say(OPERATOR, "/op ivan доставка 12 *срочно #C-1", GROUP))
    assert "срочно" in out
    entry = (await session.execute(select(WorkEntry))).scalar_one()
    assert entry.coefficient == Decimal("1.5")
    await h.press(OWNER, f"w:c:{entry.id}", GROUP)
    await session.refresh(entry)
    assert entry.amount == Decimal((10 * 400 + 2 * 450) * 1.5)
    out = texts(await h.say(IVAN, "/my_salary"))
    assert "коэф. 1,5" in out and "ступень" in out

    out = texts(await h.say(OPERATOR, "/receive coffee 40 уп cost=1.5", GROUP))
    assert "⚠️" not in out, out
    out = texts(await h.say(OWNER, "/batches coffee"))
    assert "$6/кг" in out and "осталось 10 кг" in out
    out = texts(await h.say(OWNER, "/stock"))
    assert "$60,00" in out
    # в группе себестоимость не видна
    assert "$" not in texts(await h.say(OPERATOR, "/stock", GROUP))

    await h.say(OPERATOR, "/issue ivan coffee 4", GROUP)
    doc = (await session.execute(select(StockDocument).where(StockDocument.doc_type == "issue"))).scalar_one()
    await h.press(IVAN, f"sd:a:{doc.id}")
    operator_user = await access.find_user(session, str(OPERATOR))
    await access.grant_role(session, owner, operator_user, "storekeeper", dept)
    await session.commit()
    out = texts(await h.say(OPERATOR, "/inv ivan coffee 3", GROUP))
    assert "Недостача" in out
    out = texts(await h.say(OWNER, "/case 1 employee cost"))
    assert "себестоимость" in out and "6,00" in out
    out = await h.say(OWNER, "/report stock xlsx")
    assert any(isinstance(c, SendDocument) for c in out)

    # аванс больше заработанного
    out = texts(await h.say(OWNER, f"/payout ivan {int(entry.amount) + 500} аванс"))
    assert "ожидает подтверждения" in out, out


async def test_admin_panel_simple_setup(h, session):
    """Настройка с нуля в простом режиме: одна точка создаётся сама, вопросов про подразделения нет."""
    import re

    from aiogram.types import ChatMemberLeft, ChatMemberMember, ChatMemberUpdated

    from bugalter.db.models import AccessGrant, Department, Tariff, TariffTier, TelegramChat

    out = texts(await h.say(OWNER, "/start"))
    assert "Управление" in out
    # Первый вход в Управление: сначала валюта
    out = texts(await h.say(OWNER, "⚙️ Управление"))
    assert "в какой валюте" in out
    out = await h.press(OWNER, "p:first:USD")
    markup = next(c.reply_markup for c in out if getattr(c, "reply_markup", None))
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["👥 Сотрудники", "💰 Работы и цены", "💸 Выплаты", "📦 Склад", "💬 Группы", "➕ Ещё"]
    out = await h.say(OWNER, "⚙️ Управление")
    assert [b.text for row in out[-1].reply_markup.inline_keyboard for b in row] == labels
    dept = (await session.execute(select(Department))).scalar_one()  # создана автоматически

    # Пока человек не подключён по ссылке, бот ему не отвечает
    assert await h.say(IVAN, "/start") == []

    # Сотрудник: должность → что может → ссылка
    out = texts(await h.press(OWNER, "p:e:add:0"))
    assert "Какая должность?" in out
    out = texts(await h.say(OWNER, "Курьер"))
    assert "Добавлен: <b>Курьер</b>" in out and "подразделен" not in out.lower()
    emp = (await session.execute(select(Employee))).scalar_one()
    assert emp.full_name == "Курьер"
    out = texts(await h.press(OWNER, f"p:e:who:{emp.id}:operator"))
    link = re.search(r"https://t.me/bugalter_test_bot\?start=(inv_[\w-]+)", out)
    assert link, out
    out = await h.say(IVAN, f"/start {link.group(1)}")
    assert "Вы подключены: <b>Курьер</b>" in texts(out) and "Оператор" in texts(out)
    menu = next(c.reply_markup for c in out if getattr(c, "chat_id", None) == IVAN)
    buttons = [b.text for row in menu.keyboard for b in row]
    assert "📊 Моя работа" in buttons and "📦 Мои товары" not in buttons  # товар ему ещё не выдавали
    grant = (await session.execute(select(AccessGrant).where(AccessGrant.role_code == "operator"))).scalar_one()
    assert grant.department_id == dept.id
    assert "недействительна" in texts(await h.say(IVAN, f"/start {link.group(1)}"))
    # Ник и ID видит только владелец
    out = texts(await h.press(OWNER, f"p:e:v:{emp.id}"))
    assert f"ID <code>{IVAN}</code>" in out

    # Второй курьер получает различимую подпись
    await h.press(OWNER, "p:e:add:0")
    assert "Добавлен: <b>Курьер 2</b>" in texts(await h.say(OWNER, "курьер"))

    # Работа: название → как платим → за что → цена
    await h.press(OWNER, "p:w:add")
    await h.say(OWNER, "Доставка")
    await h.press(OWNER, "p:w:m:per_unit")
    out = texts(await h.press(OWNER, "p:w:u:заказ"))
    assert "Сколько платим за 1 заказ" in out
    out = texts(await h.say(OWNER, "400 10:450"))
    assert "Цена сохранена" in out and "$400" in out and "сверх 10: $450" in out
    tariff = (await session.execute(select(Tariff))).scalar_one()
    assert tariff.currency == "USD" and tariff.employee_id is None
    assert (await session.execute(select(TariffTier))).scalar_one().threshold == Decimal(10)
    out = texts(await h.press(OWNER, "p:w"))
    assert "Доставка</b> #доставка: $400 за заказ" in out

    # Фиксированная оплата: без вопроса про единицы
    await h.press(OWNER, "p:w:add")
    await h.say(OWNER, "Погрузка")
    out = texts(await h.press(OWNER, "p:w:m:fixed"))
    assert "за один раз" in out
    await h.say(OWNER, "700")

    # «Ещё»: доплаты и настройки
    await h.press(OWNER, "p:more")
    await h.press(OWNER, "p:c:add")
    assert "*срочно" in texts(await h.say(OWNER, "срочно 1.5"))
    out = texts(await h.press(OWNER, "p:s:v:deduction_limit_percent:20"))
    assert "20 % от начислений" in out

    # Бота добавили в группу — один вопрос: чья группа
    bot_user = User(id=42, is_bot=True, first_name="bot")
    event = ChatMemberUpdated(
        chat=h._chat(GROUP),
        from_user=h._user(OWNER),
        date=dt.datetime.now(dt.UTC),
        old_chat_member=ChatMemberLeft(user=bot_user),
        new_chat_member=ChatMemberMember(user=bot_user),
    )
    mark = len(h.tg.calls)
    await h.dp.feed_update(h.bot, Update(update_id=next(h.ids), my_chat_member=event))
    assert "Чья это группа?" in texts(h.tg.calls[mark:])
    out = texts(await h.press(OWNER, f"g:e:{dept.id}:{emp.id}", GROUP))
    assert "Это группа: <b>Курьер</b>" in out and "администратором" in out
    assert (await session.get(TelegramChat, GROUP)).employee_id == emp.id

    # Оператор Иван вносит работу в своей группе без кода сотрудника
    out = texts(await h.say(IVAN, "/op доставка 3 #Z-1", GROUP))
    assert "Курьер" in out and "ожидает подтверждения" in out

    # Товар — в «Ещё»
    await h.press(OWNER, "p:pr:add")
    await h.say(OWNER, "Кофе зерно")
    out = texts(await h.press(OWNER, "p:pr:u:кг"))
    assert "Кофе зерно" in out


async def test_tag_report_in_group(h, session):
    """Сотрудник пишет отчёт тегами, владелец подтверждает одной кнопкой."""
    await setup_company(h, session)
    # Чужие хэштеги — не отчёт, бот молчит
    assert texts(await h.say(IVAN, "#привет всем", GROUP)) == ""

    out = await h.say(IVAN, "#доставка 3 #фасовка 2,5 #непонятно", GROUP)
    card = next(c for c in out if isinstance(c, SendMessage) and c.chat_id == GROUP)
    assert "Отчёт: Иван Иванов" in card.text and "Доставка × 3 заказ" in card.text
    assert "#непонятно" in card.text and "$" not in card.text  # суммы в группе скрыты
    entries = (await session.execute(select(WorkEntry).order_by(WorkEntry.id))).scalars().all()
    assert [(e.status, e.quantity) for e in entries] == [("pending", Decimal(3)), ("pending", Decimal("2.5"))]
    confirm = card.reply_markup.inline_keyboard[0][0].callback_data
    assert confirm.startswith(f"tb:c:{GROUP}:")

    # Сотрудник сам себе не подтверждает
    await h.press(IVAN, confirm, GROUP)
    assert any("Недостаточно прав" in a for a in h.tg.alerts())
    out = await h.press(OWNER, confirm, GROUP)
    assert any(isinstance(c, SendMessage) and c.chat_id == IVAN and "Отчёт подтверждён" in c.text for c in out)
    assert await ledger.balances(session, entries[0].employee_id) == {"USD": Decimal("1230.8800")}

    # Без цены отчёт не принимается целиком
    owner = await access.load_actor(session, OWNER)
    await tariffs.create_work_type(session, owner, "погрузка", "шт", "per_unit", "Погрузка")
    await session.commit()
    out = texts(await h.say(IVAN, "#погрузка 2", GROUP))
    assert "Нет цены на Погрузка" in out


async def test_payouts_panel_advance_and_salary(h, session):
    """Аванс кнопками любой суммой, в день зарплаты вычесть его или оставить на потом."""
    _, emp = await setup_company(h, session)
    owner = await access.load_actor(session, OWNER)
    delivery = (await session.execute(select(WorkType).where(WorkType.code == "доставка"))).scalar_one()
    for n in (1, 2):  # заработал 800
        res = await work.create_entry(
            session,
            owner,
            await get_settings(session),
            idempotency_key=f"p{n}",
            department_id=(await session.execute(select(Department))).scalar_one().id,
            employee_id=emp.id,
            work_type_id=delivery.id,
            quantity=Decimal(1),
        )
        await work.confirm_entry(session, owner, await get_settings(session), res.entry.id)
    await session.commit()

    out = texts(await h.press(OWNER, "p:pay"))
    assert "Иван Иванов</b>: заработано $800,00" in out
    await h.press(OWNER, f"p:pay:adv:{emp.id}")
    out = await h.say(OWNER, "1000")
    assert "✅ Выдано. Аванс $1\u202f000,00" in texts(out)
    assert any(isinstance(c, SendMessage) and c.chat_id == IVAN and "выдан аванс" in c.text for c in out)
    out = texts(await h.press(OWNER, f"p:pay:v:{emp.id}"))
    assert (
        "Аванс, ещё не вычтен: $1\u202f000,00" in out
        and "С вычетом аванса: к выплате $0,00" in out
        and "Без вычета: к выплате $800,00" in out
    )

    # Без вычета: выплачиваем заработанное, аванс остаётся
    out = texts(await h.press(OWNER, f"p:pay:s:{emp.id}:0"))
    assert "Точно выплатить $800,00" in out
    nonce_kb = next(
        c.reply_markup for c in await h.press(OWNER, f"p:pay:s:{emp.id}:0") if isinstance(c, EditMessageText)
    )
    ok = nonce_kb.inline_keyboard[0][0].callback_data
    out = texts(await h.press(OWNER, ok))
    assert "Зарплата $800,00" in out
    assert await payroll.outstanding_advance(session, emp.id, "USD") == Decimal(1000)
    # Повторное нажатие той же кнопки ничего не выплачивает
    await h.press(OWNER, ok)
    assert any("нечего" in a for a in h.tg.alerts())


async def test_backups_screen(h, session, tmp_path):
    from bugalter.bot.handlers.panel import backups

    await setup_company(h, session)
    h.config.backup_dir = str(tmp_path)
    (tmp_path / "bk_1510_0800.dump").write_bytes(b"dump")
    out = texts(await h.press(OWNER, "p:bk"))
    assert "bk_1510_0800.dump" in out and "3 раза в сутки" in out
    out = await h.press(OWNER, "p:bk:get")
    assert any(isinstance(c, SendDocument) and c.chat_id == OWNER for c in out)
    await h.press(OWNER, "p:bk:now")
    assert (tmp_path / ".now").exists()  # заявку забирает контейнер backup
    for task in list(backups._waiting):
        task.cancel()
    await h.press(IVAN, "p:bk")
    assert any("только владельцу" in a for a in h.tg.alerts())


async def test_advance_request_owner_approves(h, session):
    """Сотрудник просит аванс, владелец одобряет кнопкой, бот сразу записывает аванс."""
    dept, emp = await setup_company(h, session)
    await h.say(IVAN, "💸 Попросить аванс")
    out = await h.say(IVAN, "300000 на ремонт")
    assert "Просьба отправлена" in texts(out)
    ask = next(c for c in out if isinstance(c, SendMessage) and c.chat_id == OWNER)
    assert "просит аванс" in ask.text
    # Оператор без права на выплаты просьбу не получает
    assert not any(isinstance(c, SendMessage) and c.chat_id == OPERATOR for c in out)
    approve = ask.reply_markup.inline_keyboard[0][0].callback_data

    out = await h.press(OWNER, approve)
    note = next(c for c in out if isinstance(c, SendMessage) and c.chat_id == IVAN)
    assert "Аванс $300 000,00 выдан" in note.text and "вычтется из зарплаты" in note.text
    assert await payroll.outstanding_advance(session, emp.id, "USD") == Decimal(300000)
    # Повторное нажатие ничего не записывает и не шлёт
    out = await h.press(OWNER, approve)
    assert not any(isinstance(c, SendMessage) for c in out)
    assert (await session.execute(select(func.count()).select_from(Payout))).scalar_one() == 1


async def test_advance_request_accountant_then_owner(h, session):
    """Бухгалтер одобрил, но выплаты подтверждает владелец: владельцу приходит кнопка подтверждения."""
    dept, emp = await setup_company(h, session)
    owner = await access.load_actor(session, OWNER)
    await h.say(ACCOUNTANT, "/start")
    acc_user = await access.find_user(session, str(ACCOUNTANT))
    await access.grant_role(session, owner, acc_user, "accountant", dept)
    await session.commit()

    out = await h.say(ACCOUNTANT, "⚙️ Управление")
    assert "Выплаты" in texts(out) and "Сотрудники" not in texts(out)

    await h.say(IVAN, "💸 Попросить аванс")
    out = await h.say(IVAN, "500")
    ask = next(c for c in out if isinstance(c, SendMessage) and c.chat_id == OWNER)
    out = await h.press(ACCOUNTANT, ask.reply_markup.inline_keyboard[0][0].callback_data)
    to_owner = next(c for c in out if isinstance(c, SendMessage) and c.chat_id == OWNER)
    assert "ждёт вашего подтверждения" in to_owner.text
    to_ivan = next(c for c in out if isinstance(c, SendMessage) and c.chat_id == IVAN)
    assert "ждёт подтверждения владельца" in to_ivan.text
    assert await payroll.outstanding_advance(session, emp.id, "USD") == 0

    out = await h.press(OWNER, to_owner.reply_markup.inline_keyboard[0][0].callback_data)
    assert any(isinstance(c, SendMessage) and c.chat_id == IVAN and "Аванс $500,00 выдан" in c.text for c in out)
    assert await payroll.outstanding_advance(session, emp.id, "USD") == Decimal(500)

    # Отказ: сотруднику приходит понятное сообщение
    await h.say(IVAN, "💸 Попросить аванс")
    out = await h.say(IVAN, "100")
    ask = next(c for c in out if isinstance(c, SendMessage) and c.chat_id == OWNER)
    out = await h.press(OWNER, ask.reply_markup.inline_keyboard[0][1].callback_data)
    assert any(isinstance(c, SendMessage) and c.chat_id == IVAN and "отклонена" in c.text for c in out)


async def test_stock_panel_buttons(h, session):
    """Склад кнопками: приход, выдача сотруднику, подтверждение, возврат, остатки по людям."""
    dept, emp = await setup_company(h, session)
    coffee = await stock.get_product(session, "coffee")
    out = texts(await h.press(OWNER, "p:st"))
    assert "Движений пока нет" in out

    await h.press(OWNER, f"p:st:ip:{dept.id}:{coffee.id}")
    out = texts(await h.say(OWNER, "50 5000"))
    assert "Приход записан" in out and "на складе: <b>50 кг</b>" in out

    await h.press(OWNER, f"p:st:op:{dept.id}:{emp.id}:{coffee.id}")
    out = await h.say(OWNER, "8 уп")  # 8 упаковок по 0,25 кг
    assert any(isinstance(c, SendMessage) and c.chat_id == IVAN and "Подтвердите получение" in c.text for c in out)
    assert "ждёт подтверждения: 2 кг" in texts(out)
    doc = (await session.execute(select(StockDocument).where(StockDocument.doc_type == "issue"))).scalar_one()
    await h.press(IVAN, f"sd:a:{doc.id}")

    out = texts(await h.press(OWNER, f"p:st:d:{dept.id}"))
    assert "пришло 50 · выдано 2 · вернули 0 кг" in out
    assert "на складе: <b>48 кг</b>" in out and "Иван Иванов: 2 кг" in out

    await h.press(OWNER, f"p:st:rp:{dept.id}:{emp.id}:{coffee.id}")
    out = texts(await h.say(OWNER, "0.5"))
    assert "Возврат записан" in out and "Иван Иванов: 1,5 кг" in out and "на складе: <b>48,5 кг</b>" in out
    out = texts(await h.press(OWNER, f"p:st:log:{dept.id}"))
    assert "Выдача сотруднику · Иван Иванов" in out

    # Работнику без прав склад не открывается
    await h.press(IVAN, "p:st")
    assert any("Склад доступен" in a for a in h.tg.alerts())


async def test_product_pack_excludes_base_unit(h, session):
    await setup_company(h, session)
    coffee = await stock.get_product(session, "coffee")
    out = await h.press(OWNER, f"p:pr:pk:{coffee.id}")
    markup = next(c.reply_markup for c in out if isinstance(c, EditMessageText))
    codes = [b.text for row in markup.inline_keyboard for b in row]
    assert "кг" not in codes and "уп" not in codes and "кор" in codes
