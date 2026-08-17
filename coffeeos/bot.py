"""Telegram-бот КофейняОС — боевая версия.

Интерфейс для владельца и смены: кнопки, свободные вопросы, ввод списаний,
остатков и цен, утренняя сводка, недельные итоги, вечернее напоминание и
авто-контроль здоровья системы.

Запуск:  python -m coffeeos bot
"""
import logging
import re
import datetime as dt
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          ContextTypes, filters)
from . import config, db, orchestrator, health

# ---------- логирование ----------
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        # ротация: лог не должен за пару месяцев забить диск рядом с базой
        RotatingFileHandler(config.LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3,
                            encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
# httpx пишет строку на каждый опрос Telegram (~9 тыс/сутки) и светит токен в URL
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("coffeeos.bot")

KEYBOARD = ReplyKeyboardMarkup(
    [["💰 Маржа и меню", "🥐 Витрина на завтра"],
     ["📦 Заявка поставщику", "🥐 Еда к кофе"],
     ["💸 Упущенная выручка", "👥 Смена"],
     ["☕ Что пьют", "🥛 Молоко"],
     ["📊 Экспресс-аудит", "🗑 Списания"],
     ["📈 Выручка", "🧾 Сверка кассы"]],
    resize_keyboard=True)

COMMANDS = [
    BotCommand("svodka", "Сводка сейчас"),
    BotCommand("vitrina", "Витрина на завтра"),
    BotCommand("marzha", "Маржа и разбор меню"),
    BotCommand("zakupki", "Заявка поставщику"),
    BotCommand("upusheno", "Сколько недопродали"),
    BotCommand("smena", "Загрузка смены"),
    BotCommand("nedelya", "Итоги недели"),
    BotCommand("status", "Состояние системы"),
    BotCommand("help", "Что я умею"),
]


def _conn():
    return db.get_conn()


def allowed_ids():
    """Кому разрешено пользоваться ботом: владелец и смена.
    Поставщик сюда НЕ входит — ему бот только отправляет заявки, а обороты
    и аналитика кофейни контрагенту не предназначены."""
    ids = set(config.BRIEF_CHAT_IDS) | set(config.STAFF_CHAT_IDS)
    return {str(i).strip() for i in ids if str(i).strip()}


def is_allowed(update: Update):
    """Доступ к данным кофейни — только у своих.

    Раньше при пустом списке получателей бот пускал ЛЮБОГО, кто нашёл его в
    поиске: достаточно было не заполнить одну строку в .env, чтобы выручка,
    план и упущенные продажи стали публичными. Теперь при пустом списке
    работает только /start — он показывает человеку его chat_id, чтобы
    владелец вписал нужные в .env. Всё остальное закрыто.
    """
    ids = allowed_ids()
    chat = update.effective_chat
    return bool(ids) and chat is not None and str(chat.id) in ids


async def _deny(update: Update):
    log.warning("Отказано в доступе: chat_id=%s (%s)",
                getattr(update.effective_chat, "id", "?"),
                getattr(update.effective_user, "username", "?"))
    try:
        await update.effective_message.reply_text(
            "Этот бот обслуживает конкретную кофейню и доступен только её сотрудникам.")
    except Exception:
        pass


async def _reply(update, text, markup=None):
    if update.message is None:
        return
    # Пытаемся с Markdown; если Telegram отклонит разметку — шлём обычным текстом,
    # чтобы пользователь всегда получил ответ, а не сообщение об ошибке.
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log.warning("Markdown отклонён, шлю простым текстом: %s", e)
        await update.message.reply_text(text, reply_markup=markup)


# ---------- команды ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    uid = update.effective_chat.id
    if not is_allowed(update):
        if not allowed_ids():
            # первая настройка: список пуст, показываем владельцу его id —
            # но НИКАКИХ данных кофейни, пока он не вписан в .env
            await update.message.reply_text(
                f"Бот запущен, но список допущенных пуст — данные пока закрыты для всех.\n\n"
                f"Ваш ID: {uid}\n"
                f"Впишите его в файл .env (строка BRIEF_CHAT_IDS) и перезапустите бота — "
                f"тогда откроются кнопки, сводка и отчёты.")
            return
        await update.message.reply_text(
            f"Этот бот обслуживает конкретную кофейню.\n"
            f"Если вы её сотрудник — передайте владельцу свой ID: {uid}")
        return
    # без Markdown — чтобы id с любыми символами гарантированно отобразился
    await update.message.reply_text(
        f"Здравствуйте, {config.OWNER_NAME} 👋\n"
        f"Я — КофейняОС, операционная система «{config.VENUE_NAME}».\n\n"
        f"Спросите что угодно про кофейню или нажмите кнопку ниже.\n\n"
        f"Ваш ID: {uid}\n"
        f"Впишите это число в файл .env (строка BRIEF_CHAT_IDS), чтобы получать утреннюю сводку.",
        reply_markup=KEYBOARD)


async def cmd_help(update, ctx):
    if not is_allowed(update):
        return await _deny(update)
    await _reply(update, orchestrator.help_text(), KEYBOARD)


async def _agent_reply(update, fn_name):
    if not is_allowed(update):
        return await _deny(update)
    import asyncio

    def _work():
        conn = _conn()
        try:
            return getattr(orchestrator, fn_name)(conn)
        finally:
            conn.close()

    text = await asyncio.to_thread(_work)
    await _reply(update, text, _markup_for(text))


def _markup_for(reply):
    """Заказ витрины всегда получает кнопку «отправить смене» — и по кнопке,
    и по команде /vitrina. Заявка — кнопку «отправить поставщику»."""
    if reply.startswith("🥐 *Витрина"):
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Утвердить и отправить смене",
                                   callback_data="approve_case")]])
    if reply.startswith("📦 *Заявка"):
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("📤 Отправить поставщику", callback_data="send_order")],
             [InlineKeyboardButton("✏️ Дописать от руки", callback_data="rewrite_order")]])
    return None


