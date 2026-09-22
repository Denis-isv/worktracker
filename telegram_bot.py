import logging
import os
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
from utils import parse_time_string, time_to_minutes, calculate_worked_hours

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния
(
    ASK_EMAIL,
    PLAN_DATE, PLAN_START, PLAN_END,
    FACT_DATE, FACT_START, FACT_END, FACT_EFF,
    ABS_TYPE, ABS_DATE_START, ABS_DATE_END, ABS_CUSTOM, ABS_FILE,
) = range(13)

DAYS_RU_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
DAYS_RU_FULL = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
MONTHS_RU_FULL = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']

ROLE_RU = {'employee': 'Сотрудник', 'admin': 'Администратор'}
ABS_TYPE_RU = {'vacation': '🏖 Отпуск', 'sick': '🤒 Больничный', 'other': '📌 Другое'}
STATUS_RU = {
    'pending': '⏳ ожидает',
    'approved': '✅ подтверждено',
    'rejected': '❌ отклонено',
    'confirmed': '✅ подтверждено',
    'pending_deletion': '🗑 ожидает удаления',
}

# Кнопки главного меню
MENU_BUTTONS = [
    '👤 Мой профиль', '📋 Расписание недели',
    '✏️ Предложить план', '⏰ Отметить факт',
    '🏖 Заявка на отпуск', '📂 Мои заявки',
    '👥 Кто работает', '📊 Итоги месяца',
    '❓ Помощь', '🔓 Отвязать аккаунт',
]

CANCEL_TEXT = '❌ Отмена'


# ---------- УТИЛИТЫ ----------
def get_user_by_chat(chat_id):
    return User.query.filter_by(telegram_chat_id=str(chat_id)).first()


def main_menu_keyboard():
    kb = [
        [KeyboardButton('👤 Мой профиль'), KeyboardButton('📋 Расписание недели')],
        [KeyboardButton('✏️ Предложить план'), KeyboardButton('⏰ Отметить факт')],
        [KeyboardButton('🏖 Заявка на отпуск'), KeyboardButton('📂 Мои заявки')],
        [KeyboardButton('👥 Кто работает'), KeyboardButton('📊 Итоги месяца')],
        [KeyboardButton('❓ Помощь'), KeyboardButton('🔓 Отвязать аккаунт')],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(CANCEL_TEXT)]],
        resize_keyboard=True
    )


