#!/bin/sh
# Резервная копия PostgreSQL в формате custom (pg_restore). Параметры — стандартные PG* переменные.
#   sh backup.sh        — одна копия
#   sh backup.sh loop   — копия каждые BACKUP_INTERVAL_SECONDS, старше BACKUP_KEEP_DAYS удаляются
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"

backup_once() {
    mkdir -p "$BACKUP_DIR"
    ts="$(date -u +%Y%m%d_%H%M%S)"
    file="$BACKUP_DIR/bugalter_${ts}.dump"
    pg_dump --format=custom --no-owner --file="${file}.tmp"
    # Проверяем, что архив читается, и только потом считаем копию готовой.
    pg_restore --list "${file}.tmp" > /dev/null
    mv "${file}.tmp" "$file"
    find "$BACKUP_DIR" -name 'bugalter_*.dump' -mtime +"$KEEP_DAYS" -delete
    echo "$(date -u +%FT%TZ) backup ok: $file ($(du -h "$file" | cut -f1))"
}

if [ "${1:-once}" = "loop" ]; then
    while true; do
        backup_once || echo "$(date -u +%FT%TZ) backup FAILED" >&2
        sleep "$INTERVAL"
    done
else
    backup_once
fi