async def cmd_svodka(update, ctx):   await _agent_reply(update, "morning_brief")
async def cmd_vitrina(update, ctx):  await _agent_reply(update, "answer_case")
async def cmd_marzha(update, ctx):   await _agent_reply(update, "answer_margin")
async def cmd_smena(update, ctx):    await _agent_reply(update, "answer_shift")
async def cmd_nedelya(update, ctx):  await _agent_reply(update, "weekly_brief")
async def cmd_upusheno(update, ctx): await _agent_reply(update, "answer_lost")
async def cmd_zakupki(update, ctx):  await _agent_reply(update, "answer_supply")


ORDER_PROMPT = (
    "✏️ Допишите, что добавить к заявке — свободным текстом.\n\n"
    "Например:\n"
    "Салфетки — 2 упаковки\n"
    "Средство для чистки групп\n"
    "Ванильный сироп — 1 бутылка\n\n"
    "Передумали — напишите «отмена».")


async def cmd_status(update, ctx):
    if not is_allowed(update):
        return await _deny(update)
    # status_text ходит по сети (проверка связи с моделью) и в базу. Раньше это
    # выполнялось прямо в цикле событий, и на несколько секунд бот переставал
    # отвечать всем остальным.
    import asyncio
    text = await asyncio.to_thread(health.status_text)
    await _reply(update, text)