async def route_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Маршрутизирует нажатие кнопки меню в нужный обработчик."""
    if text == '👤 Мой профиль':
        await cmd_profile(update, context)
    elif text == '📋 Расписание недели':
        await cmd_week(update, context)
    elif text == '👥 Кто работает':
        await cmd_who(update, context)
    elif text == '📂 Мои заявки':
        await cmd_my_absences(update, context)
    elif text == '📊 Итоги месяца':
        await cmd_summary(update, context)
    elif text == '❓ Помощь':
        await cmd_help(update, context)
    elif text == '🔓 Отвязать аккаунт':
        await detach_start(update, context)
    elif text == '✏️ Предложить план':
        return await plan_start(update, context)
    elif text == '⏰ Отметить факт':
        return await fact_start(update, context)
    elif text == '🏖 Заявка на отпуск':
        return await abs_start(update, context)
    return None


async def universal_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Универсальный обработчик: если нажата кнопка меню — прерываем диалог и выполняем действие."""
    text = update.message.text
    if text in MENU_BUTTONS:
        # Сбрасываем состояние
        context.user_data.pop('plan_date', None)
        context.user_data.pop('plan_start', None)
        context.user_data.pop('fact_date', None)
        context.user_data.pop('fact_start', None)
        context.user_data.pop('fact_end', None)
        context.user_data.pop('abs_type', None)
        context.user_data.pop('abs_start', None)
        context.user_data.pop('abs_end', None)
        context.user_data.pop('abs_custom', None)

        # Выполняем действие
        result = await route_menu_action(update, context, text)
        # Если действие открывает новый Conversation, возвращаем нужное состояние
        if result is not None:
            return result
        return ConversationHandler.END

    elif text == CANCEL_TEXT:
        await update.message.reply_text(
            "Отменено.",
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    else:
        await update.message.reply_text(
            "🤔 Не понимаю. Используй кнопки меню внизу 👇",
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END


# ---------- ТЕКСТЫ ----------
def profile_text(user):
    today = date.today()
    schedule = Schedule.query.filter_by(user_id=user.id, date=today).first()
    attendance = Attendance.query.filter(
        Attendance.user_id == user.id,
        Attendance.date == today,
        Attendance.status != 'rejected'
    ).first()
    absence = Absence.query.filter(
        Absence.user_id == user.id,
        Absence.date_start <= today,
        Absence.date_end >= today,
        Absence.status == 'approved'
    ).first()

    role_ru = ROLE_RU.get(user.role, user.role)

    text = f"👤 *{user.full_name}*\n"
    text += f"📧 {user.email}\n"
    text += f"🎭 Роль: {role_ru}\n"
    if user.telegram_id:
        text += f"💬 {user.telegram_id}\n"
    text += "\n"

    if absence:
        abs_ru = ABS_TYPE_RU.get(absence.type, absence.type)
        if absence.type == 'other' and absence.custom_type:
            abs_ru = f"📌 {absence.custom_type}"
        text += f"{abs_ru} до {absence.date_end.strftime('%d.%m.%Y')}\n\n"

    text += f"*Сегодня — {DAYS_RU_FULL[today.weekday()]}, {today.strftime('%d.%m.%Y')}:*\n"

    if schedule and schedule.status != 'rejected':
        if schedule.is_day_off:
            text += "🏖 План: выходной\n"
        else:
            text += f"📋 План: {schedule.planned_start.strftime('%H:%M')} – {schedule.planned_end.strftime('%H:%M')}\n"
    else:
        text += "📋 План: не указан\n"

    if attendance:
        text += f"⏰ Факт: {attendance.actual_start.strftime('%H:%M')} – {attendance.actual_end.strftime('%H:%M')}\n"
        text += f"📈 Эффективность: {int(attendance.efficiency * 100)}%\n"
        text += f"🎯 Статус: {STATUS_RU.get(attendance.status, attendance.status)}"

    return text


def week_text(user, offset):
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    end_of_week = start_of_week + timedelta(days=6)

    text = f"📋 *Расписание на неделю*\n"
    text += f"{start_of_week.strftime('%d.%m')} – {end_of_week.strftime('%d.%m.%Y')}\n\n"

    for i in range(7):
        day = start_of_week + timedelta(days=i)
        sch = Schedule.query.filter_by(user_id=user.id, date=day).first()
        att = Attendance.query.filter(
            Attendance.user_id == user.id,
            Attendance.date == day,
            Attendance.status != 'rejected'
        ).first()

        if sch and sch.status == 'rejected':
            sch = None

        day_short = DAYS_RU_SHORT[day.weekday()]
        date_str = day.strftime('%d.%m')
        is_today = (day == today)
        marker = " 🔸" if is_today else ""

        if sch:
            if sch.is_day_off:
                line = f"• *{day_short} {date_str}*{marker}: 🏖 выходной"
            else:
                line = f"• *{day_short} {date_str}*{marker}: 📋 {sch.planned_start.strftime('%H:%M')}–{sch.planned_end.strftime('%H:%M')}"
        else:
            line = f"• *{day_short} {date_str}*{marker}: —"

        if att:
            line += f"\n   ⏰ факт: {att.actual_start.strftime('%H:%M')}–{att.actual_end.strftime('%H:%M')} (e% {int(att.efficiency*100)})"

        text += line + "\n"

    return text


def who_text():
    today = date.today()
    plans = Schedule.query.filter_by(date=today, status='approved').all()

    text = f"👥 *Кто работает сегодня ({today.strftime('%d.%m.%Y')})*\n\n"
    found = False
    for plan in plans:
        if plan.is_day_off or not plan.user or plan.user.status != 'active':
            continue
        found = True
        if plan.planned_start and plan.planned_end:
            att = Attendance.query.filter(
                Attendance.user_id == plan.user_id,
                Attendance.date == today,
                Attendance.status != 'rejected'
            ).first()
            marker = " ✅" if att else ""
            text += f"• {plan.user.full_name}: {plan.planned_start.strftime('%H:%M')}–{plan.planned_end.strftime('%H:%M')}{marker}\n"

    if not found:
        text += "Сегодня никто не работает."

    text += "\n✅ — уже отметился"
    return text


def summary_text(user, offset_months=0):
    today = date.today()
    m = today.month + offset_months
    y = today.year
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1

    import calendar as cal_mod
    start_date = date(y, m, 1)
    end_date = date(y, m, cal_mod.monthrange(y, m)[1])

    total_worked_min = 0
    total_eff_min = 0
    total_late = 0
    count_days = 0

    attendances = Attendance.query.filter(
        Attendance.user_id == user.id,
        Attendance.date >= start_date,
        Attendance.date <= end_date,
        Attendance.status != 'rejected'
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
        f"📊 *Итоги за {MONTHS_RU_FULL[m-1]} {y}*\n\n"
        f"👤 {user.full_name}\n\n"
        f"📅 Дней отработано: *{count_days}*\n"
        f"⏱ Отработано часов: *{total_worked_min / 60:.2f}*\n"
        f"📈 Средний e%: *{avg_eff:.1f}%*\n"
        f"⏰ Опоздания (часов): *{total_late / 60:.2f}*\n"
        f"✅ Итого часов: *{final_min / 60:.2f}*"
    )
    return text


def absences_text(user):
    absences = Absence.query.filter_by(user_id=user.id).order_by(Absence.date_start.desc()).limit(10).all()
    if not absences:
        return "📂 У тебя пока нет заявок на отпуск/больничный."

    text = "📂 *Твои последние заявки:*\n\n"
    for ab in absences:
        if ab.type == 'other' and ab.custom_type:
            type_ru = f"📌 {ab.custom_type}"
        else:
            type_ru = ABS_TYPE_RU.get(ab.type, ab.type)
        status = STATUS_RU.get(ab.status, ab.status)
        text += (
            f"{type_ru}\n"
            f"📅 {ab.date_start.strftime('%d.%m.%Y')} – {ab.date_end.strftime('%d.%m.%Y')}\n"
            f"📊 {status}\n"
        )
        if ab.file_path:
            text += f"📎 файл прикреплён\n"
        text += "\n"
    return text


# ---------- СТАРТ / РЕГИСТРАЦИЯ ----------
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
            return ConversationHandler.END

    await update.message.reply_text(
        "👋 Привет! Я — бот WorkTracker.\n\n"
        "Чтобы привязать Telegram к учётной записи,\n"
        "пришли свой email (тот, под которым ты входишь на сайт).",
        reply_markup=cancel_keyboard()
    )
    return ASK_EMAIL

async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        await update.message.reply_text("Отменено. Отправь /start, чтобы начать заново.")
        return ConversationHandler.END

    email = text.lower()
    chat_id = update.effective_chat.id
    tg_user = update.effective_user

    with app.app_context():
        user = User.query.filter_by(email=email).first()
        if not user:
            await update.message.reply_text(
                "❌ Email не найден.\n"
                "Проверь написание или обратись к администратору.\n\n"
                "Попробуй ещё раз или нажми «❌ Отмена»."
            )
            return ASK_EMAIL

        # Сохраняем числовой chat_id
        user.telegram_chat_id = str(chat_id)

        # Сохраняем username, если есть; иначе — имя или id
        if tg_user.username:
            user.telegram_id = f"@{tg_user.username}"
        elif not user.telegram_id:
            if tg_user.first_name:
                user.telegram_id = tg_user.first_name
            else:
                user.telegram_id = f"id{chat_id}"

        db.session.commit()

        await update.message.reply_text(
            f"✅ Отлично, {user.full_name}!\n"
            f"Аккаунт привязан.\n\n"
            f"Выбери действие в меню 👇",
            reply_markup=main_menu_keyboard()
        )
    return ConversationHandler.END

async def detach_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("Ты не привязан.")
            return
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Да, отвязать", callback_data="detach_yes"),
                InlineKeyboardButton("❌ Отмена", callback_data="detach_no"),
            ]
        ])
        await update.message.reply_text(
            "⚠️ Ты точно хочешь *отвязать* аккаунт?\n"
            "После этого уведомления приходить не будут.",
            parse_mode='Markdown',
            reply_markup=kb
        )


