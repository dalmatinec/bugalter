#!/bin/sh
# Обновление бота на сервере: резервная копия → git pull → сборка → перезапуск.
# Миграции базы применяются автоматически при старте контейнера bot.
set -eu
cd "$(dirname "$0")/.."

echo "== Резервная копия перед обновлением"
docker compose run --rm backup once

echo "== Получение изменений"
git pull --ff-only

echo "== Сборка и перезапуск"
docker compose up -d --build

echo "== Состояние"
docker compose ps
sleep 5
docker compose logs --tail=30 bot
