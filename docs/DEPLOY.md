# Развёртывание на сервере: пошагово

Инструкция для чистого сервера с Ubuntu 22.04 или 24.04. Команды выполняются в терминале сервера по SSH.
Домен, сертификаты и открытые порты не нужны: бот сам опрашивает Telegram (long polling).

## 0. Что понадобится

| Что | Минимум |
|---|---|
| Сервер (VPS) | 1 vCPU, 1–2 ГБ RAM, 15 ГБ диска, Ubuntu 22.04/24.04 |
| Доступ | SSH к серверу, доступ к репозиторию `dalmatinec/bugalter` на GitHub |
| Telegram | аккаунт владельца, рабочая группа подразделения |

Сервер должен видеть `api.telegram.org`. Если хостинг его не видит, понадобится прокси (шаг 5, `TELEGRAM_PROXY`).

## 1. Создать бота в Telegram

1. Напишите [@BotFather](https://t.me/BotFather) → `/newbot` → имя и username бота.
2. Сохраните токен вида `1234567890:AA...` — это `BOT_TOKEN`. Никому его не пересылайте.
3. `/setjoingroups` → выберите бота → **Enable** (чтобы бота можно было добавить в группу).
4. Узнайте свой Telegram ID: напишите [@userinfobot](https://t.me/userinfobot) — число `Id` это `OWNER_TELEGRAM_IDS`.

Privacy mode менять не нужно: команды и ответы на сообщения бота он видит и в приватном режиме.

## 2. Подготовить сервер

```bash
ssh root@IP_СЕРВЕРА

apt update && apt -y upgrade
apt -y install ca-certificates curl git ufw unattended-upgrades

# Отдельный пользователь вместо root
adduser bugalter
usermod -aG sudo bugalter

# Фаервол: снаружи нужен только SSH
ufw allow OpenSSH
ufw --force enable
```

Рекомендуется вход только по SSH-ключу: скопируйте свой ключ (`ssh-copy-id bugalter@IP_СЕРВЕРА` со своего
компьютера), затем в `/etc/ssh/sshd_config` поставьте `PasswordAuthentication no` и `systemctl restart ssh`.

Дальше работаем под пользователем `bugalter`: `su - bugalter`.

## 3. Установить Docker

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
exit            # выйти и зайти снова (su - bugalter), чтобы группа docker применилась
docker run --rm hello-world
```

## 4. Склонировать репозиторий

**Если репозиторий приватный** — через deploy key (ключ только на чтение, только для этого репозитория):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/bugalter_deploy -N "" -C "bugalter-server"
cat ~/.ssh/bugalter_deploy.pub
```
Скопируйте выведенную строку в GitHub: репозиторий → **Settings → Deploy keys → Add deploy key**
(галочку «Allow write access» не ставить). Затем:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/bugalter_deploy
    IdentitiesOnly yes
EOF
git clone git@github.com:dalmatinec/bugalter.git ~/bugalter
```

**Если репозиторий публичный:** `git clone https://github.com/dalmatinec/bugalter.git ~/bugalter`

```bash
cd ~/bugalter
git log --oneline -3     # видно последние коммиты — значит, склонировалось
```

## 5. Настроить `.env`

```bash
cp .env.example .env
openssl rand -hex 24     # сгенерировать пароль базы, скопировать
nano .env
chmod 600 .env           # файл с секретами читает только владелец
```

| Переменная | Что указать |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `OWNER_TELEGRAM_IDS` | ваш Telegram ID; несколько — через запятую: `111,222` |
| `POSTGRES_PASSWORD` | сгенерированный пароль (только латиница и цифры) |
| `BACKUP_KEEP_DAYS` | сколько дней хранить копии (по умолчанию 14) |
| `BACKUP_INTERVAL_SECONDS` | как часто делать копию (86400 = раз в сутки) |
| `TELEGRAM_PROXY` | пусто; заполнить, только если сервер не видит api.telegram.org: `http://user:pass@host:port` |

## 6. Запустить

```bash
docker compose up -d --build
docker compose ps                 # db — healthy, bot и backup — running
docker compose logs -f bot        # ждём строку "bot_started", выход — Ctrl+C
```

При каждом старте контейнер `bot` сам применяет миграции базы (`alembic upgrade head`) и выдаёт роль
владельца ID из `OWNER_TELEGRAM_IDS`. Контейнеры запускаются автоматически после перезагрузки сервера.

## 7. Первичная настройка в Telegram

Напишите боту `/start` и нажмите **⚙️ Управление**. В меню четыре кнопки:

0. При первом входе в Управление бот спросит **валюту** (₸, $ и т.д.): все цены и выплаты будут в ней.
1. **👥 Сотрудники** → «Добавить» → должность (например, Курьер) → что он может (работник / оператор / руководитель) → ссылка. Пока человек не открыл ссылку, бот ему не отвечает.
   Бот даст ссылку — перешлите её человеку, он откроет её и сразу подключится с нужной ролью.
2. **💰 Работы и цены** → «Добавить работу» → название → как платим (за штуку/заказ, фикс за раз, % от суммы)
   → цена. Своя цена конкретному человеку — кнопка «👤 Своя цена сотруднику».
3. **💬 Группы** — создайте группу в Telegram, добавьте туда людей и бота. Бот спросит: общая группа
   или группа конкретного сотрудника.
4. **➕ Ещё** — доплаты (коэффициенты), товары и склад, настройки (часовой пояс, лимиты), доступ,
   филиалы (только если у вас несколько точек), журнал.

Проверка: в группе `/op`, выбрать работу и количество, «📤 Отправить», руководитель жмёт «✅ Подтвердить»,
у сотрудника в личке «💰 Мой расчёт» показывает сумму.

## 8. Обновление

```bash
cd ~/bugalter
sh scripts/update.sh
```
Скрипт делает резервную копию, `git pull`, пересборку и перезапуск, показывает логи.
Миграции применяются автоматически. Откат — восстановление из копии (шаг 9) и `git checkout <прошлый коммит>`.

## 9. Резервные копии и восстановление

* Копии делает контейнер `backup` в `~/bugalter/backups/` (формат `pg_dump -Fc`), каждая проверяется
  `pg_restore --list`; старше `BACKUP_KEEP_DAYS` удаляются.
* Разовая копия: `docker compose run --rm backup once`.
* **Копии на том же сервере не спасут при потере сервера.** Настройте копирование наружу, например
  на другой сервер раз в сутки (`crontab -e`):
  ```
  30 4 * * * rsync -a --delete ~/bugalter/backups/ backup@ДРУГОЙ_СЕРВЕР:/srv/bugalter-backups/
  ```
  или в облако через `rclone` (`rclone copy ~/bugalter/backups remote:bugalter-backups`).

Восстановление (текущая база не удаляется, а переименовывается):
```bash
ls -t backups/                                   # самая свежая — первая
sh scripts/restore.sh backups/bugalter_YYYYMMDD_HHMMSS.dump
```
Скрипт спросит подтверждение (`yes`), остановит бота, восстановит копию в новую базу, подменит её и запустит бота.

**Переезд на новый сервер:** шаги 2–5 на новом сервере → скопировать файл копии в `~/bugalter/backups/` →
`docker compose up -d db` → `sh scripts/restore.sh backups/<файл>` → `docker compose up -d`.
Старый бот перед этим остановить (`docker compose stop bot`), иначе два бота будут читать один токен.

## 10. Обслуживание

| Задача | Команда |
|---|---|
| Состояние | `docker compose ps` |
| Логи бота | `docker compose logs --tail=100 bot` (JSON: время, уровень, событие, пользователь) |
| Перезапуск | `docker compose restart bot` |
| Остановить всё | `docker compose down` (данные в томе `pgdata` сохраняются) |
| Консоль базы | `docker compose exec db psql -U bugalter bugalter` |
| Место на диске | `df -h` и `du -sh ~/bugalter/backups` |
| Очистка старых образов | `docker image prune -f` |

При внутренней ошибке бот присылает владельцам сообщение с кодом ошибки; тот же код есть в логах.

## 11. Безопасность

* `.env` — права `600`, в git не попадает (`.gitignore`).
* База не публикуется наружу: порт 5432 не проброшен, доступ только из контейнеров.
* Если токен бота утёк: @BotFather → `/revoke` → новый токен в `.env` → `docker compose up -d`.
* Проведённые операции, проводки, тарифы, партии и журнал изменений защищены триггерами от правки и удаления.
  Пользователь БД из `.env` — владелец схемы и технически может отключить триггеры; поэтому доступ к серверу
  должен быть только у доверенных людей, а копии — храниться вне сервера.

## 12. Если что-то не так

| Симптом | Что проверить |
|---|---|
| `bot` постоянно перезапускается | `docker compose logs bot`: `BOT_TOKEN не задан`, неверный токен, нет доступа к api.telegram.org (нужен `TELEGRAM_PROXY`) |
| Бот молчит в группе | бот добавлен в группу и сделан **администратором** (иначе не видит сообщения с тегами)? группа подключена (бот спрашивает сам при добавлении, иначе `/bind`)? человек подключился по ссылке? |
| Бот молчит в личке | человек ещё не открыл ссылку-приглашение: незнакомым бот не отвечает. Пришлите ему ссылку из карточки сотрудника |
| «Недостаточно прав» | откройте карточку сотрудника в Управлении, Сотрудники, и проверьте его роль |
| «Укажите филиал» в личке | добавьте `@название` к команде или выполните её в рабочей группе |
| `db` не становится healthy | `docker compose logs db`; пароль в `.env` менялся после первого запуска — пароль задаётся только при создании тома |
| Нужно сменить пароль базы | `docker compose exec db psql -U bugalter -c "ALTER USER bugalter PASSWORD 'новый'"`, затем новый пароль в `.env` и `docker compose up -d` |