async def detach_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "detach_no":
        await query.edit_message_text("Отменено.")
        return

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if user:
            user.telegram_chat_id = None
            db.session.commit()
    await query.edit_message_text(
        "✅ Аккаунт отвязан.\n\n"
        "Чтобы привязать снова — отправь /start."
    )


# ---------- ПРОФИЛЬ ----------
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return
        text = profile_text(user)
    await update.message.reply_text(text, parse_mode='Markdown',
                                    reply_markup=main_menu_keyboard())


# ---------- РАСПИСАНИЕ НА НЕДЕЛЮ ----------
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    offset = context.user_data.get('week_offset', 0)

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return
        text = week_text(user, offset)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️", callback_data="week_prev"),
            InlineKeyboardButton("Текущая", callback_data="week_now"),
            InlineKeyboardButton("➡️", callback_data="week_next"),
        ]
    ])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard)


async def week_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "week_prev":
        context.user_data['week_offset'] = context.user_data.get('week_offset', 0) - 1
    elif query.data == "week_next":
        context.user_data['week_offset'] = context.user_data.get('week_offset', 0) + 1
    elif query.data == "week_now":
        context.user_data['week_offset'] = 0

    chat_id = query.message.chat_id
    offset = context.user_data.get('week_offset', 0)

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.edit_message_text("❌ Аккаунт не привязан.")
            return
        text = week_text(user, offset)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️", callback_data="week_prev"),
            InlineKeyboardButton("Текущая", callback_data="week_now"),
            InlineKeyboardButton("➡️", callback_data="week_next"),
        ]
    ])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=keyboard)


