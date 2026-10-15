#!/bin/sh
# Резервные копии PostgreSQL в формате custom (pg_restore). Параметры подключения: стандартные PG* переменные.
#   sh backup.sh        одна копия
#   sh backup.sh loop   копия каждые BACKUP_INTERVAL_SECONDS (по умолчанию 8 часов, то есть 3 раза в сутки
#                       в разное время). Хранятся BACKUP_KEEP самых новых (по умолчанию 3): новая копия
#                       заменяет самую старую. Ещё выполняет заявки из бота (кнопка Бэкап в Настройках):
#                       файл .now значит сделать копию сейчас, .restore значит восстановить из restore.dump.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP="${BACKUP_KEEP:-3}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-28800}"
DB="${PGDATABASE:-bugalter}"

log() { echo "$(date -u +%FT%TZ) $*"; }

rotate() {
    ls -1t "$BACKUP_DIR"/bk_*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do rm -f "$old"; done
}

backup_once() {
    mkdir -p "$BACKUP_DIR"
    file="$BACKUP_DIR/bk_$(date -u +%d%m_%H%M)${1:-}.dump"
    pg_dump --format=custom --no-owner --file="${file}.tmp"
    # Проверяем, что архив читается, и только потом считаем копию готовой.
    pg_restore --list "${file}.tmp" > /dev/null
    mv "${file}.tmp" "$file"
    chmod 644 "$file"
    rotate
    log "backup ok: $file ($(du -h "$file" | cut -f1))"
}

# Восстановление без остановки бота: копия разворачивается во временную базу, затем базы меняются местами.
# Текущая база остаётся под именем <база>_old (предыдущая _old удаляется), бот после этого перезапускается сам.
restore_from() {
    src="$1"
    pg_restore --list "$src" > /dev/null
    backup_once _pre  # копия того, что было до восстановления
    psql -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"${DB}_restore\"" \
        -c "CREATE DATABASE \"${DB}_restore\""
    pg_restore --no-owner --exit-on-error -d "${DB}_restore" "$src"
    psql -d postgres -v ON_ERROR_STOP=1 \
        -c "DROP DATABASE IF EXISTS \"${DB}_old\"" \
        -c "ALTER DATABASE \"$DB\" WITH ALLOW_CONNECTIONS false" \
        -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB' AND pid <> pg_backend_pid()" \
        -c "ALTER DATABASE \"$DB\" RENAME TO \"${DB}_old\"" \
        -c "ALTER DATABASE \"${DB}_restore\" RENAME TO \"$DB\"" \
        -c "ALTER DATABASE \"${DB}_old\" WITH ALLOW_CONNECTIONS true"
    log "restore ok from $src"
}

if [ "${1:-once}" = "loop" ]; then
    mkdir -p "$BACKUP_DIR"
    chmod 777 "$BACKUP_DIR"  # бот кладёт сюда заявки и присланный файл копии
    last=0
    while true; do
        if [ -f "$BACKUP_DIR/.restore" ]; then
            rm -f "$BACKUP_DIR/.restore" "$BACKUP_DIR/.restore_result"
            if restore_from "$BACKUP_DIR/restore.dump" > "$BACKUP_DIR/.restore_log" 2>&1; then
                echo "ok" > "$BACKUP_DIR/.restore_result"
            else
                echo "error: $(tail -3 "$BACKUP_DIR/.restore_log")" > "$BACKUP_DIR/.restore_result"
            fi
            chmod 666 "$BACKUP_DIR/.restore_result"
            last="$(date +%s)"
        fi
        now="$(date +%s)"
        if [ -f "$BACKUP_DIR/.now" ] || [ "$((now - last))" -ge "$INTERVAL" ]; then
            rm -f "$BACKUP_DIR/.now"
            backup_once || log "backup FAILED" >&2
            last="$now"
        fi
        sleep 10
    done
else
    backup_once
fi
