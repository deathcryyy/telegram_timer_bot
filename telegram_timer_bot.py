"""
Telegram Timer Bot
==================
Команды:
  /add <имя> <время> <единица> — добавить объект с таймером
      Примеры:
        /add Пицца 30 мин
        /add Стирка 1 час
        /add Лекарство 90 сек
        /add Пицца 00:30:00
        /add Пицца 0 30 0
        /add Пицца 1ч30м
        /add Пицца 45м
        /add Пицца 2ч
  /list   — список активных таймеров
  /cancel <имя> — отменить таймер (только для администраторов)

Установка: py -m pip install python-telegram-bot redis --upgrade
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.error import TimedOut, NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes

# ───────────────────────────── НАСТРОЙКИ ─────────────────────────────
BOT_TOKEN       = "8518716891:AAHaKareX_3dzTSDGyzLZV842OzjGFyNRlo"   # <-- токен от @BotFather
ALLOWED_CHAT_ID = -5130704239                    # <-- ID чата (например: -1001234567890)
SAVE_FILE       = "timers.json"

TZ_MAIN   = ZoneInfo("Europe/Moscow")   # МСК (UTC+3)
TZ_SAMARA = ZoneInfo("Europe/Samara")   # Самара (UTC+4)
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

active_timers: Dict[int, Dict[str, tuple]] = {}


# ═══════════════════════════ ВРЕМЯ ═══════════════════════════════════

def now_msk() -> datetime:
    """Текущее время в МСК."""
    return datetime.now(tz=TZ_MAIN)

def fmt_time(dt: datetime) -> str:
    """Форматирует время в МСК с самарским в скобках."""
    msk = dt.astimezone(TZ_MAIN)
    sam = dt.astimezone(TZ_SAMARA)
    return f"{msk.strftime('%H:%M:%S')} МСК ({sam.strftime('%H:%M:%S')} Самара)"


# ═══════════════════════════ ФИЛЬТР ЧАТА ═════════════════════════════

def allowed(update: Update) -> bool:
    return update.effective_chat.id == ALLOWED_CHAT_ID

async def reject(update: Update):
    logger.warning("Запрос отклонён: chat_id=%s", update.effective_chat.id)


# ═══════════════════════════ ПРОВЕРКА ПРАВ ═══════════════════════════

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, является ли пользователь администратором или создателем чата."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # В личных чатах (приватных) — считаем администратором
    if update.effective_chat.type == "private":
        return True

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception as e:
        logger.error("Ошибка проверки прав администратора: %s", e)
        return False


# ═══════════════════════════ СОХРАНЕНИЕ ══════════════════════════════

def save_timers():
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            import redis
            r = redis.from_url(redis_url)
            data = {}
            for chat_id, timers in active_timers.items():
                data[str(chat_id)] = {
                    name: finish_at.isoformat()
                    for name, (task, finish_at) in timers.items()
                }
            r.set("timers", json.dumps(data, ensure_ascii=False))
            return
        except Exception as e:
            logger.error("Ошибка сохранения в Redis: %s", e)

    data = {}
    for chat_id, timers in active_timers.items():
        data[str(chat_id)] = {
            name: finish_at.isoformat()
            for name, (task, finish_at) in timers.items()
        }
    try:
        with open(SAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Ошибка сохранения таймеров: %s", e)


def load_timers_raw() -> dict:
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            import redis
            r = redis.from_url(redis_url)
            raw = r.get("timers")
            if raw:
                return json.loads(raw)
            return {}
        except Exception as e:
            logger.error("Ошибка чтения из Redis: %s", e)

    if not os.path.exists(SAVE_FILE):
        return {}
    try:
        with open(SAVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Ошибка чтения файла таймеров: %s", e)
        return {}


# ═══════════════════════════ ОТПРАВКА С RETRY ════════════════════════

async def send_with_retry(bot, chat_id: int, text: str, retries: int = 5, delay: float = 5.0):
    for attempt in range(1, retries + 1):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            return
        except (TimedOut, NetworkError) as e:
            if attempt == retries:
                logger.error("Не удалось отправить сообщение после %d попыток: %s", retries, e)
                return
            wait = delay * attempt
            logger.warning("Ошибка отправки (попытка %d/%d): %s. Повтор через %.0f сек...", attempt, retries, e, wait)
            await asyncio.sleep(wait)
        except Exception as e:
            logger.error("Неожиданная ошибка при отправке: %s", e)
            return


# ═══════════════════════════ ПАРСИНГ ВРЕМЕНИ ═════════════════════════

def parse_hhmmss(value: str) -> int:
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


def parse_compact(value: str) -> int:
    import re
    value = value.lower().strip()
    pattern = re.fullmatch(
        r'(?:(\d+)\s*(?:ч|h|час|hours?))?'
        r'\s*(?:(\d+)\s*(?:м|m|мин|min|минут?))?'
        r'\s*(?:(\d+)\s*(?:с|s|сек|sec|секунд?))?',
        value
    )
    if not pattern:
        return -1
    h_str, m_str, s_str = pattern.groups()
    if not any([h_str, m_str, s_str]):
        return -1
    h = int(h_str) if h_str else 0
    m = int(m_str) if m_str else 0
    s = int(s_str) if s_str else 0
    total = h * 3600 + m * 60 + s
    return total if total > 0 else -1


def try_parse_hms_triplet(tokens: list) -> int:
    try:
        h, m, s = int(tokens[0]), int(tokens[1]), int(tokens[2])
        if m >= 60 or s >= 60:
            return -1
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return -1


def parse_duration(value: str, unit: str) -> int:
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
    remaining = int((finish_at - now_msk()).total_seconds())
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


# ═══════════════════════════ ТАЙМЕРЫ ═════════════════════════════════

async def timer_task(bot, chat_id: int, name: str, finish_at: datetime):
    try:
        now = now_msk()
        total_remaining = (finish_at - now).total_seconds()

        if total_remaining <= 0:
            await send_with_retry(
                bot, chat_id,
                f"⚠️ *Таймер «{name}» истёк пока бот был выключен!*\n"
                f"Время окончания было: {fmt_time(finish_at)}"
            )
            return

        warn_at = finish_at - timedelta(seconds=60)
        if warn_at > now and total_remaining > 60:
            await asyncio.sleep((warn_at - now_msk()).total_seconds())
            await send_with_retry(
                bot, chat_id,
                f"⏰ *Внимание!* До конца таймера «{name}» осталась *1 минута*!\n"
                f"Завершение в {fmt_time(finish_at)}"
            )

        remaining_now = (finish_at - now_msk()).total_seconds()
        if remaining_now > 0:
            await asyncio.sleep(remaining_now)

        await send_with_retry(bot, chat_id, f"✅ *Таймер «{name}» завершён!*")

    except asyncio.CancelledError:
        logger.info("Таймер '%s' для чата %s отменён.", name, chat_id)
    finally:
        if chat_id in active_timers and name in active_timers[chat_id]:
            del active_timers[chat_id][name]
            if not active_timers[chat_id]:
                del active_timers[chat_id]
        save_timers()


def start_timer(bot, chat_id: int, name: str, finish_at: datetime) -> asyncio.Task:
    task = asyncio.create_task(timer_task(bot, chat_id, name, finish_at))
    active_timers.setdefault(chat_id, {})[name] = (task, finish_at)
    return task


# ═══════════════════════ ВОССТАНОВЛЕНИЕ ══════════════════════════════

async def restore_timers(bot):
    raw = load_timers_raw()
    if not raw:
        return
    restored = 0
    for chat_id_str, timers in raw.items():
        chat_id = int(chat_id_str)
        for name, finish_iso in timers.items():
            try:
                finish_at = datetime.fromisoformat(finish_iso)
                if finish_at.tzinfo is None:
                    finish_at = finish_at.replace(tzinfo=TZ_MAIN)
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
    if not allowed(update):
        await reject(update)
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "❌ Использование:\n"
            "`/add <имя> <число> <единица>` — `/add Пицца 30 мин`\n"
            "`/add <имя> <ЧЧ:ММ:СС>` — `/add Пицца 00:30:00`\n"
            "`/add <имя> ЧЧ ММ СС` — `/add Пицца 0 30 0`\n"
            "`/add <имя> 1ч30м` — `/add Пицца 1ч30м`\n"
            "Единицы: сек / мин / час",
            parse_mode="Markdown",
        )
        return

    chat_id = update.effective_chat.id
    seconds = -1
    display_duration = ""
    name = ""

    if ":" in args[-1]:
        name = " ".join(args[:-1])
        seconds = parse_hhmmss(args[-1])
        display_duration = args[-1]

    elif len(args) >= 2 and any(c.isalpha() for c in args[-1]) and any(c.isdigit() for c in args[-1]):
        compact = parse_compact(args[-1])
        if compact > 0:
            name = " ".join(args[:-1])
            seconds = compact
            display_duration = args[-1]

    elif len(args) >= 4 and all(a.isdigit() for a in args[-3:]):
        name = " ".join(args[:-3])
        seconds = try_parse_hms_triplet(args[-3:])
        if seconds >= 0:
            h, m, s = int(args[-3]), int(args[-2]), int(args[-1])
            display_duration = f"{h:02d}:{m:02d}:{s:02d}"

    elif len(args) >= 3:
        name = " ".join(args[:-2])
        seconds = parse_duration(args[-2], args[-1])
        display_duration = f"{args[-2]} {args[-1]}"

    if not name or seconds <= 0:
        await update.message.reply_text(
            "❌ Не удалось распознать команду.\n"
            "Примеры: `/add Пицца 30 мин`, `/add Пицца 1ч30м`, `/add Пицца 00:30:00`",
            parse_mode="Markdown",
        )
        return

    if chat_id in active_timers and name in active_timers[chat_id]:
        active_timers[chat_id][name][0].cancel()
        await update.message.reply_text(f"♻️ Старый таймер «{name}» сброшен, создаю новый.")

    finish_at = now_msk() + timedelta(seconds=seconds)
    start_timer(context.bot, chat_id, name, finish_at)
    save_timers()

    warning_note = ""
    if seconds > 60:
        warn_at = finish_at - timedelta(seconds=60)
        warning_note = f"\n🔔 Предупреждение в {fmt_time(warn_at)}"

    await update.message.reply_text(
        f"✅ Таймер *«{name}»* запущен!\n"
        f"⏱ Продолжительность: {display_duration}\n"
        f"🏁 Завершение в {fmt_time(finish_at)}"
        f"{warning_note}",
        parse_mode="Markdown",
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await reject(update)
        return

    chat_id = update.effective_chat.id
    timers = active_timers.get(chat_id, {})
    if not timers:
        await update.message.reply_text("📭 Нет активных таймеров.")
        return

    lines = ["⏳ *Активные таймеры:*"]
    for i, (name, (task, finish_at)) in enumerate(
        sorted(timers.items(), key=lambda x: x[1][1]), 1
    ):
        lines.append(
            f"  {i}. *{name}* — осталось {fmt_remaining(finish_at)}\n"
            f"      до {fmt_time(finish_at)}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await reject(update)
        return

    # ── Проверка прав администратора ──
    if not await is_admin(update, context):
        await update.message.reply_text(
            "🚫 Отменять таймеры могут только администраторы беседы."
        )
        return

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


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await reject(update)
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Использование: `/find <название>`", parse_mode="Markdown"
        )
        return

    chat_id = update.effective_chat.id
    timers = active_timers.get(chat_id, {})
    if not timers:
        await update.message.reply_text("📭 Нет активных таймеров.")
        return

    query = " ".join(context.args).lower()

    exact   = [(n, d) for n, d in timers.items() if n.lower() == query]
    partial = [(n, d) for n, d in timers.items() if query in n.lower() and n.lower() != query]
    matches = exact + partial

    if not matches:
        await update.message.reply_text(
            f"❌ Таймер «{' '.join(context.args)}» не найден."
        )
        return

    lines = []
    for name, (task, finish_at) in matches:
        lines.append(
            f"⏳ *{name}*\n"
            f"   Осталось: {fmt_remaining(finish_at)}\n"
            f"   До {fmt_time(finish_at)}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await reject(update)
        return

    await update.message.reply_text(
        "📖 *Справка по боту:*\n\n"
        "`/add <имя> <число> <единица>`\n"
        "   `/add Пицца 30 мин`\n\n"
        "`/add <имя> <ЧЧ:ММ:СС>`\n"
        "   `/add Пицца 00:30:00`\n\n"
        "`/add <имя> ЧЧ ММ СС`\n"
        "   `/add Пицца 0 30 0`\n\n"
        "`/add <имя> <ч/м/с>`\n"
        "   `/add Пицца 1ч30м` · `/add Пицца 45м` · `/add Пицца 2ч`\n\n"
        "`/list` — показать активные таймеры\n\n"
        "`/find <название>` — найти таймер по названию\n\n"
        "`/cancel <имя>` — отменить таймер *(только администраторы)*\n"
        "`/cancel all` — отменить все *(только администраторы)*\n\n"
        "Единицы: `сек`, `мин`, `час`\n"
        "Время отображается в МСК (и Самарском).\n"
        "Бот предупредит за 1 минуту до завершения.\n"
        "При перезапуске таймеры восстанавливаются автоматически.",
        parse_mode="Markdown",
    )


# ═══════════════════════════════ MAIN ════════════════════════════════

async def post_init(application: Application):
    await restore_timers(application.bot)


def main():
    if BOT_TOKEN == "ВАШ_ТОКЕН_ЗДЕСЬ":
        print("❌ Вставьте токен бота в BOT_TOKEN!")
        return
    if ALLOWED_CHAT_ID == 0:
        print("❌ Укажите ID чата в ALLOWED_CHAT_ID!")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("find",   cmd_find))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("start",  cmd_help))

    logger.info("Бот запущен. Разрешённый чат: %s.", ALLOWED_CHAT_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