# ---------- КТО РАБОТАЕТ ----------
async def cmd_who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with app.app_context():
        text = who_text()
    await update.message.reply_text(text, parse_mode='Markdown',
                                    reply_markup=main_menu_keyboard())


# ---------- ПРЕДЛОЖИТЬ ПЛАН ----------
async def plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Сегодня", callback_data="plan_today")],
        [InlineKeyboardButton("📅 Завтра", callback_data="plan_tomorrow")],
    ])
    await update.message.reply_text(
        "✏️ *Новый план на день*\n\n"
        "Выбери дату кнопкой или введи вручную в формате *ДД.ММ.ГГГГ*\n"
        "Например: `23.09.2026`",
        parse_mode='Markdown',
        reply_markup=kb
    )
    return PLAN_DATE


async def plan_today_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d = date.today() if query.data == "plan_today" else date.today() + timedelta(days=1)
    context.user_data['plan_date'] = d
    await query.edit_message_text(
        f"📅 Дата: *{d.strftime('%d.%m.%Y')}*\n\n"
        f"🕐 Введи *время прихода* в формате *ЧЧ:ММ*\nНапример: `10:00`",
        parse_mode='Markdown'
    )
    return PLAN_START


async def plan_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        d = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат. Введи как `23.09.2026`\n"
            "Или нажми «❌ Отмена», чтобы выйти.",
            parse_mode='Markdown',
            reply_markup=cancel_keyboard()
        )
        return PLAN_DATE

    context.user_data['plan_date'] = d
    await update.message.reply_text(
        f"📅 Дата: *{d.strftime('%d.%m.%Y')}*\n\n"
        f"🕐 Введи *время прихода* в формате *ЧЧ:ММ*",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return PLAN_START


async def plan_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        start_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как `10:00`", parse_mode='Markdown',
                                        reply_markup=cancel_keyboard())
        return PLAN_START

    context.user_data['plan_start'] = start_time
    await update.message.reply_text(
        f"🕐 Приход: *{start_time.strftime('%H:%M')}*\n\n"
        f"🕕 Введи *время ухода* в формате *ЧЧ:ММ*",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return PLAN_END


async def plan_end_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        end_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как `18:00`", parse_mode='Markdown',
                                        reply_markup=cancel_keyboard())
        return PLAN_END

    start_time = context.user_data['plan_start']
    if start_time >= end_time:
        await update.message.reply_text("❌ Время ухода должно быть позже времени прихода.",
                                        reply_markup=cancel_keyboard())
        return PLAN_END

    d = context.user_data['plan_date']
    chat_id = update.effective_chat.id

    with app.app_context():
        user = get_user_by_chat(chat_id)
        existing = Schedule.query.filter_by(user_id=user.id, date=d).first()
        if existing:
            await update.message.reply_text(
                f"⚠️ На {d.strftime('%d.%m.%Y')} уже есть заявка.",
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
    today = date.today()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📅 Сегодня ({today.strftime('%d.%m')})", callback_data="fact_today")],
        [InlineKeyboardButton("📅 Вчера", callback_data="fact_yesterday")],
    ])
    await update.message.reply_text(
        f"⏰ *Отметить фактическое время*\n\n"
        f"Выбери дату кнопкой или введи вручную *ДД.ММ.ГГГГ*",
        parse_mode='Markdown',
        reply_markup=kb
    )
    return FACT_DATE


