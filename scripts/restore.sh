#!/bin/sh
# Восстановление базы из резервной копии:  sh scripts/restore.sh backups/bugalter_YYYYMMDD_HHMMSS.dump
# Текущая база не удаляется: она переименовывается в <имя>_before_restore_<время>.
set -eu
cd "$(dirname "$0")/.."

DUMP="${1:?Укажите файл копии: sh scripts/restore.sh backups/bugalter_....dump}"
[ -f "$DUMP" ] || { echo "Нет файла $DUMP"; exit 1; }

env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- ; }
DB_USER="$(env_value POSTGRES_USER)"; DB_USER="${DB_USER:-bugalter}"
DB_NAME="$(env_value POSTGRES_DB)"; DB_NAME="${DB_NAME:-bugalter}"
STAMP="$(date -u +%Y%m%d%H%M%S)"
OLD_NAME="${DB_NAME}_before_restore_${STAMP}"

echo "База $DB_NAME будет восстановлена из $DUMP."
echo "Текущая база сохранится под именем $OLD_NAME."
printf "Продолжить? Введите yes: "
read -r answer
[ "$answer" = "yes" ] || { echo "Отменено"; exit 1; }

echo "== Останавливаю бота"
docker compose stop bot

echo "== Восстанавливаю копию во временную базу"
docker compose exec -T db dropdb -U "$DB_USER" --if-exists "${DB_NAME}_restore"
docker compose exec -T db createdb -U "$DB_USER" "${DB_NAME}_restore"
docker compose exec -T db pg_restore -U "$DB_USER" --no-owner --exit-on-error -d "${DB_NAME}_restore" < "$DUMP"

echo "== Подменяю базу"
docker compose exec -T db psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "ALTER DATABASE \"$DB_NAME\" RENAME TO \"$OLD_NAME\"" \
    -c "ALTER DATABASE \"${DB_NAME}_restore\" RENAME TO \"$DB_NAME\""

echo "== Запускаю бота"
docker compose up -d bot
echo "Готово. Старая база: $OLD_NAME (удалить, когда убедитесь, что всё в порядке:"
echo "  docker compose exec db dropdb -U $DB_USER $OLD_NAME )"
