"""
Telegram Timer Bot
==================
Команды:
  /add <имя> <время> <единица> — добавить объект с таймером
      Примеры:
        /add Пицца 30 мин
        /add Стирка 1 час
        /add Лекарство 90 сек
  /list   — список активных таймеров (сортировка по ближайшему)
  /cancel <имя> — отменить таймер

Бот присылает уведомление за 1 минуту до конца и по истечении.
При перезапуске таймеры восстанавливаются из файла timers.json.

Установка зависимостей:
  py -m pip install python-telegram-bot --upgrade

Запуск:
  1. Вставьте BOT_TOKEN ниже (получить у @BotFather)
  2. py telegram_timer_bot.py
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ───────────────────────────── НАСТРОЙКИ ─────────────────────────────
BOT_TOKEN   = "8518716891:AAHaKareX_3dzTSDGyzLZV842OzjGFyNRlo"   # <-- токен от @BotFather
SAVE_FILE   = "timers.json"        # файл сохранения (рядом со скриптом)
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# {chat_id: {name: (asyncio.Task, finish_at: datetime)}}
active_timers: Dict[int, Dict[str, tuple]] = {}


# ═══════════════════════════ СОХРАНЕНИЕ ══════════════════════════════

def save_timers():
    """Сохраняет все активные таймеры в JSON-файл."""
    data = {}
    for chat_id, timers in active_timers.items():
        data[str(chat_id)] = {}
        for name, (task, finish_at) in timers.items():
            data[str(chat_id)][name] = finish_at.isoformat()
    try:
        with open(SAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug("Таймеры сохранены (%d чатов).", len(data))
    except Exception as e:
        logger.error("Ошибка сохранения таймеров: %s", e)


def load_timers_raw() -> dict:
    """Загружает сырые данные из JSON-файла."""
    if not os.path.exists(SAVE_FILE):
        return {}
    try:
        with open(SAVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Ошибка чтения файла таймеров: %s", e)
        return {}


# ═══════════════════════════ ТАЙМЕРЫ ═════════════════════════════════

def parse_hhmmss(value: str) -> int:
    """Парсит строку вида ЧЧ:ММ:СС или ММ:СС в секунды. Возвращает -1 при ошибке."""
    parts = value.split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(parts[1])
        else:
            return -1
        if m >= 60 or s >= 60:
            return -1
        return h * 3600 + m * 60 + s
    except ValueError:
        return -1



    """Конвертирует значение + единицу в секунды. Возвращает -1 при ошибке."""
    unit = unit.lower().strip()
    try:
        v = float(value.replace(",", "."))
    except ValueError:
        return -1

    if unit in ("сек", "с", "sec", "s", "секунд", "секунды", "секунда"):
        return int(v)
    if unit in ("мин", "м", "min", "m", "минут", "минуты", "минута", "минуту"):
        return int(v * 60)
    if unit in ("час", "ч", "h", "hr", "hour", "часов", "часа", "часы"):
        return int(v * 3600)
    return -1


def fmt_remaining(finish_at: datetime) -> str:
    """Форматирует оставшееся время в читаемую строку."""
    remaining = int((finish_at - datetime.now()).total_seconds())
    if remaining <= 0:
        return "завершается..."
    hours, rem = divmod(remaining, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}мин")
    if seconds and not hours:
        parts.append(f"{seconds}сек")
    return " ".join(parts) if parts else "< 1 сек"


async def timer_task(bot, chat_id: int, name: str, finish_at: datetime):
    """
    Корутина таймера.
    finish_at — абсолютное время завершения (уже вычислено при создании).
    При восстановлении после перезапуска корректно досчитывает оставшееся время.
    """
    try:
        now = datetime.now()
        total_remaining = (finish_at - now).total_seconds()

        if total_remaining <= 0:
            # Таймер уже истёк пока бот был выключен
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ *Таймер «{name}» истёк пока бот был выключен!*\n"
                    f"Время окончания было: {finish_at.strftime('%H:%M:%S %d.%m.%Y')}"
                ),
                parse_mode="Markdown",
            )
            return

        finish_str = finish_at.strftime("%H:%M:%S")

        # Нужно ли ещё слать предупреждение за 1 минуту?
        warn_at = finish_at - timedelta(seconds=60)
        if warn_at > now and total_remaining > 60:
            sleep_until_warn = (warn_at - now).total_seconds()
            await asyncio.sleep(sleep_until_warn)
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏰ *Внимание!* До конца таймера «{name}» осталась *1 минута*!\n"
                    f"Завершение в {finish_str}"
                ),
                parse_mode="Markdown",
            )

        # Ждём до самого конца
        remaining_now = (finish_at - datetime.now()).total_seconds()
        if remaining_now > 0:
            await asyncio.sleep(remaining_now)

        await bot.send_message(
            chat_id=chat_id,
            text=f"✅ *Таймер «{name}» завершён!*",
            parse_mode="Markdown",
        )

    except asyncio.CancelledError:
        logger.info("Таймер '%s' для чата %s отменён.", name, chat_id)
    finally:
        if chat_id in active_timers and name in active_timers[chat_id]:
            del active_timers[chat_id][name]
            if not active_timers[chat_id]:
                del active_timers[chat_id]
        save_timers()  # обновляем файл после завершения/отмены


def start_timer(bot, chat_id: int, name: str, finish_at: datetime) -> asyncio.Task:
    """Создаёт asyncio-задачу для таймера и регистрирует её."""
    task = asyncio.create_task(timer_task(bot, chat_id, name, finish_at))
    active_timers.setdefault(chat_id, {})[name] = (task, finish_at)
    return task


# ═══════════════════════ ВОССТАНОВЛЕНИЕ ══════════════════════════════

async def restore_timers(bot):
    """При запуске бота восстанавливает таймеры из файла."""
    raw = load_timers_raw()
    if not raw:
        return

    restored = 0
    for chat_id_str, timers in raw.items():
        chat_id = int(chat_id_str)
        for name, finish_iso in timers.items():
            try:
                finish_at = datetime.fromisoformat(finish_iso)
            except ValueError:
                logger.warning("Невалидная дата для таймера '%s', пропускаю.", name)
                continue
            start_timer(bot, chat_id, name, finish_at)
            restored += 1

    if restored:
        logger.info("Восстановлено таймеров: %d", restored)
        save_timers()


# ═════════════════════════ ОБРАБОТЧИКИ ═══════════════════════════════

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/add <имя> <число> <единица>  или  /add <имя> <ЧЧ:ММ:СС>"""
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "❌ Использование:\n"
            "`/add <имя> <число> <единица>` — например `/add Пицца 30 мин`\n"
            "`/add <имя> <ЧЧ:ММ:СС>` — например `/add Пицца 00:30:00`\n"
            "Единицы: сек / мин / час",
            parse_mode="Markdown",
        )
        return

    chat_id = update.effective_chat.id
    seconds = -1
    display_duration = ""

    # Проверяем, является ли последний аргумент форматом ЧЧ:ММ:СС / ММ:СС
    if ":" in args[-1]:
        name = " ".join(args[:-1])
        seconds = parse_hhmmss(args[-1])
        display_duration = args[-1]
    elif len(args) >= 3:
        *name_parts, value_str, unit_str = args
        name = " ".join(name_parts)
        seconds = parse_duration(value_str, unit_str)
        display_duration = f"{value_str} {unit_str}"
    else:
        name = args[0]  # на случай если только 1 аргумент после имени — покажем ошибку ниже

    if seconds <= 0:
        await update.message.reply_text(
            "❌ Не удалось распознать время.\n"
            "Используйте: `30 мин`, `1 час`, `90 сек` или `01:30:00`",
            parse_mode="Markdown",
        )
        return

    # Если таймер с таким именем уже есть — отменяем старый
    if chat_id in active_timers and name in active_timers[chat_id]:
        active_timers[chat_id][name][0].cancel()
        await update.message.reply_text(f"♻️ Старый таймер «{name}» сброшен, создаю новый.")

    finish_at = datetime.now() + timedelta(seconds=seconds)
    start_timer(context.bot, chat_id, name, finish_at)
    save_timers()

    finish_str = finish_at.strftime("%H:%M:%S")
    warning_note = ""
    if seconds > 60:
        warn_str = (finish_at - timedelta(seconds=60)).strftime("%H:%M:%S")
        warning_note = f"\n🔔 Предупреждение в {warn_str}"

    await update.message.reply_text(
        f"✅ Таймер *«{name}»* запущен!\n"
        f"⏱ Продолжительность: {display_duration}\n"
        f"🏁 Завершение в {finish_str}"
        f"{warning_note}",
        parse_mode="Markdown",
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list — список активных таймеров, от ближайшего к дальнему."""
    chat_id = update.effective_chat.id
    timers = active_timers.get(chat_id, {})

    if not timers:
        await update.message.reply_text("📭 Нет активных таймеров.")
        return

    sorted_timers = sorted(timers.items(), key=lambda x: x[1][1])

    lines = ["⏳ *Активные таймеры:*"]
    for i, (name, (task, finish_at)) in enumerate(sorted_timers, 1):
        remaining = fmt_remaining(finish_at)
        finish_str = finish_at.strftime("%H:%M:%S")
        lines.append(f"  {i}. *{name}* — осталось {remaining} (до {finish_str})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel <имя> | all — отменить таймер по имени или все сразу."""
    if not context.args:
        await update.message.reply_text(
            "❌ Использование: `/cancel <имя>` или `/cancel all`", parse_mode="Markdown"
        )
        return

    name = " ".join(context.args)
    chat_id = update.effective_chat.id

    if name.lower() == "all":
        timers = active_timers.get(chat_id, {})
        if not timers:
            await update.message.reply_text("📭 Нет активных таймеров для отмены.")
            return
        count = len(timers)
        for task, _ in list(timers.values()):
            task.cancel()
        await update.message.reply_text(f"🛑 Все таймеры отменены ({count} шт.).")
        return

    if chat_id not in active_timers or name not in active_timers[chat_id]:
        await update.message.reply_text(f"❌ Таймер «{name}» не найден.")
        return

    active_timers[chat_id][name][0].cancel()
    await update.message.reply_text(f"🛑 Таймер «{name}» отменён.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Справка по боту:*\n\n"
        "`/add <имя> <число> <единица>` — создать таймер\n"
        "   Пример: `/add Пицца 30 мин`\n"
        "`/add <имя> <ЧЧ:ММ:СС>` — создать таймер в формате времени\n"
        "   Пример: `/add Пицца 00:30:00`\n\n"
        "`/list` — показать активные таймеры\n\n"
        "`/cancel <имя>` — отменить таймер\n"
        "`/cancel all` — отменить все таймеры\n\n"
        "Единицы времени: `сек`, `мин`, `час`\n\n"
        "Бот предупредит за 1 минуту до завершения (если таймер > 1 мин).\n"
        "При перезапуске все таймеры автоматически восстанавливаются.",
        parse_mode="Markdown",
    )


# ═══════════════════════════════ MAIN ════════════════════════════════

async def post_init(application: Application):
    """Вызывается после инициализации бота — восстанавливаем таймеры."""
    await restore_timers(application.bot)


def main():
    if BOT_TOKEN == "ВАШ_ТОКЕН_ЗДЕСЬ":
        print("❌ Пожалуйста, вставьте токен бота в переменную BOT_TOKEN!")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("start",  cmd_help))

    logger.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
