# bugalter

Telegram-бот внутреннего учёта: выполненные операции, сдельная оплата по индивидуальным тарифам
(ставка за единицу, ступенчатые шкалы, процент от суммы, фиксированная оплата, коэффициенты), склад с партиями
и себестоимостью (FIFO), товар на руках у сотрудников, премии/штрафы/удержания с подписью и лимитом удержаний,
авансы в долг, выплаты, расчётные периоды и отчёты с расшифровкой сумм.

* У каждого сотрудника своя **рабочая группа**: он пишет отчёт тегами (`#доставка 3 #фасовка05 20`),
  бот считает по ценам и присылает карточку, владелец подтверждает одной кнопкой. Суммы в группе не видны.
* В **личке с ботом** сотрудник видит свою работу, расчёт, свои товары и запрашивает выплату;
  владелец всё настраивает кнопками в Управлении. Незнакомым людям бот не отвечает, вход только по ссылке.

| Документ | Что внутри |
|---|---|
| [docs/DEPLOY.md](docs/DEPLOY.md) | пошаговый запуск на сервере: Docker, клонирование, `.env`, первичная настройка, обновление, копии, восстановление |
| [docs/DESIGN.md](docs/DESIGN.md) | бизнес-правила, формулы расчёта, настройки, защита от ошибок, открытые вопросы |
| [docs/COMMANDS.md](docs/COMMANDS.md) | все команды бота |

## Быстрый старт

```bash
git clone git@github.com:dalmatinec/bugalter.git && cd bugalter
cp .env.example .env && nano .env        # BOT_TOKEN, OWNER_TELEGRAM_IDS, POSTGRES_PASSWORD
docker compose up -d --build
docker compose logs -f bot               # ждём "bot_started"
```
Дальше — в личке с ботом `/start` → **⚙️ Управление**: валюта, сотрудники по должности (ссылка-приглашение), работы и цены, группы — всё кнопками. Подробно: [docs/DEPLOY.md, шаг 7](docs/DEPLOY.md#7-первичная-настройка-в-telegram).

Обновление: `sh scripts/update.sh`. Разовая копия базы: `docker compose run --rm backup once`.
Восстановление: `sh scripts/restore.sh backups/<файл>.dump`.

## Стек

Python 3.12 · aiogram 3 · PostgreSQL 16 · SQLAlchemy 2 (async) + Alembic · Docker Compose · pytest.
Деньги и количества — только `Decimal`/`NUMERIC`. Защита данных продублирована в БД триггерами.
Состояние мастеров ввода хранится в PostgreSQL — перезапуск бота не сбрасывает незаконченный ввод.

## Разработка

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
# PostgreSQL для тестов: postgresql+asyncpg://bugalter:bugalter@localhost:5432/bugalter_test (или TEST_DATABASE_URL)
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check bugalter migrations tests
```
Тесты работают с настоящим PostgreSQL (триггеры, блокировки, уникальные ключи, использование индексов);
сквозные сценарии гоняют настоящий диспетчер aiogram с подменённым Telegram.

Новая миграция: `alembic revision --autogenerate -m "..."`, затем проверить файл руками
(автогенерация не видит CHECK-ограничения и триггеры).

## Структура

```
bugalter/
  domain/     деньги (Decimal, округление), даты и периоды, права, разбор тегов, ошибки — без БД и Telegram
  db/         модели SQLAlchemy
  services/   бизнес-логика: access, org, tariffs, work, ledger, payroll, stock, reports, settings, audit
  bot/        aiogram: middleware (транзакция на действие, молчание для чужих), карточки, хранилище состояний
    handlers/
      common.py   /start, /help
      panel/      Управление: один модуль на раздел (employees, works, groups, settings, products, access, …)
      tags.py     отчёт о работе тегами в группе
      work.py     ввод и подтверждение работы (/op, кнопки карточки)
      me.py       личное меню сотрудника
      stock.py    склад
      payroll.py  премии, штрафы, выплаты, периоды
      reports.py  отчёты
migrations/   Alembic, включая триггеры защиты данных
tests/        pytest (сервисы, индексы, сквозные сценарии бота)
scripts/      backup.sh, update.sh, restore.sh
```