async def fact_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d = date.today() if query.data == "fact_today" else date.today() - timedelta(days=1)
    context.user_data['fact_date'] = d
    await query.edit_message_text(
        f"📅 Дата: *{d.strftime('%d.%m.%Y')}*\n\n"
        f"🕐 Введи *время прихода* в формате *ЧЧ:ММ*",
        parse_mode='Markdown'
    )
    return FACT_START


async def fact_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()

    if text == 'сегодня':
        d = date.today()
    elif text == 'вчера':
        d = date.today() - timedelta(days=1)
    else:
        try:
            d = datetime.strptime(text, '%d.%m.%Y').date()
        except ValueError:
            await update.message.reply_text(
                "❌ Введи как `23.09.2026`, либо напиши «сегодня» или «вчера».",
                parse_mode='Markdown',
                reply_markup=cancel_keyboard()
            )
            return FACT_DATE

    if d > date.today():
        await update.message.reply_text("❌ Нельзя отмечать факт на будущую дату.",
                                        reply_markup=cancel_keyboard())
        return FACT_DATE

    context.user_data['fact_date'] = d
    await update.message.reply_text(
        f"📅 Дата: *{d.strftime('%d.%m.%Y')}*\n\n"
        f"🕐 Введи *время прихода* в формате *ЧЧ:ММ*",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return FACT_START


async def fact_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        start_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как `10:00`", parse_mode='Markdown',
                                        reply_markup=cancel_keyboard())
        return FACT_START

    context.user_data['fact_start'] = start_time
    await update.message.reply_text(
        f"🕐 Приход: *{start_time.strftime('%H:%M')}*\n\n"
        f"🕕 Введи *время ухода* в формате *ЧЧ:ММ*",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return FACT_END


async def fact_end_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        end_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как `18:00`", parse_mode='Markdown',
                                        reply_markup=cancel_keyboard())
        return FACT_END

    if context.user_data['fact_start'] >= end_time:
        await update.message.reply_text("❌ Время ухода должно быть позже времени прихода.",
                                        reply_markup=cancel_keyboard())
        return FACT_END

    context.user_data['fact_end'] = end_time
    await update.message.reply_text(
        f"🕕 Уход: *{end_time.strftime('%H:%M')}*\n\n"
        f"📈 Введи *эффективность* в процентах (0-100)",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return FACT_EFF


async def fact_efficiency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        eff = float(text)
        if not (0 <= eff <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи число от 0 до 100",
                                        reply_markup=cancel_keyboard())
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
    abs_type = types.get(query.data, 'vacation')
    context.user_data['abs_type'] = abs_type

    type_ru = ABS_TYPE_RU.get(abs_type, abs_type)

    await query.edit_message_text(
        f"{type_ru}\n\n"
        f"📅 Введи *дату начала* в формате *ДД.ММ.ГГГГ*\nНапример: `25.09.2026`",
        parse_mode='Markdown'
    )
    return ABS_DATE_START


async def abs_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        d = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text("❌ Введи как `25.09.2026`", parse_mode='Markdown',
                                        reply_markup=cancel_keyboard())
        return ABS_DATE_START

    context.user_data['abs_start'] = d
    await update.message.reply_text(
        f"📅 Начало: *{d.strftime('%d.%m.%Y')}*\n\n"
        f"📅 Введи *дату окончания* в формате *ДД.ММ.ГГГГ*",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return ABS_DATE_END


async def abs_date_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        d = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text("❌ Введи как `30.09.2026`", parse_mode='Markdown',
                                        reply_markup=cancel_keyboard())
        return ABS_DATE_END

    if d < context.user_data['abs_start']:
        await update.message.reply_text("❌ Дата окончания раньше даты начала.",
                                        reply_markup=cancel_keyboard())
        return ABS_DATE_END

    context.user_data['abs_end'] = d

    if context.user_data.get('abs_type') == 'other':
        await update.message.reply_text(
            "📝 Опиши причину отсутствия\n(например: «отгул за переработку», «учёба»)",
            reply_markup=cancel_keyboard()
        )
        return ABS_CUSTOM

    await update.message.reply_text(
        "📎 Прикрепи *справку/документ*, если есть.\n\n"
        "Отправь файл (PDF или фото) или напиши «Пропустить».",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return ABS_FILE


async def abs_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) > 200:
        await update.message.reply_text("❌ Слишком длинно. Максимум 200 символов.",
                                        reply_markup=cancel_keyboard())
        return ABS_CUSTOM

    context.user_data['abs_custom'] = text
    await update.message.reply_text(
        "📎 Прикрепи *справку/документ*, если есть.\n\n"
        "Отправь файл (PDF или фото) или напиши «Пропустить».",
        parse_mode='Markdown',
        reply_markup=cancel_keyboard()
    )
    return ABS_FILE


async def abs_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message = update.message
    file_path = None

    if message.document:
        file = await message.document.get_file()
        ext = os.path.splitext(message.document.file_name)[1] or '.dat'
        filename = f"abs_{chat_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
    elif message.photo:
        file = await message.photo[-1].get_file()
        filename = f"abs_{chat_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
    else:
        text = message.text.strip().lower() if message.text else ''
        if text == 'пропустить':
            return await abs_save(update, context, file_path=None)
        await message.reply_text("❌ Отправь файл или напиши «Пропустить».",
                                 reply_markup=cancel_keyboard())
        return ABS_FILE

    upload_folder = os.path.join(app.root_path, 'static', 'uploads')
    os.makedirs(upload_folder, exist_ok=True)
    full_path = os.path.join(upload_folder, filename)
    await file.download_to_drive(full_path)
    file_path = f'uploads/{filename}'
    return await abs_save(update, context, file_path=file_path)


async def abs_save(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path=None):
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
            status='pending',
            file_path=file_path
        )
        db.session.add(absence)
        db.session.commit()

        admins = User.query.filter_by(role='admin').all()
        type_ru = {'vacation': 'отпуск', 'sick': 'больничный', 'other': custom or 'другое'}.get(abs_type, abs_type)
        for admin in admins:
            if admin.telegram_chat_id:
                notif = Notification(
                    user_id=admin.id,
                    chat_id=admin.telegram_chat_id,
                    message=f"📩 {user.full_name} — заявка на {type_ru}: {d_start.strftime('%d.%m.%Y')} – {d_end.strftime('%d.%m.%Y')}"
                )
                db.session.add(notif)
        db.session.commit()

    type_ru = {'vacation': '🏖 Отпуск', 'sick': '🤒 Больничный', 'other': f'📌 {custom}'}.get(abs_type, abs_type)
    text = (
        f"✅ *Заявка отправлена!*\n\n"
        f"📌 {type_ru}\n"
        f"📅 {d_start.strftime('%d.%m.%Y')} – {d_end.strftime('%d.%m.%Y')}"
    )
    if file_path:
        text += f"\n📎 Файл прикреплён"
    text += "\n\nОжидай подтверждения администратора."

    await update.message.reply_text(text, parse_mode='Markdown',
                                    reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# ---------- МОИ ЗАЯВКИ ----------
async def cmd_my_absences(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return
        text = absences_text(user)
    await update.message.reply_text(text, parse_mode='Markdown',
                                    reply_markup=main_menu_keyboard())


# ---------- ИТОГИ ----------
async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    offset = context.user_data.get('summary_offset', 0)

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return
        text = summary_text(user, offset)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Прошлый", callback_data="sum_prev"),
            InlineKeyboardButton("Текущий", callback_data="sum_now"),
            InlineKeyboardButton("Следующий ➡️", callback_data="sum_next"),
        ]
    ])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard)