# ---------- свободный текст ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message is None:          # отредактированное сообщение / пост в канале
        return
    if not is_allowed(update):
        return await _deny(update)
    text = update.message.text or ""
    low = text.lower().strip()

    # 1) владелец дописывает строки к заявке от руки
    if ctx.user_data.get("awaiting_order"):
        if low in ("отмена", "отменить", "стоп", "/cancel", "назад"):
            ctx.user_data["awaiting_order"] = False
            await update.message.reply_text("Отменил. Спрашивайте что угодно 🙂",
                                            reply_markup=KEYBOARD)
            return
        # Клавиатура остаётся на экране и приглашает нажать кнопку. Нажатие —
        # это не список закупки: раньше поставщику уходила заявка с текстом
        # «📈 Выручка». Нажатие кнопки считаем выходом из режима и обрабатываем
        # как обычную команду.
        ctx.user_data["awaiting_order"] = False
        if low not in orchestrator.BUTTONS:
            ctx.user_data["extra_order"] = text.strip()
            msg = ("📦 Добавлю к заявке:\n\n" + text.strip() + "\n\nОтправляем?")
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Отправить поставщику", callback_data="send_order")],
                [InlineKeyboardButton("✏️ Переписать", callback_data="rewrite_order")]])
            await update.message.reply_text(msg, reply_markup=markup)
            return

    # 2) обычная маршрутизация. Ответ считаем в отдельном потоке: обращение к
    # модели — блокирующее, и без этого бот замирал бы на время её ответа.
    import asyncio

    def _work():
        conn = _conn()
        try:
            return orchestrator.answer(conn, text)
        finally:
            conn.close()

    # Локальная qwen2.5:3b на ноутбуке может думать 10–40 секунд. Показываем
    # «печатает…» всё это время, чтобы бот не выглядел зависшим.
    task = asyncio.create_task(asyncio.to_thread(_work))
    while not task.done():
        try:
            await update.effective_chat.send_action("typing")
        except Exception:
            pass
        await asyncio.wait({task}, timeout=4.5)
    reply = await task
    await _reply(update, reply, _markup_for(reply))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await _deny(update)
    q = update.callback_query
    await q.answer()
    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass
    if q.data == "approve_case":
        await _approve_case(ctx, q)
    elif q.data == "rewrite_order":
        ctx.user_data["awaiting_order"] = True
        await q.message.reply_text(ORDER_PROMPT)
    elif q.data == "send_order":
        await _send_order(ctx, q)


async def _approve_case(ctx, q):
    """Утвердить заказ витрины и отправить его смене."""
    import asyncio

    def _work():
        conn = _conn()
        try:
            return orchestrator.approve_case_order(conn)
        finally:
            conn.close()

    try:
        plan = await asyncio.to_thread(_work)
    except Exception as e:                                  # noqa: BLE001
        log.warning("approve_case error: %s", e)
        await q.message.reply_text("Не смог собрать заказ. Попробуйте ещё раз.")
        return
    owner_chat = str(q.message.chat_id)
    targets = [c for c in config.STAFF_CHAT_IDS if str(c) != owner_chat]
    sent = 0
    for cid in targets:
        try:
            await ctx.bot.send_message(int(cid), plan["text"], parse_mode="Markdown")
            sent += 1
        except Exception as e:                              # noqa: BLE001
            log.warning("case send error to %s: %s", cid, e)
    if sent:
        await q.message.reply_text(f"✅ Заказ витрины утверждён и отправлен смене ({sent} получ.).")
    elif targets:
        await q.message.reply_text(
            "⚠️ Заказ утверждён, но отправить смене не удалось — "
            "проверьте STAFF_CHAT_IDS в .env и что сотрудник написал боту /start.")
    else:
        await q.message.reply_text(
            "✅ Заказ утверждён и сохранён.\n"
            "Отправлять пока некому: впишите chat_id бариста в STAFF_CHAT_IDS в .env "
            "(он узнает свой id, написав боту /start).")


