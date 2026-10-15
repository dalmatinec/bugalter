"""Бэкап из бота: скачать копию базы, сделать новую, восстановить из присланного файла.

Сами копии делает контейнер backup (scripts/backup.sh). Бот общается с ним через общую папку:
кладёт заявку .now (сделать копию) или .restore (восстановить из restore.dump) и ждёт результата.
"""

import asyncio
import datetime as dt
import os
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ....domain.errors import AccessDenied, ValidationError
from ...ui import h
from .base import AdminForm, ask, cancel_kb, kb, show

router = Router(name="panel.backups")

# Ссылки на фоновые ожидания, чтобы их не собрал сборщик мусора.
_waiting: set[asyncio.Task] = set()


def _owner(actor) -> None:
    if not actor.is_owner:
        raise AccessDenied("Бэкапы доступны только владельцу")


def _files(folder: Path) -> list[Path]:
    return sorted(folder.glob("bk_*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)


def _size(path: Path) -> str:
    size = path.stat().st_size
    return f"{size / 1024 / 1024:.1f} МБ" if size >= 1024 * 1024 else f"{max(size // 1024, 1)} КБ"


def backups_view(config):
    folder = Path(config.backup_dir)
    lines = [
        "💾 <b>Бэкапы</b>",
        "Копия базы делается сама 3 раза в сутки, хранятся 3 последние: новая заменяет самую старую. "
        "Лежат на сервере в папке <code>backups</code>.",
        "",
    ]
    files = _files(folder) if folder.is_dir() else []
    for path in files:
        when = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)
        lines.append(f"• {h(path.name)} · {_size(path)} · {when:%d.%m %H:%M} UTC")
    if not files:
        lines.append("Копий пока нет. Нажмите Сделать сейчас.")
    lines += [
        "",
        "Переезд на другой сервер: скачайте копию здесь, на новом сервере откройте Бэкап, "
        "нажмите Восстановить и пришлите файл.",
    ]
    rows = [[("📥 Скачать", "p:bk:get"), ("💾 Сделать сейчас", "p:bk:now")], [("♻️ Восстановить", "p:bk:rs")]]
    rows.append([("⬅️ Назад", "p:s")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "p:bk")
async def backups(callback: CallbackQuery, state: FSMContext, actor, config, notifier):
    _owner(actor)
    await state.clear()
    show(callback, notifier, *backups_view(config))


@router.callback_query(F.data == "p:bk:get")
async def backup_download(callback: CallbackQuery, actor, config, notifier):
    _owner(actor)
    files = _files(Path(config.backup_dir)) if Path(config.backup_dir).is_dir() else []
    if not files:
        raise ValidationError("Копий пока нет. Нажмите Сделать сейчас")
    notifier.document(callback.message.chat.id, files[0].read_bytes(), files[0].name, "💾 Последняя копия базы")


async def _send_new_backup(bot: Bot, chat_id: int, folder: Path, since: float) -> None:
    for _ in range(36):
        await asyncio.sleep(5)
        fresh = [p for p in _files(folder) if p.stat().st_mtime > since]
        if fresh:
            await bot.send_document(
                chat_id, BufferedInputFile(fresh[0].read_bytes(), fresh[0].name), caption="💾 Свежая копия базы"
            )
            return
    await bot.send_message(chat_id, "⚠️ Копия не появилась за 3 минуты. Проверьте контейнер backup на сервере.")


@router.callback_query(F.data == "p:bk:now")
async def backup_now(callback: CallbackQuery, actor, config, notifier, bot: Bot):
    _owner(actor)
    folder = Path(config.backup_dir)
    if not folder.is_dir():
        raise ValidationError("Папка бэкапов не найдена. Обновите бота на сервере (docker compose up -d)")
    (folder / ".now").touch()
    task = asyncio.create_task(_send_new_backup(bot, callback.message.chat.id, folder, dt.datetime.now().timestamp()))
    _waiting.add(task)
    task.add_done_callback(_waiting.discard)
    notifier.toast(callback, "Делаю копию, пришлю файл через минуту")


@router.callback_query(F.data == "p:bk:rs")
async def restore_ask(callback: CallbackQuery, state: FSMContext, actor, notifier):
    _owner(actor)
    await ask(state, "restore_file")
    show(
        callback,
        notifier,
        "♻️ Пришлите сюда файл копии (.dump), который скачали в Бэкапе.\n"
        "Перед восстановлением я спрошу ещё раз. Текущая база сохранится отдельной копией.",
        cancel_kb("p:bk"),
    )


@router.message(AdminForm.input, F.document)
async def restore_file(message: Message, state: FSMContext, actor, config, notifier, bot: Bot):
    data = await state.get_data()
    if data.get("form") != "restore_file":
        return
    _owner(actor)
    doc = message.document
    if not (doc.file_name or "").endswith(".dump"):
        raise ValidationError("Нужен файл копии с расширением .dump")
    folder = Path(config.backup_dir)
    target = folder / "restore.dump"
    await bot.download(doc, destination=target)
    await state.clear()
    notifier.answer(
        message,
        f"Файл <b>{h(doc.file_name)}</b> получен.\n\n"
        "⚠️ Все текущие данные бота заменятся данными из этого файла. "
        "Текущая база сохранится копией. Бот перезапустится, это займёт около минуты.",
        kb([[("♻️ Да, восстановить", "p:bk:ry"), ("✖️ Нет", "p:bk")]]),
    )


async def _wait_restore(bot: Bot, chat_id: int, folder: Path) -> None:
    result_file = folder / ".restore_result"
    for _ in range(60):
        await asyncio.sleep(5)
        if result_file.exists():
            result = result_file.read_text().strip()
            result_file.unlink(missing_ok=True)
            if result == "ok":
                await bot.send_message(chat_id, "✅ Данные восстановлены. Перезапускаюсь, через минуту всё работает.")
                # Перезапуск: docker поднимет бота заново, миграции догонят схему, если копия старее.
                asyncio.get_running_loop().call_later(2, os._exit, 0)
            else:
                await bot.send_message(chat_id, f"❌ Восстановить не получилось, данные не изменены.\n{h(result)}")
            return
    await bot.send_message(chat_id, "⚠️ Нет ответа 5 минут. Проверьте контейнер backup на сервере.")


@router.callback_query(F.data == "p:bk:ry")
async def restore_go(callback: CallbackQuery, actor, config, notifier, bot: Bot):
    _owner(actor)
    folder = Path(config.backup_dir)
    if not (folder / "restore.dump").exists():
        raise ValidationError("Сначала пришлите файл копии")
    (folder / ".restore").touch()
    task = asyncio.create_task(_wait_restore(bot, callback.message.chat.id, folder))
    _waiting.add(task)
    task.add_done_callback(_waiting.discard)
    show(callback, notifier, "♻️ Восстанавливаю. Сообщу, когда закончу.")