async def summary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "sum_prev":
        context.user_data['summary_offset'] = context.user_data.get('summary_offset', 0) - 1
    elif query.data == "sum_next":
        context.user_data['summary_offset'] = context.user_data.get('summary_offset', 0) + 1
    elif query.data == "sum_now":
        context.user_data['summary_offset'] = 0

    chat_id = query.message.chat_id
    offset = context.user_data.get('summary_offset', 0)

    with app.app_context():
        user = get_user_by_chat(chat_id)
        text = summary_text(user, offset)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Прошлый", callback_data="sum_prev"),
            InlineKeyboardButton("Текущий", callback_data="sum_now"),
            InlineKeyboardButton("Следующий ➡️", callback_data="sum_next"),
        ]
    ])
    await query.edit_message_text(text, parse_mode='Markdown', reply_markup=keyboard)


# ---------- ПОМОЩЬ ----------
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ *Помощь по боту WorkTracker*\n\n"
        "Нажми на раздел ниже, чтобы перейти к нему, "
        "или используй кнопки внизу экрана 👇"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Мой профиль", callback_data="help_profile"),
         InlineKeyboardButton("📋 Расписание недели", callback_data="help_week")],
        [InlineKeyboardButton("👥 Кто работает", callback_data="help_who"),
         InlineKeyboardButton("📊 Итоги месяца", callback_data="help_summary")],
        [InlineKeyboardButton("📂 Мои заявки", callback_data="help_absences"),
         InlineKeyboardButton("🏖 Заявка на отпуск", callback_data="help_absence")],
        [InlineKeyboardButton("✏️ Предложить план", callback_data="help_plan"),
         InlineKeyboardButton("⏰ Отметить факт", callback_data="help_fact")],
    ])
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=kb)


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return

        if data == "help_profile":
            await query.message.reply_text(profile_text(user), parse_mode='Markdown')
        elif data == "help_week":
            text = week_text(user, context.user_data.get('week_offset', 0))
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️", callback_data="week_prev"),
                    InlineKeyboardButton("Текущая", callback_data="week_now"),
                    InlineKeyboardButton("➡️", callback_data="week_next"),
                ]
            ])
            await query.message.reply_text(text, parse_mode='Markdown', reply_markup=kb)
        elif data == "help_who":
            await query.message.reply_text(who_text(), parse_mode='Markdown')
        elif data == "help_summary":
            text = summary_text(user, context.user_data.get('summary_offset', 0))
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️ Прошлый", callback_data="sum_prev"),
                    InlineKeyboardButton("Текущий", callback_data="sum_now"),
                    InlineKeyboardButton("Следующий ➡️", callback_data="sum_next"),
                ]
            ])
            await query.message.reply_text(text, parse_mode='Markdown', reply_markup=kb)
        elif data == "help_absences":
            await query.message.reply_text(absences_text(user), parse_mode='Markdown')
        elif data == "help_absence":
            await query.message.reply_text(
                "🏖 Чтобы подать заявку на отпуск/больничный, нажми кнопку "
                "«🏖 Заявка на отпуск» внизу 👇"
            )
        elif data == "help_plan":
            await query.message.reply_text(
                "✏️ Чтобы предложить план, нажми кнопку "
                "«✏️ Предложить план» внизу 👇"
            )
        elif data == "help_fact":
            await query.message.reply_text(
                "⏰ Чтобы отметить факт, нажми кнопку "
                "«⏰ Отметить факт» внизу 👇"
            )