async def _send_order(ctx, q):
    """Отправить поставщику посчитанную заявку (плюс дописанное от руки)."""
    import asyncio

    def _work():
        conn = _conn()
        try:
            from . import supply
            return supply.order_draft(conn)
        finally:
            conn.close()

    draft = await asyncio.to_thread(_work)
    extra = (ctx.user_data.get("extra_order") or "").strip()
    body = draft["text"]
    if extra:
        body = (body + "\n\nОт руки:\n" + extra) if body else extra
    if not body:
        await q.message.reply_text(
            "Заказывать нечего: расход не посчитан. Нажмите «📦 Заявка поставщику», "
            "чтобы посмотреть, что мешает.")
        return
    text = f"📦 Заявка от «{config.VENUE_NAME}»:\n\n{body}"
    if config.SUPPLIER_CHAT_ID:
        try:
            await ctx.bot.send_message(int(config.SUPPLIER_CHAT_ID), text)
            await q.message.reply_text("✅ Отправлено поставщику.")
        except Exception as e:                              # noqa: BLE001
            log.warning("supplier send error: %s", e)
            await q.message.reply_text(
                "Не удалось отправить автоматически. Вот заявка — перешлите её сами:\n\n"
                + text)
    else:
        await q.message.reply_text(
            text + "\n\n_Перешлите это поставщику. Чтобы бот отправлял сам, "
                   "впишите SUPPLIER_CHAT_ID в .env._",
            parse_mode="Markdown")
    ctx.user_data["extra_order"] = ""


# ---------- фоновые задачи ----------
async def send_brief(ctx: ContextTypes.DEFAULT_TYPE):
    conn = _conn()
    try:
        text = orchestrator.morning_brief(conn)
        # по пятницам добавляем итоги недели
        if dt.datetime.now(ZoneInfo(config.TIMEZONE)).weekday() == 4:
            text += "\n\n" + orchestrator.weekly_brief(conn)
    finally:
        conn.close()
    for cid in config.BRIEF_CHAT_IDS:
        try:
            await ctx.bot.send_message(int(cid), text, parse_mode="Markdown", reply_markup=KEYBOARD)
        except Exception as e:
            log.warning("brief markdown error to %s: %s — шлю простым текстом", cid, e)
            try:
                await ctx.bot.send_message(int(cid), text, reply_markup=KEYBOARD)
            except Exception as e2:
                log.warning("brief send error to %s: %s", cid, e2)


async def closing_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    # Напоминание намеренно мягкое: план выпечки считается по чекам и работает
    # без этого ввода. Просить персонал о том, без чего система не живёт, —
    # значит потерять систему через месяц вместе с текучкой продавцов.
    text = ("🌙 Смена закрывается. Если есть минута — отметьте, что осталось и что вылили: "
            "«списание круассаны 4», «вылил молоко 1,5». Это уточняет заказ на завтра, "
            "но не обязательно: что закончилось раньше времени, я вижу и сам по чекам. "
            "А вот вылитое молоко не видно нигде, кроме этой строки.")
    for cid in config.STAFF_CHAT_IDS:
        try:
            await ctx.bot.send_message(int(cid), text)
        except Exception as e:
            log.warning("closing reminder error to %s: %s", cid, e)


async def auto_sync(ctx: ContextTypes.DEFAULT_TYPE):
    import asyncio
    from . import sync
    try:
        # в отдельном потоке — чтобы загрузка большого файла не подвешивала бота
        res = await asyncio.to_thread(sync.run_once)
        if res.get("items") or res.get("failed") or res.get("error"):
            log.info("Автозагрузка: %s", res)
    except Exception as e:
        log.warning("Автозагрузка: ошибка %r", e)


async def daily_backup(ctx: ContextTypes.DEFAULT_TYPE):
    import asyncio
    from . import backup
    try:
        res = await asyncio.to_thread(backup.make_backup)
        log.info("Резервная копия: %s", res)
    except Exception as e:
        log.warning("Резервная копия: ошибка %r", e)


async def health_watch(ctx: ContextTypes.DEFAULT_TYPE):
    import asyncio
    warn = await asyncio.to_thread(health.alert_if_broken)
    if warn:
        log.warning("health alert: %s", warn)
        for cid in config.BRIEF_CHAT_IDS:
            try:
                await ctx.bot.send_message(int(cid), warn)
            except Exception as e:
                log.warning("health alert send error: %s", e)


