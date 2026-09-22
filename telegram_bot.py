import logging
from datetime import datetime, date, timedelta

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    filters, ContextTypes,
)

from config import Config
from app import app, db
from models import User, Schedule, Attendance, Absence, Notification
from utils import parse_time_string, time_to_minutes, calculate_worked_hours, format_timedelta_hhmm

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния разговора
(
    ASK_EMAIL,
    PLAN_DATE, PLAN_START, PLAN_END,
    FACT_DATE, FACT_START, FACT_END, FACT_EFF,
    ABS_TYPE, ABS_DATE_START, ABS_DATE_END, ABS_CUSTOM, ABS_FILE,
) = range(13)

DAYS_RU = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
MONTHS_RU = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']


# ---------- УТИЛИТЫ ----------
def get_user_by_chat(chat_id):
    return User.query.filter_by(telegram_chat_id=str(chat_id)).first()


def main_menu_keyboard():
    kb = [
        [KeyboardButton('📅 Мой профиль'), KeyboardButton('📋 Расписание на неделю')],
        [KeyboardButton('✏️ Предложить план'), KeyboardButton('⏰ Отметить факт')],
        [KeyboardButton('🏖 Заявка на отпуск'), KeyboardButton('👥 Кто работает')],
        [KeyboardButton('📊 Итоги месяца'), KeyboardButton('❓ Помощь')],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def cancel_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton('❌ Отмена')]], resize_keyboard=True)