# ---------- УВЕДОМЛЕНИЯ ----------
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


async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершает диалог при нажатии «❌ Отмена»."""
    await update.message.reply_text(
        "Отменено. Ты в главном меню 👇",
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Надёжный роутер по тексту кнопок через подстроку — не зависит от эмодзи."""
    text = update.message.text or ''

    # Регистрация: если не привязан, ждём email
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
    if not user:
        await update.message.reply_text(
            "Сначала отправь /start и привяжи аккаунт.",
            reply_markup=cancel_keyboard()
        )
        return

    # Меню
    if 'Мой профиль' in text:
        await cmd_profile(update, context)
    elif 'Расписание недели' in text:
        await cmd_week(update, context)
    elif 'Предложить план' in text:
        await plan_start(update, context)
        # Это запустит новую conversation при следующем сообщении
    elif 'Отметить факт' in text:
        await fact_start(update, context)
    elif 'Заявка на отпуск' in text:
        await abs_start(update, context)
    elif 'Мои заявки' in text:
        await cmd_my_absences(update, context)
    elif 'Кто работает' in text:
        await cmd_who(update, context)
    elif 'Итоги месяца' in text:
        await cmd_summary(update, context)
    elif 'Помощь' in text:
        await cmd_help(update, context)
    elif 'Отвязать аккаунт' in text:
        await detach_start(update, context)
    else:
        await update.message.reply_text(
            "🤔 Не понимаю команду. Используй кнопки внизу 👇",
            reply_markup=main_menu_keyboard()
        )