def _error_text(err):
    """Понятная причина вместо «что-то пошло не так».

    Занятая база — не поломка, а очередь: в этот момент идёт загрузка большой
    выгрузки. Сказать об этом честно дешевле, чем заставлять владельца гадать
    и писать в поддержку.
    """
    import sqlite3
    if isinstance(err, sqlite3.OperationalError) and "locked" in str(err).lower():
        return ("Секунду — сейчас идёт загрузка выгрузки чеков, и база занята записью. "
                "Повторите через минуту, ничего не потерялось.")
    if isinstance(err, sqlite3.DatabaseError):
        return ("Не смог прочитать базу. Проверьте на сервере: "
                "`python -m coffeeos status` — и, если нужно, восстановите последнюю "
                "копию из папки backups.")
    return ("Упс, что-то пошло не так — я уже записал ошибку. "
            "Попробуйте ещё раз или нажмите /svodka.")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("Ошибка при обработке обновления: %s", ctx.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(_error_text(ctx.error))
    except Exception:
        pass


# ---------- сборка ----------
async def _post_init(app: Application):
    await app.bot.set_my_commands(COMMANDS)
    log.info("Команды бота зарегистрированы.")


def build_app():
    problems = config.validate()
    if any("BOT_TOKEN" in p for p in problems):
        raise SystemExit("Не задан BOT_TOKEN. Получите токен у @BotFather и впишите в .env")
    bad_time = [p for p in problems if "ЧЧ:ММ" in p]
    if bad_time:
        raise SystemExit(bad_time[0] + " Проверьте BRIEF_TIME и CLOSE_TIME в .env.")
    bad_id = [p for p in problems if "не похож на число" in p]
    if bad_id:
        raise SystemExit(bad_id[0] + " Исправьте BRIEF_CHAT_IDS/STAFF_CHAT_IDS в .env "
                                     "(только цифры, несколько — через запятую), "
                                     "иначе бот не пустит вас самих.")
    for p in problems:
        log.warning("Настройка: %s", p)
    db.init_db()

    app = Application.builder().token(config.BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("svodka", cmd_svodka))
    app.add_handler(CommandHandler("vitrina", cmd_vitrina))
    app.add_handler(CommandHandler("marzha", cmd_marzha))
    app.add_handler(CommandHandler("smena", cmd_smena))
    app.add_handler(CommandHandler("zakupki", cmd_zakupki))
    app.add_handler(CommandHandler("nedelya", cmd_nedelya))
    app.add_handler(CommandHandler("upusheno", cmd_upusheno))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    tz = ZoneInfo(config.TIMEZONE)
    bh, bm = config.brief_hour_minute()
    ch, cm = config.close_hour_minute()
    app.job_queue.run_daily(send_brief, time=dt.time(bh, bm, tzinfo=tz), name="morning_brief")
    app.job_queue.run_daily(closing_reminder, time=dt.time(ch, cm, tzinfo=tz), name="closing_reminder")
    app.job_queue.run_repeating(health_watch, interval=6 * 3600, first=120, name="health_watch")
    app.job_queue.run_daily(daily_backup, time=dt.time(max(0, min(23, config.BACKUP_HOUR)), 15, tzinfo=tz),
                            name="daily_backup")
    if config.SYNC_MODE and config.SYNC_MODE != "off":
        app.job_queue.run_repeating(auto_sync, interval=max(5, config.SYNC_INTERVAL_MIN) * 60,
                                    first=30, name="auto_sync")
        log.info("Автозагрузка чеков включена: режим %s, каждые %s мин.",
                 config.SYNC_MODE, config.SYNC_INTERVAL_MIN)
    return app


def main():
    app = build_app()
    log.info("КофейняОС запущена. Сводка %s, напоминание %s (%s).",
             config.BRIEF_TIME, config.CLOSE_TIME, config.TIMEZONE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