def require_registration(func):
    """Декоратор: требует привязки telegram_chat_id."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        with app.app_context():
            user = get_user_by_chat(chat_id)
            if not user:
                await update.message.reply_text(
                    "❌ Ты ещё не привязал аккаунт.\n"
                    "Отправь /start и введи свой email."
                )
                return
        return await func(update, context)
    return wrapper


# ---------- СТАРТ И РЕГИСТРАЦИЯ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if user:
            await update.message.reply_text(
                f"👋 С возвращением, {user.full_name}!\n\n"
                f"Выбери действие в меню ниже 👇",
                reply_markup=main_menu_keyboard()
            )
            return

    await update.message.reply_text(
        "👋 Привет! Я — бот WorkTracker.\n\n"
        "Чтобы привязать Telegram к твоей учётной записи, "
        "пришли свой email (тот, под которым ты входишь на сайт).",
        reply_markup=cancel_keyboard()
    )
    return ASK_EMAIL


async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text == '❌ отмена':
        await update.message.reply_text("Отменено. Отправь /start, чтобы начать заново.")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    tg_user = update.effective_user

    with app.app_context():
        user = User.query.filter_by(email=text).first()
        if not user:
            await update.message.reply_text(
                "❌ Email не найден.\n"
                "Проверь написание или обратись к администратору.\n\n"
                "Попробуй ещё раз или нажми «❌ Отмена»."
            )
            return ASK_EMAIL

        user.telegram_chat_id = str(chat_id)
        if tg_user.username:
            user.telegram_id = f"@{tg_user.username}"
        db.session.commit()

        await update.message.reply_text(
            f"✅ Отлично, {user.full_name}!\n"
            f"Твой аккаунт привязан.\n\n"
            f"Выбери действие в меню 👇",
            reply_markup=main_menu_keyboard()
        )
    return ConversationHandler.END


# ---------- ПРОФИЛЬ ----------
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return

        today = date.today()
        schedule = Schedule.query.filter_by(user_id=user.id, date=today).first()
        attendance = Attendance.query.filter_by(user_id=user.id, date=today).first()

        text = f"👤 *{user.full_name}*\n"
        text += f"📧 {user.email}\n"
        text += f"🆔 {user.role}\n\n"
        text += f"*Сегодня ({today.strftime('%d.%m.%Y')}):*\n"

        if schedule:
            if schedule.is_day_off:
                text += "🏖 План: выходной\n"
            else:
                text += f"📋 План: {schedule.planned_start.strftime('%H:%M')} – {schedule.planned_end.strftime('%H:%M')}\n"
        else:
            text += "📋 План: не указан\n"

        if attendance:
            text += f"⏰ Факт: {attendance.actual_start.strftime('%H:%M')} – {attendance.actual_end.strftime('%H:%M')}\n"
            text += f"📈 Эффективность: {int(attendance.efficiency * 100)}%\n"

        await update.message.reply_text(text, parse_mode='Markdown',
                                        reply_markup=main_menu_keyboard())


# ---------- РАСПИСАНИЕ НА НЕДЕЛЮ ----------
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    offset = context.user_data.get('week_offset', 0)

    if isinstance(update.message, type(None)):
        # вызов из callback
        message = update.callback_query.message
    else:
        message = update.message

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return

        today = date.today()
        start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        end_of_week = start_of_week + timedelta(days=6)

        text = f"📋 *Расписание на неделю*\n"
        text += f"{start_of_week.strftime('%d.%m')} – {end_of_week.strftime('%d.%m.%Y')}\n\n"

        for i in range(7):
            day = start_of_week + timedelta(days=i)
            sch = Schedule.query.filter_by(user_id=user.id, date=day).first()
            att = Attendance.query.filter_by(user_id=user.id, date=day).first()

            day_str = DAYS_RU[day.weekday()][:2]
            date_str = day.strftime('%d.%m')

            if sch:
                if sch.is_day_off:
                    line = f"• {day_str} {date_str}: 🏖 выходной"
                else:
                    line = f"• {day_str} {date_str}: 📋 {sch.planned_start.strftime('%H:%M')}–{sch.planned_end.strftime('%H:%M')}"
            else:
                line = f"• {day_str} {date_str}: — не указано"

            if att:
                line += f"\n   ⏰ факт: {att.actual_start.strftime('%H:%M')}–{att.actual_end.strftime('%H:%M')} (e% {int(att.efficiency*100)})"

            text += line + "\n"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⬅️ Прошлая", callback_data="week_prev"),
                InlineKeyboardButton("Текущая", callback_data="week_now"),
                InlineKeyboardButton("Следующая ➡️", callback_data="week_next"),
            ]
        ])

        await message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard)


async def week_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "week_prev":
        context.user_data['week_offset'] = context.user_data.get('week_offset', 0) - 1
    elif query.data == "week_next":
        context.user_data['week_offset'] = context.user_data.get('week_offset', 0) + 1
    elif query.data == "week_now":
        context.user_data['week_offset'] = 0

    # Перерисовываем сообщение
    chat_id = query.message.chat_id
    offset = context.user_data.get('week_offset', 0)

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.edit_message_text("❌ Аккаунт не привязан.")
            return

        today = date.today()
        start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        end_of_week = start_of_week + timedelta(days=6)

        text = f"📋 *Расписание на неделю*\n"
        text += f"{start_of_week.strftime('%d.%m')} – {end_of_week.strftime('%d.%m.%Y')}\n\n"

        for i in range(7):
            day = start_of_week + timedelta(days=i)
            sch = Schedule.query.filter_by(user_id=user.id, date=day).first()
            att = Attendance.query.filter_by(user_id=user.id, date=day).first()

            day_str = DAYS_RU[day.weekday()][:2]
            date_str = day.strftime('%d.%m')

            if sch:
                if sch.is_day_off:
                    line = f"• {day_str} {date_str}: 🏖 выходной"
                else:
                    line = f"• {day_str} {date_str}: 📋 {sch.planned_start.strftime('%H:%M')}–{sch.planned_end.strftime('%H:%M')}"
            else:
                line = f"• {day_str} {date_str}: — не указано"

            if att:
                line += f"\n   ⏰ факт: {att.actual_start.strftime('%H:%M')}–{att.actual_end.strftime('%H:%M')}"

            text += line + "\n"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⬅️ Прошлая", callback_data="week_prev"),
                InlineKeyboardButton("Текущая", callback_data="week_now"),
                InlineKeyboardButton("Следующая ➡️", callback_data="week_next"),
            ]
        ])

        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=keyboard)


# ---------- КТО РАБОТАЕТ ----------
async def cmd_who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with app.app_context():
        today = date.today()
        plans = Schedule.query.filter_by(date=today, status='approved').all()

        text = f"👥 *Кто работает сегодня ({today.strftime('%d.%m.%Y')})*\n\n"
        found = False
        for plan in plans:
            if plan.is_day_off or not plan.user or plan.user.status != 'active':
                continue
            found = True
            if plan.planned_start and plan.planned_end:
                text += f"• {plan.user.full_name}: {plan.planned_start.strftime('%H:%M')} – {plan.planned_end.strftime('%H:%M')}\n"

        if not found:
            text += "Сегодня никто не работает."

        await update.message.reply_text(text, parse_mode='Markdown',
                                        reply_markup=main_menu_keyboard())


# ---------- ПРЕДЛОЖИТЬ ПЛАН ----------
async def plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *Новый план на день*\n\n"
        "Введи дату в формате *ДД.ММ.ГГГГ*\n"
        "Например: `23.09.2026`",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return PLAN_DATE


async def plan_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '❌ Отмена':
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    try:
        d = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введи как `23.09.2026`", parse_mode='Markdown')
        return PLAN_DATE

    context.user_data['plan_date'] = d
    await update.message.reply_text(
        "🕐 Введи *время прихода* в формате *ЧЧ:ММ*\nНапример: `10:00`",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return PLAN_START


async def plan_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '❌ Отмена':
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        start_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Неверный формат. Введи как `10:00`", parse_mode='Markdown')
        return PLAN_START

    context.user_data['plan_start'] = start_time
    await update.message.reply_text(
        "🕕 Введи *время ухода* в формате *ЧЧ:ММ*\nНапример: `18:00`",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return PLAN_END


async def plan_end_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '❌ Отмена':
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        end_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Неверный формат. Введи как `18:00`", parse_mode='Markdown')
        return PLAN_END

    start_time = context.user_data['plan_start']
    if start_time >= end_time:
        await update.message.reply_text("❌ Время ухода должно быть позже времени прихода. Попробуй снова:")
        return PLAN_END

    d = context.user_data['plan_date']
    chat_id = update.effective_chat.id

    with app.app_context():
        user = get_user_by_chat(chat_id)
        existing = Schedule.query.filter_by(user_id=user.id, date=d).first()
        if existing:
            await update.message.reply_text(
                f"⚠️ На {d.strftime('%d.%m.%Y')} уже есть заявка. Удали её на сайте.",
                reply_markup=main_menu_keyboard()
            )
            return ConversationHandler.END

        schedule = Schedule(
            user_id=user.id, date=d,
            planned_start=start_time, planned_end=end_time,
            is_day_off=False, status='approved'
        )
        db.session.add(schedule)
        db.session.commit()

    await update.message.reply_text(
        f"✅ План сохранён!\n\n"
        f"📅 {d.strftime('%d.%m.%Y')}\n"
        f"⏰ {start_time.strftime('%H:%M')} – {end_time.strftime('%H:%M')}",
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


# ---------- ОТМЕТИТЬ ФАКТ ----------
async def fact_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().strftime('%d.%m.%Y')
    await update.message.reply_text(
        f"⏰ *Отметить фактическое время*\n\n"
        f"Введи дату в формате *ДД.ММ.ГГГГ*\n"
        f"Сегодня: `{today}`",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return FACT_DATE


async def fact_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '❌ Отмена':
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    try:
        d = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введи как `23.09.2026`", parse_mode='Markdown')
        return FACT_DATE

    if d > date.today():
        await update.message.reply_text("❌ Нельзя отмечать факт на будущую дату.")
        return FACT_DATE

    context.user_data['fact_date'] = d
    await update.message.reply_text(
        "🕐 Введи *время прихода* в формате *ЧЧ:ММ*\nНапример: `10:00`",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return FACT_START


async def fact_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '❌ Отмена':
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        start_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как `10:00`", parse_mode='Markdown')
        return FACT_START

    context.user_data['fact_start'] = start_time
    await update.message.reply_text("🕕 Введи *время ухода* в формате *ЧЧ:ММ*", parse_mode='Markdown',
                                    reply_markup=cancel_keyboard())
    return FACT_END


async def fact_end_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '❌ Отмена':
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        end_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как `18:00`", parse_mode='Markdown')
        return FACT_END

    if context.user_data['fact_start'] >= end_time:
        await update.message.reply_text("❌ Время ухода должно быть позже времени прихода.")
        return FACT_END

    context.user_data['fact_end'] = end_time
    await update.message.reply_text("📈 Введи *эффективность* в процентах (0-100)", parse_mode='Markdown',
                                    reply_markup=cancel_keyboard())
    return FACT_EFF


async def fact_efficiency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '❌ Отмена':
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    try:
        eff = float(text)
        if not (0 <= eff <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи число от 0 до 100")
        return FACT_EFF

    chat_id = update.effective_chat.id
    d = context.user_data['fact_date']
    start_time = context.user_data['fact_start']
    end_time = context.user_data['fact_end']

    with app.app_context():
        user = get_user_by_chat(chat_id)
        existing = Attendance.query.filter_by(user_id=user.id, date=d).first()
        if existing:
            await update.message.reply_text(
                f"⚠️ На {d.strftime('%d.%m.%Y')} уже есть отметка.",
                reply_markup=main_menu_keyboard()
            )
            return ConversationHandler.END

        schedule = Schedule.query.filter_by(user_id=user.id, date=d, status='approved').first()
        early_start = 0
        if schedule and schedule.planned_start and not schedule.is_day_off:
            early_start = time_to_minutes(start_time) - time_to_minutes(schedule.planned_start)

        status = 'confirmed' if d == date.today() else 'pending'

        att = Attendance(
            user_id=user.id, date=d,
            actual_start=start_time, actual_end=end_time,
            efficiency=eff / 100, early_start=early_start,
            status=status
        )
        db.session.add(att)
        db.session.commit()

    if status == 'pending':
        await update.message.reply_text(
            f"✅ Факт сохранён и ждёт подтверждения администратора.",
            reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            f"✅ Факт сохранён!\n\n"
            f"📅 {d.strftime('%d.%m.%Y')}\n"
            f"⏰ {start_time.strftime('%H:%M')} – {end_time.strftime('%H:%M')}\n"
            f"📈 e%: {int(eff)}",
            reply_markup=main_menu_keyboard()
        )
    return ConversationHandler.END


# ---------- ЗАЯВКА НА ОТПУСК ----------
async def abs_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏖 Отпуск", callback_data="abs_vacation")],
        [InlineKeyboardButton("🤒 Больничный", callback_data="abs_sick")],
        [InlineKeyboardButton("📌 Другое", callback_data="abs_other")],
        [InlineKeyboardButton("❌ Отмена", callback_data="abs_cancel")],
    ])
    await update.message.reply_text(
        "🏖 *Заявка на отсутствие*\n\n"
        "Выбери тип:",
        parse_mode='Markdown',
        reply_markup=kb
    )
    return ABS_TYPE


async def abs_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "abs_cancel":
        await query.edit_message_text("Отменено.")
        await query.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    types = {"abs_vacation": "vacation", "abs_sick": "sick", "abs_other": "other"}
    context.user_data['abs_type'] = types.get(query.data, 'vacation')

    await query.edit_message_text(
        "📅 Введи *дату начала* в формате *ДД.ММ.ГГГГ*\nНапример: `25.09.2026`",
        parse_mode='Markdown'
    )
    return ABS_DATE_START


async def abs_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '❌ Отмена':
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    try:
        d = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введи как `25.09.2026`", parse_mode='Markdown')
        return ABS_DATE_START

    context.user_data['abs_start'] = d
    await update.message.reply_text(
        "📅 Введи *дату окончания* в формате *ДД.ММ.ГГГГ*",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return ABS_DATE_END


async def abs_date_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '❌ Отмена':
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    try:
        d = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Введи как `30.09.2026`", parse_mode='Markdown')
        return ABS_DATE_END

    if d < context.user_data['abs_start']:
        await update.message.reply_text("❌ Дата окончания раньше даты начала. Попробуй снова:")
        return ABS_DATE_END

    context.user_data['abs_end'] = d

    if context.user_data.get('abs_type') == 'other':
        await update.message.reply_text(
            "📝 Опиши причину отсутствия (например: «отгул за переработку», «учёба»):",
            reply_markup=cancel_keyboard()
        )
        return ABS_CUSTOM
    else:
        # Переходим к подтверждению
        return await abs_confirm(update, context)


async def abs_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '❌ Отмена':
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    if len(text) > 200:
        await update.message.reply_text("❌ Слишком длинно. Максимум 200 символов.")
        return ABS_CUSTOM

    context.user_data['abs_custom'] = text
    return await abs_confirm(update, context)


async def abs_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    d_start = context.user_data['abs_start']
    d_end = context.user_data['abs_end']
    abs_type = context.user_data['abs_type']
    custom = context.user_data.get('abs_custom')

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return ConversationHandler.END

        absence = Absence(
            user_id=user.id,
            date_start=d_start, date_end=d_end,
            type=abs_type,
            custom_type=custom if abs_type == 'other' else None,
            status='pending'
        )
        db.session.add(absence)
        db.session.commit()

        # Уведомление админам
        admins = User.query.filter_by(role='admin').all()
        for admin in admins:
            if admin.telegram_chat_id:
                type_ru = {'vacation': 'отпуск', 'sick': 'больничный', 'other': 'другое'}.get(abs_type, abs_type)
                notif = Notification(
                    user_id=admin.id,
                    chat_id=admin.telegram_chat_id,
                    message=f"📩 {user.full_name} подал заявку на {type_ru}: {d_start.strftime('%d.%m.%Y')} – {d_end.strftime('%d.%m.%Y')}"
                )
                db.session.add(notif)
        db.session.commit()

    type_ru = {'vacation': '🏖 Отпуск', 'sick': '🤒 Больничный', 'other': f'📌 {custom}'}.get(abs_type, abs_type)

    await update.message.reply_text(
        f"✅ *Заявка отправлена!*\n\n"
        f"📌 Тип: {type_ru}\n"
        f"📅 {d_start.strftime('%d.%m.%Y')} – {d_end.strftime('%d.%m.%Y')}\n\n"
        f"Ожидай подтверждения администратора. "
        f"Уведомление придёт сюда.",
        parse_mode='Markdown',
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


# ---------- ИТОГИ МЕСЯЦА ----------
async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return

        today = date.today()
        start_date = date(today.year, today.month, 1)
        import calendar as cal_mod
        end_date = date(today.year, today.month, cal_mod.monthrange(today.year, today.month)[1])

        total_worked_min = 0
        total_eff_min = 0
        total_late = 0
        count_days = 0

        attendances = Attendance.query.filter(
            Attendance.user_id == user.id,
            Attendance.date >= start_date,
            Attendance.date <= end_date
        ).all()

        for att in attendances:
            if att.actual_start and att.actual_end:
                worked = calculate_worked_hours(att.actual_start, att.actual_end)
                wm = int(worked.total_seconds() // 60)
                total_worked_min += wm
                total_eff_min += int(wm * att.efficiency)
                count_days += 1
                if att.early_start and att.early_start > 0:
                    total_late += att.early_start

        avg_eff = (total_eff_min / total_worked_min * 100) if total_worked_min > 0 else 0
        final_min = total_eff_min - total_late

        text = (
            f"📊 *Итоги за {MONTHS_RU[today.month-1]} {today.year}*\n\n"
            f"👤 {user.full_name}\n\n"
            f"📅 Дней отработано: {count_days}\n"
            f"⏱ Отработано часов: {total_worked_min / 60:.2f}\n"
            f"📈 Средний e%: {avg_eff:.1f}%\n"
            f"⏰ Опоздания (часов): {total_late / 60:.2f}\n"
            f"✅ Итого часов: {final_min / 60:.2f}"
        )
        await update.message.reply_text(text, parse_mode='Markdown',
                                        reply_markup=main_menu_keyboard())


# ---------- ПОМОЩЬ ----------
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ *Помощь по боту WorkTracker*\n\n"
        "📅 *Мой профиль* — данные за сегодня\n"
        "📋 *Расписание на неделю* — твой график с фактом\n"
        "✏️ *Предложить план* — указать план на день\n"
        "⏰ *Отметить факт* — фактическое время и эффективность\n"
        "🏖 *Заявка на отпуск* — отпуск / больничный / другое\n"
        "👥 *Кто работает* — сотрудники на сегодня\n"
        "📊 *Итоги месяца* — статистика за текущий месяц\n\n"
        "Все команды доступны через кнопки внизу экрана 👇"
    )
    await update.message.reply_text(text, parse_mode='Markdown',
                                    reply_markup=main_menu_keyboard())


# ---------- ОБРАБОТКА ТЕКСТА МЕНЮ ----------
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == '📅 Мой профиль':
        await cmd_profile(update, context)
    elif text == '📋 Расписание на неделю':
        await cmd_week(update, context)
    elif text == '👥 Кто работает':
        await cmd_who(update, context)
    elif text == '📊 Итоги месяца':
        await cmd_summary(update, context)
    elif text == '❓ Помощь':
        await cmd_help(update, context)
    elif text == '❌ Отмена':
        await update.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text(
            "🤔 Не понимаю. Используй кнопки внизу 👇",
            reply_markup=main_menu_keyboard()
        )


# ---------- ПРОВЕРКА УВЕДОМЛЕНИЙ ----------
async def check_notifications(context: ContextTypes.DEFAULT_TYPE):
    with app.app_context():
        pending = Notification.query.filter_by(status='pending').all()
        for n in pending:
            try:
                await context.bot.send_message(chat_id=n.chat_id, text=n.message)
                n.status = 'sent'
            except Exception as e:
                logger.error(f"Не отправлено уведомление {n.id}: {e}")
                n.status = 'failed'
        db.session.commit()


# ---------- ЗАПУСК ----------
def main():
    if not Config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не задан в .env")
        return

    application = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    # Регистрация
    reg_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            ASK_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email)],
        },
        fallbacks=[CommandHandler('start', start)],
    )
    application.add_handler(reg_handler)

    # План
    plan_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^✏️ Предложить план$'), plan_start)],
        states={
            PLAN_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_date)],
            PLAN_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_start_time)],
            PLAN_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_end_time)],
        },
        fallbacks=[MessageHandler(filters.Regex('^❌ Отмена$'), lambda u, c: ConversationHandler.END)],
    )
    application.add_handler(plan_handler)

    # Факт
    fact_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^⏰ Отметить факт$'), fact_start)],
        states={
            FACT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, fact_date)],
            FACT_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, fact_start_time)],
            FACT_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, fact_end_time)],
            FACT_EFF: [MessageHandler(filters.TEXT & ~filters.COMMAND, fact_efficiency)],
        },
        fallbacks=[MessageHandler(filters.Regex('^❌ Отмена$'), lambda u, c: ConversationHandler.END)],
    )
    application.add_handler(fact_handler)

    # Отпуск
    abs_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🏖 Заявка на отпуск$'), abs_start)],
        states={
            ABS_TYPE: [CallbackQueryHandler(abs_type_callback, pattern='^abs_')],
            ABS_DATE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, abs_date_start)],
            ABS_DATE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, abs_date_end)],
            ABS_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, abs_custom)],
        },
        fallbacks=[MessageHandler(filters.Regex('^❌ Отмена$'), lambda u, c: ConversationHandler.END)],
    )
    application.add_handler(abs_handler)

    # Остальные кнопки меню
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))

    # Инлайн-кнопки для недели
    application.add_handler(CallbackQueryHandler(week_callback, pattern='^week_'))

    # Периодическая проверка уведомлений
    application.job_queue.run_repeating(check_notifications, interval=5, first=5)

    print("🤖 Бот запущен. Нажми Ctrl+C для остановки.")
    application.run_polling()


if __name__ == '__main__':
    main()