# ---------- ЗАПУСК ----------
def main():
    if not Config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не задан в .env")
        return

    application = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    # ===== Регистрация =====
    reg_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            ASK_EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_email),
            ],
        },
        fallbacks=[
            CommandHandler('start', start),
        ],
    )
    application.add_handler(reg_handler, group=-1)

    # ===== План =====
    plan_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('Предложить план'), plan_start)],
        states={
            PLAN_DATE: [
                CallbackQueryHandler(plan_today_callback, pattern='^plan_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, plan_date),
            ],
            PLAN_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_start_time)],
            PLAN_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, plan_end_time)],
        },
        fallbacks=[
            CommandHandler('start', start),
            MessageHandler(filters.Text([CANCEL_TEXT]), end_conversation),
        ],
    )
    application.add_handler(plan_handler, group=-1)

    # ===== Факт =====
    fact_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('Отметить факт'), fact_start)],
        states={
            FACT_DATE: [
                CallbackQueryHandler(fact_date_callback, pattern='^fact_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fact_date),
            ],
            FACT_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, fact_start_time)],
            FACT_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, fact_end_time)],
            FACT_EFF: [MessageHandler(filters.TEXT & ~filters.COMMAND, fact_efficiency)],
        },
        fallbacks=[
            CommandHandler('start', start),
            MessageHandler(filters.Text([CANCEL_TEXT]), end_conversation),
        ],
    )
    application.add_handler(fact_handler, group=-1)

    # ===== Отпуск =====
    abs_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('Заявка на отпуск'), abs_start)],
        states={
            ABS_TYPE: [CallbackQueryHandler(abs_type_callback, pattern='^abs_')],
            ABS_DATE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, abs_date_start)],
            ABS_DATE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, abs_date_end)],
            ABS_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, abs_custom)],
            ABS_FILE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, abs_receive_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, abs_receive_file),
            ],
        },
        fallbacks=[
            CommandHandler('start', start),
            MessageHandler(filters.Text([CANCEL_TEXT]), end_conversation),
        ],
    )
    application.add_handler(abs_handler, group=-1)

    # ===== Роутер по тексту кнопок (вне диалогов) =====
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # ===== Инлайн-кнопки =====
    application.add_handler(CallbackQueryHandler(week_callback, pattern='^week_'))
    application.add_handler(CallbackQueryHandler(summary_callback, pattern='^sum_'))
    application.add_handler(CallbackQueryHandler(help_callback, pattern='^help_'))
    application.add_handler(CallbackQueryHandler(detach_callback, pattern='^detach_'))

    # ===== Уведомления =====
    application.job_queue.run_repeating(check_notifications, interval=5, first=5)

    print("🤖 Бот запущен. Нажми Ctrl+C для остановки.")
    application.run_polling()


if __name__ == '__main__':
    main()