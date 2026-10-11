# bugalter

Telegram-бот внутреннего учёта: выполненные операции, сдельная оплата по индивидуальным тарифам,
склад (включая товар на руках у сотрудников), премии/штрафы/удержания с подписью, выплаты,
расчётные периоды и отчёты с расшифровкой сумм.

* В **рабочей группе** подразделения вводятся операции и складские движения, руководитель подтверждает их кнопками.
  Суммы и зарплаты в группе не показываются.
* В **личке с ботом** сотрудник видит свою работу, расчёт, товар на руках и запрашивает выплату;
  руководители выписывают премии, регистрируют выплаты, закрывают периоды и строят отчёты.

Бизнес-правила, принятые решения и открытые вопросы — в [docs/DESIGN.md](docs/DESIGN.md).
Полный список команд — в [docs/COMMANDS.md](docs/COMMANDS.md).

## Стек

Python 3.12 · aiogram 3 · PostgreSQL 16 · SQLAlchemy 2 (async) + Alembic · Docker Compose · pytest.
Деньги и количества — только `Decimal`/`NUMERIC`. Защита данных продублирована в БД триггерами.

## Запуск (Docker Compose)

1. Создайте бота у @BotFather, получите токен.
   Для мастера ввода в группах ничего настраивать не нужно (он использует ответ на сообщение бота),
   но бота нужно добавить в рабочую группу.
2. `cp .env.example .env` и заполните `BOT_TOKEN`, `OWNER_TELEGRAM_IDS`, `POSTGRES_PASSWORD`
   (пароль — латиница и цифры: он подставляется в URL подключения).
3. `docker compose up -d --build`
   Миграции применяются автоматически при старте контейнера `bot`.
4. Логи: `docker compose logs -f bot` (JSON, по строке на событие).

## Первичная настройка (в личке с ботом, от имени владельца)

```
/dept_add north Север
/emp_add ivan Иван Иванов
/emp_dept ivan north
/emp_link ivan 123456789          ← Telegram ID сотрудника (он может узнать его командой /id)
/grant 987654321 operator north   ← оператор подразделения
/grant 555555555 manager north    ← руководитель подразделения
/wt_add доставка заказ per_unit Доставка
/tariff доставка 400 dept=north                ← ставка подразделения
/tariff доставка 450 dept=north emp=ivan       ← индивидуальная ставка
/product_add coffee кг Кофе зерно
/product_unit coffee уп 0.25
/set currency USD
/set timezone Europe/Moscow
```
Затем в рабочей группе: `/bind north`.

## Резервное копирование

Сервис `backup` делает `pg_dump` раз в `BACKUP_INTERVAL_SECONDS` в `./backups`, проверяет читаемость
архива и удаляет копии старше `BACKUP_KEEP_DAYS`. Разовая копия: `docker compose run --rm backup sh /backup.sh`.

Восстановление в новую базу (старую не трогаем, пока не проверили):
```
docker compose exec db createdb -U bugalter bugalter_restore
docker compose exec -T db pg_restore -U bugalter --no-owner -d bugalter_restore < backups/bugalter_YYYYMMDD_HHMMSS.dump
```
Проверьте данные, остановите бота, переименуйте базы (`ALTER DATABASE … RENAME TO …`) и запустите снова.
Храните копии ещё и вне сервера: каталог `./backups` лежит на том же диске, что и база.

## Разработка

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
# PostgreSQL для тестов: postgresql+asyncpg://bugalter:bugalter@localhost:5432/bugalter_test
#   (или задайте TEST_DATABASE_URL)
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check bugalter migrations tests
```
Тесты работают с настоящим PostgreSQL: они проверяют триггеры, блокировки и уникальные ключи,
сквозные сценарии гоняют настоящий диспетчер aiogram с подменённым Telegram.

Новая миграция: `alembic revision --autogenerate -m "..."`, затем проверить файл руками.

## Структура

```
bugalter/
  domain/     деньги (Decimal, округление), даты и периоды, права, ошибки — без БД и Telegram
  db/         модели SQLAlchemy
  services/   бизнес-логика: access, org, tariffs, work, ledger, payroll, stock, reports, settings, audit
  bot/        aiogram: middleware (транзакция на действие), handlers, карточки и клавиатуры
migrations/   Alembic, включая триггеры защиты данных
tests/        pytest (сервисы + сквозные сценарии бота)
scripts/      backup.sh
```
