import logging
import os
from datetime import datetime, date, time as dtime, timedelta
from io import BytesIO

from sqlalchemy import func
from PIL import Image, ImageDraw, ImageFont
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
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

DAYS_RU_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
DAYS_RU_FULL = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
MONTHS_RU_FULL = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']

ROLE_RU = {'employee': 'Сотрудник', 'admin': 'Администратор'}
ABS_TYPE_RU = {'vacation': '🏖 Отпуск', 'sick': '🤒 Больничный', 'other': '📌 Другое'}
ABS_TYPE_RU_SHORT = {'vacation': 'отпуск', 'sick': 'больничный', 'other': 'другое'}
STATUS_RU = {
    'pending': '⏳ ожидает',
    'approved': '✅ подтверждено',
    'rejected': '❌ отклонено',
    'confirmed': '✅ подтверждено',
    'pending_deletion': '🗑 ожидает удаления',
}

MENU_BUTTONS = [
    'Мой профиль', 'Расписание недели',
    'Плановое время', 'Фактическое время',
    'Расписание на неделю', 'Расписание всех',
    'Заявка на отпуск', 'Мои заявки',
    'Кто работает', 'Итоги месяца',
    'Помощь', 'Отвязать аккаунт',
]

CANCEL_TEXT = '❌ Отмена'


# ================== УТИЛИТЫ ==================
def get_user_by_chat(chat_id):
    return User.query.filter_by(telegram_chat_id=str(chat_id)).first()


def find_user_by_email(email):
    return User.query.filter(func.lower(User.email) == email.strip().lower()).first()


def esc(text):
    """Экранирует спецсимволы для HTML в Telegram."""
    if text is None:
        return ''
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def main_menu_keyboard():
    kb = [
        [KeyboardButton('👤 Мой профиль'), KeyboardButton('📋 Расписание недели')],
        [KeyboardButton('🕐 Плановое время'), KeyboardButton('⏰ Фактическое время')],
        [KeyboardButton('📅 Расписание на неделю'), KeyboardButton('👥 Расписание всех')],
        [KeyboardButton('🏖 Заявка на отпуск'), KeyboardButton('📂 Мои заявки')],
        [KeyboardButton('👥 Кто работает'), KeyboardButton('📊 Итоги месяца')],
        [KeyboardButton('❓ Помощь'), KeyboardButton('🔓 Отвязать аккаунт')],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def cancel_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton(CANCEL_TEXT)]], resize_keyboard=True)


def clear_state(context):
    keys = [
        'state', 'plan_date', 'plan_start', 'plan_end',
        'fact_date', 'fact_start', 'fact_end', 'fact_eff',
        'abs_type', 'abs_start', 'abs_end', 'abs_custom',
        'week_plan_idx', 'week_plan_data', 'week_start', 'week_temp_start',
        'pending_plan',
    ]
    for k in keys:
        context.user_data.pop(k, None)


def month_offset_range(offset_months):
    today = date.today()
    m = today.month + offset_months
    y = today.year
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return y, m


def get_fonts():
    font_paths = [
        '/System/Library/Fonts/Helvetica.ttc',
        '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/Library/Fonts/Arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for path in font_paths:
        try:
            return {
                'title': ImageFont.truetype(path, 32),
                'header': ImageFont.truetype(path, 22),
                'cell': ImageFont.truetype(path, 18),
                'small': ImageFont.truetype(path, 16),
            }
        except Exception:
            continue
    default = ImageFont.load_default()
    return {'title': default, 'header': default, 'cell': default, 'small': default}


# ================== ТЕКСТЫ ==================
def week_text(user, offset):
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    end_of_week = start_of_week + timedelta(days=6)

    text = f"📋 <b>Расписание на неделю</b>\n"
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
        marker = " 🔸" if day == today else ""

        if sch:
            if sch.is_day_off:
                line = f"• <b>{day_short} {date_str}</b>{marker}: 🏖 выходной"
            else:
                line = f"• <b>{day_short} {date_str}</b>{marker}: 📋 {sch.planned_start.strftime('%H:%M')}–{sch.planned_end.strftime('%H:%M')}"
                if sch.plan_text:
                    line += f"\n   📝 {esc(sch.plan_text[:80])}"
        else:
            line = f"• <b>{day_short} {date_str}</b>{marker}: —"

        if att:
            line += f"\n   ⏰ факт: {att.actual_start.strftime('%H:%M')}–{att.actual_end.strftime('%H:%M')} (e% {int(att.efficiency*100)})"

        text += line + "\n"

    return text


def who_text():
    today = date.today()
    plans = Schedule.query.filter_by(date=today, status='approved').all()

    text = f"👥 <b>Кто работает сегодня ({today.strftime('%d.%m.%Y')})</b>\n\n"
    found = False
    for plan in plans:
        if plan.is_day_off or not plan.user or plan.user.status != 'active':
            continue
        found = True
        if plan.planned_start and plan.planned_end:
            text += f"• {esc(plan.user.full_name)}: {plan.planned_start.strftime('%H:%M')}–{plan.planned_end.strftime('%H:%M')}\n"
            if plan.plan_text:
                text += f"   📝 {esc(plan.plan_text[:80])}\n"

    if not found:
        text += "Сегодня никто не работает."

    return text


def summary_text(user, offset_months=0):
    y, m = month_offset_range(offset_months)
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

    return (
        f"📊 <b>Мои итоги за {MONTHS_RU_FULL[m-1]} {y}</b>\n\n"
        f"👤 {esc(user.full_name)}\n\n"
        f"📅 Дней отработано: <b>{count_days}</b>\n"
        f"⏱ Отработано часов: <b>{total_worked_min / 60:.2f}</b>\n"
        f"📈 Средний e%: <b>{avg_eff:.1f}%</b>\n"
        f"⏰ Опоздания (часов): <b>{total_late / 60:.2f}</b>\n"
        f"✅ Итого часов: <b>{final_min / 60:.2f}</b>"
    )


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
    text = f"👤 <b>{esc(user.full_name)}</b>\n"
    text += f"📧 {esc(user.email)}\n"
    text += f"🎭 Роль: {esc(role_ru)}\n"
    if user.telegram_id:
        text += f"💬 {esc(user.telegram_id)}\n"
    text += "\n"

    if absence:
        abs_ru = ABS_TYPE_RU.get(absence.type, absence.type)
        if absence.type == 'other' and absence.custom_type:
            abs_ru = f"📌 {esc(absence.custom_type)}"
        text += f"{abs_ru} до {absence.date_end.strftime('%d.%m.%Y')}\n\n"

    text += f"<b>Сегодня — {DAYS_RU_FULL[today.weekday()]}, {today.strftime('%d.%m.%Y')}:</b>\n"

    if schedule and schedule.status != 'rejected':
        if schedule.is_day_off:
            text += "🏖 План: выходной\n"
        else:
            text += f"📋 План: {schedule.planned_start.strftime('%H:%M')} – {schedule.planned_end.strftime('%H:%M')}\n"
            if schedule.plan_text:
                text += f"📝 Задачи: {esc(schedule.plan_text[:200])}\n"
    else:
        text += "📋 План: не указан\n"

    if attendance:
        text += f"⏰ Факт: {attendance.actual_start.strftime('%H:%M')} – {attendance.actual_end.strftime('%H:%M')}\n"
        text += f"📈 Эффективность: {int(attendance.efficiency * 100)}%\n"
        text += f"🎯 Статус: {esc(STATUS_RU.get(attendance.status, attendance.status))}"

    return text


def absences_text(user):
    absences = Absence.query.filter_by(user_id=user.id).order_by(Absence.date_start.desc()).limit(10).all()
    if not absences:
        return "📂 У тебя пока нет заявок на отпуск/больничный."

    text = "📂 <b>Твои последние заявки:</b>\n\n"
    for ab in absences:
        if ab.type == 'other' and ab.custom_type:
            type_ru = f"📌 {esc(ab.custom_type)}"
        else:
            type_ru = ABS_TYPE_RU.get(ab.type, ab.type)
        status = STATUS_RU.get(ab.status, ab.status)
        text += (
            f"{type_ru}\n"
            f"📅 {ab.date_start.strftime('%d.%m.%Y')} – {ab.date_end.strftime('%d.%m.%Y')}\n"
            f"📊 {esc(status)}\n"
        )
        if ab.file_path:
            text += f"📎 файл прикреплён\n"
        text += "\n"
    return text


# ================== КАРТИНКИ ==================
def generate_all_summary_image(offset_months=0):
    y, m = month_offset_range(offset_months)
    import calendar as cal_mod
    start_date = date(y, m, 1)
    end_date = date(y, m, cal_mod.monthrange(y, m)[1])

    employees = User.query.filter_by(role='employee', status='active').all()
    rows = []
    grand_worked = 0
    grand_eff = 0
    grand_late = 0

    for emp in employees:
        wm = em = late = 0
        atts = Attendance.query.filter(
            Attendance.user_id == emp.id,
            Attendance.date >= start_date,
            Attendance.date <= end_date,
            Attendance.status == 'confirmed'
        ).all()
        for att in atts:
            if att.actual_start and att.actual_end:
                w = calculate_worked_hours(att.actual_start, att.actual_end)
                wmin = int(w.total_seconds() // 60)
                wm += wmin
                em += int(wmin * att.efficiency)
                if att.early_start and att.early_start > 0:
                    late += att.early_start
        if wm > 0:
            rows.append((emp.full_name, wm / 60, em / wm * 100, late / 60, (em - late) / 60))
            grand_worked += wm
            grand_eff += em
            grand_late += late

    grand_avg = (grand_eff / grand_worked * 100) if grand_worked > 0 else 0
    grand_final = grand_eff - grand_late

    img_width = 1000
    row_height = 55
    header_height = 150
    table_header_height = 60
    footer_height = 100
    img_height = header_height + table_header_height + row_height * max(len(rows), 1) + footer_height

    img = Image.new('RGB', (img_width, img_height), '#f8fafc')
    draw = ImageDraw.Draw(img)
    f = get_fonts()

    draw.rectangle([0, 0, img_width, header_height], fill='#4f46e5')
    draw.text((30, 30), 'ИТОГИ МЕСЯЦА', fill='white', font=f['title'])
    draw.text((30, 85), f'{MONTHS_RU_FULL[m-1]} {y}', fill='#e0e7ff', font=f['header'])

    y0 = header_height
    draw.rectangle([0, y0, img_width, y0 + table_header_height], fill='#e5e7eb')
    col_x = [30, 420, 570, 720, 870]
    headers = ['Сотрудник', 'Отработано', 'Ср. e%', 'Опоздания', 'Итого']
    for i, h in enumerate(headers):
        draw.text((col_x[i], y0 + 18), h, fill='#1f2937', font=f['header'])

    for i, (name, worked, avg, late, final) in enumerate(rows):
        y1 = y0 + table_header_height + i * row_height
        bg = '#ffffff' if i % 2 == 0 else '#f1f5f9'
        draw.rectangle([0, y1, img_width, y1 + row_height], fill=bg)
        name_short = name if len(name) <= 30 else name[:27] + '...'
        draw.text((col_x[0], y1 + 16), name_short, fill='#1f2937', font=f['cell'])
        draw.text((col_x[1], y1 + 16), f'{worked:.2f} ч', fill='#4f46e5', font=f['cell'])
        draw.text((col_x[2], y1 + 16), f'{avg:.1f}%', fill='#06b6d4', font=f['cell'])
        draw.text((col_x[3], y1 + 16), f'{late:.2f} ч', fill='#f59e0b', font=f['cell'])
        draw.text((col_x[4], y1 + 16), f'{final:.2f} ч', fill='#10b981', font=f['cell'])

    y_footer = y0 + table_header_height + row_height * max(len(rows), 1)
    draw.rectangle([0, y_footer, img_width, y_footer + row_height], fill='#1f2937')
    draw.text((30, y_footer + 16),
              f'ВСЕГО: {grand_worked/60:.2f} ч  |  ср. e% {grand_avg:.1f}  |  опоздания {grand_late/60:.2f} ч  |  итог {grand_final/60:.2f} ч',
              fill='white', font=f['small'])

    draw.text((30, img_height - 40),
              f'Сформировано: {datetime.now().strftime("%d.%m.%Y %H:%M")}',
              fill='#6b7280', font=f['small'])

    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


def generate_all_week_image(offset=0):
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    end_of_week = start_of_week + timedelta(days=6)

    employees = User.query.filter_by(role='employee', status='active').all()

    rows = []
    for emp in employees:
        emp_row = []
        has_any = False
        for i in range(7):
            day = start_of_week + timedelta(days=i)
            sch = Schedule.query.filter_by(user_id=emp.id, date=day).first()
            if sch and sch.status != 'rejected':
                if sch.is_day_off:
                    emp_row.append('Вых')
                    has_any = True
                else:
                    emp_row.append(f"{sch.planned_start.strftime('%H:%M')}–{sch.planned_end.strftime('%H:%M')}")
                    has_any = True
            else:
                emp_row.append('—')
        if has_any:
            rows.append((emp.full_name, emp_row))

    img_width = 1400
    header_height = 150
    table_header_height = 90
    row_height = 55
    footer_height = 80
    img_height = header_height + table_header_height + row_height * max(len(rows), 1) + footer_height

    img = Image.new('RGB', (img_width, img_height), '#f8fafc')
    draw = ImageDraw.Draw(img)
    f = get_fonts()

    draw.rectangle([0, 0, img_width, header_height], fill='#4f46e5')
    draw.text((30, 30), 'РАСПИСАНИЕ НА НЕДЕЛЮ', fill='white', font=f['title'])
    draw.text((30, 85), f"{start_of_week.strftime('%d.%m')} – {end_of_week.strftime('%d.%m.%Y')}",
              fill='#e0e7ff', font=f['header'])

    y0 = header_height
    draw.rectangle([0, y0, img_width, y0 + table_header_height], fill='#e5e7eb')

    name_col_width = 230
    day_col_width = (img_width - name_col_width - 40) / 7

    draw.text((30, y0 + 30), 'Сотрудник', fill='#1f2937', font=f['header'])

    for i in range(7):
        day_x = name_col_width + 30 + int(i * day_col_width)
        day = start_of_week + timedelta(days=i)
        draw.text((day_x + 25, y0 + 15), DAYS_RU_SHORT[i], fill='#1f2937', font=f['header'])
        draw.text((day_x + 15, y0 + 50), day.strftime('%d.%m'), fill='#6b7280', font=f['small'])

    for i, (name, emp_row) in enumerate(rows):
        y1 = y0 + table_header_height + i * row_height
        bg = '#ffffff' if i % 2 == 0 else '#f1f5f9'
        draw.rectangle([0, y1, img_width, y1 + row_height], fill=bg)

        name_short = name if len(name) <= 22 else name[:20] + '...'
        draw.text((30, y1 + 16), name_short, fill='#1f2937', font=f['cell'])

        for j, cell in enumerate(emp_row):
            cell_x = name_col_width + 30 + int(j * day_col_width)
            if cell == 'Вых':
                draw.text((cell_x + 10, y1 + 16), 'Вых', fill='#6b7280', font=f['cell'])
            elif cell == '—':
                draw.text((cell_x + 10, y1 + 16), '—', fill='#9ca3af', font=f['cell'])
            else:
                draw.text((cell_x + 5, y1 + 16), cell, fill='#4f46e5', font=f['cell'])

    if not rows:
        y_empty = y0 + table_header_height + 30
        draw.text((30, y_empty), 'Пока никто не составил расписание на эту неделю.',
                  fill='#6b7280', font=f['header'])

    draw.text((30, img_height - 40),
              f'Сформировано: {datetime.now().strftime("%d.%m.%Y %H:%M")}',
              fill='#6b7280', font=f['small'])

    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


# ================== СТАРТ / РЕГИСТРАЦИЯ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if user:
            await update.message.reply_text(
                f"👋 С возвращением, {user.full_name}!\n\nВыбери действие 👇",
                reply_markup=main_menu_keyboard()
            )
            return

    context.user_data['state'] = 'ask_email'
    await update.message.reply_text(
        "👋 Привет! Я — бот WorkTracker.\n\n"
        "Чтобы привязать Telegram к учётной записи,\n"
        "пришли свой email (тот, под которым ты входишь на сайт).",
        reply_markup=cancel_keyboard()
    )


async def receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text(
            "Отменено. Отправь /start, чтобы начать заново.",
            reply_markup=main_menu_keyboard()
        )
        return

    chat_id = update.effective_chat.id
    tg_user = update.effective_user

    with app.app_context():
        current = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
        if current:
            clear_state(context)
            await update.message.reply_text(
                f"👋 С возвращением, {current.full_name}!\n\nВыбери действие 👇",
                reply_markup=main_menu_keyboard()
            )
            return

        user = find_user_by_email(text)
        if not user:
            await update.message.reply_text(
                "❌ Email не найден.\nПроверь написание или обратись к администратору."
            )
            return

        if user.telegram_chat_id and user.telegram_chat_id != str(chat_id):
            await update.message.reply_text(
                "⚠️ Этот email уже привязан к другому Telegram-аккаунту.\n\n"
                "Обратись к администратору, если это ошибка."
            )
            return

        user.telegram_chat_id = str(chat_id)
        if tg_user.username:
            user.telegram_id = f"@{tg_user.username}"
        elif not user.telegram_id:
            user.telegram_id = tg_user.first_name or f"id{chat_id}"

        db.session.commit()

    clear_state(context)
    await update.message.reply_text(
        f"✅ Отлично, {user.full_name}!\nАккаунт успешно привязан.\n\nВыбери действие 👇",
        reply_markup=main_menu_keyboard()
    )


# ================== ПРОФИЛЬ / НЕДЕЛЯ / КТО ==================
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return
        text = profile_text(user)
    await update.message.reply_text(text, parse_mode='HTML',
                                    reply_markup=main_menu_keyboard())


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    offset = context.user_data.get('week_offset', 0)
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return
        text = week_text(user, offset)

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️", callback_data="week_prev"),
            InlineKeyboardButton("Текущая", callback_data="week_now"),
            InlineKeyboardButton("➡️", callback_data="week_next"),
        ]
    ])
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=kb)


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

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️", callback_data="week_prev"),
            InlineKeyboardButton("Текущая", callback_data="week_now"),
            InlineKeyboardButton("➡️", callback_data="week_next"),
        ]
    ])
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)


async def cmd_all_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    offset = context.user_data.get('all_week_offset', 0)
    with app.app_context():
        buf = generate_all_week_image(offset)

    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)

    await update.message.reply_photo(
        photo=buf,
        caption=f"👥 <b>Расписание всех сотрудников</b>\nНеделя с {start_of_week.strftime('%d.%m.%Y')}",
        parse_mode='HTML',
        reply_markup=main_menu_keyboard()
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️", callback_data="allweek_prev"),
            InlineKeyboardButton("Текущая", callback_data="allweek_now"),
            InlineKeyboardButton("➡️", callback_data="allweek_next"),
        ]
    ])
    await update.message.reply_text("Навигация по неделям:", reply_markup=kb)


async def all_week_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "allweek_prev":
        context.user_data['all_week_offset'] = context.user_data.get('all_week_offset', 0) - 1
    elif query.data == "allweek_next":
        context.user_data['all_week_offset'] = context.user_data.get('all_week_offset', 0) + 1
    elif query.data == "allweek_now":
        context.user_data['all_week_offset'] = 0

    offset = context.user_data.get('all_week_offset', 0)
    with app.app_context():
        buf = generate_all_week_image(offset)

    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)

    await query.message.reply_photo(
        photo=buf,
        caption=f"👥 <b>Расписание всех сотрудников</b>\nНеделя с {start_of_week.strftime('%d.%m.%Y')}",
        parse_mode='HTML'
    )


async def cmd_who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with app.app_context():
        text = who_text()
    await update.message.reply_text(text, parse_mode='HTML',
                                    reply_markup=main_menu_keyboard())


async def cmd_my_absences(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return
        text = absences_text(user)
    await update.message.reply_text(text, parse_mode='HTML',
                                    reply_markup=main_menu_keyboard())


# ================== ИТОГИ ==================
async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Мои итоги", callback_data="sum_my")],
        [InlineKeyboardButton("👥 Итоги всех (картинкой)", callback_data="sum_all")],
    ])
    await update.message.reply_text(
        "📊 <b>Итоги месяца</b>\n\nЧто показать?",
        parse_mode='HTML',
        reply_markup=kb
    )


async def summary_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    context.user_data['summary_offset'] = 0

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.edit_message_text("❌ Аккаунт не привязан.")
            return

        if query.data == "sum_my":
            text = summary_text(user, 0)
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️ Прошлый", callback_data="sum_prev"),
                    InlineKeyboardButton("Текущий", callback_data="sum_now"),
                    InlineKeyboardButton("Следующий ➡️", callback_data="sum_next"),
                ],
                [InlineKeyboardButton("👥 Показать всех", callback_data="sum_all")],
            ])
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        else:
            await query.edit_message_text("🖼 Формирую картинку с итогами всех...")
            buf = generate_all_summary_image(0)
            await query.message.reply_photo(
                photo=buf,
                caption="📊 <b>Итоги всех сотрудников</b>",
                parse_mode='HTML'
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️ Прошлый", callback_data="allsum_prev"),
                    InlineKeyboardButton("Текущий", callback_data="allsum_now"),
                    InlineKeyboardButton("Следующий ➡️", callback_data="allsum_next"),
                ],
                [InlineKeyboardButton("👤 Только мои", callback_data="sum_my")],
            ])
            await query.message.reply_text("Выбери действие:", reply_markup=kb)


async def summary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data in ("sum_prev", "allsum_prev"):
        context.user_data['summary_offset'] = context.user_data.get('summary_offset', 0) - 1
    elif data in ("sum_next", "allsum_next"):
        context.user_data['summary_offset'] = context.user_data.get('summary_offset', 0) + 1
    elif data in ("sum_now", "allsum_now"):
        context.user_data['summary_offset'] = 0

    offset = context.user_data.get('summary_offset', 0)

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            return

        if data.startswith("sum_"):
            text = summary_text(user, offset)
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️ Прошлый", callback_data="sum_prev"),
                    InlineKeyboardButton("Текущий", callback_data="sum_now"),
                    InlineKeyboardButton("Следующий ➡️", callback_data="sum_next"),
                ],
                [InlineKeyboardButton("👥 Показать всех", callback_data="sum_all")],
            ])
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        else:
            await query.edit_message_text("🖼 Формирую картинку...")
            buf = generate_all_summary_image(offset)
            await query.message.reply_photo(
                photo=buf,
                caption="📊 <b>Итоги всех сотрудников</b>",
                parse_mode='HTML'
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️ Прошлый", callback_data="allsum_prev"),
                    InlineKeyboardButton("Текущий", callback_data="allsum_now"),
                    InlineKeyboardButton("Следующий ➡️", callback_data="allsum_next"),
                ],
                [InlineKeyboardButton("👤 Только мои", callback_data="sum_my")],
            ])
            await query.message.reply_text("Выбери действие:", reply_markup=kb)


# ================== ПОМОЩЬ / ОТВЯЗКА ==================
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>Помощь по боту WorkTracker</b>\n\n"
        "Используй кнопки внизу экрана:\n\n"
        "🕐 <b>Плановое время</b> — план на конкретный день или всю неделю\n"
        "⏰ <b>Фактическое время</b> — отметить, когда реально пришёл/ушёл\n"
        "👥 <b>Расписание всех</b> — график всех сотрудников картинкой\n"
        "📊 <b>Итоги месяца</b> — своя статистика или картинкой для всех\n\n"
        "В групповом чате можно писать:\n"
        "<code>#план 25.09.2026 10:00 18:00</code>\n"
        "<code>#факт 25.09.2026 10:15 18:05 90</code>\n"
        "<code>#отпуск 25.09.2026 30.09.2026 sick</code>"
    )
    await update.message.reply_text(text, parse_mode='HTML',
                                    reply_markup=main_menu_keyboard())


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
        "⚠️ Отвязать аккаунт? Telegram ID будет удалён с сайта.",
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
            user.telegram_id = None
            db.session.commit()

    clear_state(context)

    await query.edit_message_text(
        "✅ Аккаунт отвязан.\n\nTelegram ID удалён из профиля.\nЧтобы привязать снова — /start."
    )


# ================== ПЛАНИРОВАНИЕ НА ВСЮ НЕДЕЛЮ ==================
async def week_plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    context.user_data['state'] = 'week_choose'

    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    next_monday = this_monday + timedelta(days=7)
    week_after = this_monday + timedelta(days=14)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Текущая ({this_monday.strftime('%d.%m')}–{(this_monday+timedelta(days=6)).strftime('%d.%m')})",
                              callback_data="week_pick_0")],
        [InlineKeyboardButton(f"Следующая ({next_monday.strftime('%d.%m')}–{(next_monday+timedelta(days=6)).strftime('%d.%m')})",
                              callback_data="week_pick_1")],
        [InlineKeyboardButton(f"Через неделю ({week_after.strftime('%d.%m')}–{(week_after+timedelta(days=6)).strftime('%d.%m')})",
                              callback_data="week_pick_2")],
        [InlineKeyboardButton("❌ Отмена", callback_data="week_cancel")],
    ])

    text = "📅 <b>На какую неделю составить расписание?</b>"
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode='HTML', reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode='HTML', reply_markup=kb)


async def week_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "week_cancel":
        clear_state(context)
        await query.edit_message_text("❌ Отменено.")
        await query.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())
        return

    if data.startswith("week_pick_"):
        weeks_ahead = int(data.split('_')[-1])
        today = date.today()
        this_monday = today - timedelta(days=today.weekday())
        week_start = this_monday + timedelta(days=7 * weeks_ahead)

        context.user_data['week_start'] = week_start
        context.user_data['week_plan_idx'] = 0
        context.user_data['week_plan_data'] = {}
        context.user_data['state'] = 'week_plan'

        await query.edit_message_text(f"📅 Начинаем с {week_start.strftime('%d.%m.%Y')}")
        await ask_week_day(update, context)


async def ask_week_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data.get('week_plan_idx', 0)
    week_start = context.user_data['week_start']

    if idx > 6:
        return await save_week_plan(update, context)

    day = week_start + timedelta(days=idx)
    day_name = DAYS_RU_FULL[idx]
    date_str = day.strftime('%d.%m.%Y')

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Полный день 10:00–18:00", callback_data="week_full_day")],
        [InlineKeyboardButton("📅 Своё время", callback_data="week_custom")],
        [InlineKeyboardButton("🏖 Выходной", callback_data="week_dayoff")],
        [InlineKeyboardButton("⏭ Пропустить день", callback_data="week_skip")],
        [InlineKeyboardButton("❌ Отменить всё", callback_data="week_cancel")],
    ])

    text = (
        f"📅 <b>Планирование недели</b>\n\n"
        f"День {idx + 1} из 7: <b>{day_name}, {date_str}</b>\n\n"
        f"Что делаешь в этот день?"
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode='HTML', reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode='HTML', reply_markup=kb)


async def week_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    idx = context.user_data.get('week_plan_idx', 0)

    if data == "week_cancel":
        clear_state(context)
        await query.edit_message_text("❌ Отменено.")
        await query.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())
        return

    if data == "week_skip":
        context.user_data['week_plan_idx'] = idx + 1
        await query.edit_message_text(f"⏭ {DAYS_RU_FULL[idx]} пропущен")
        await ask_week_day(update, context)
        return

    if data == "week_dayoff":
        context.user_data['week_plan_data'][idx] = {'is_day_off': True}
        context.user_data['week_plan_idx'] = idx + 1
        await query.edit_message_text(f"🏖 {DAYS_RU_FULL[idx]} — выходной")
        await ask_week_day(update, context)
        return

    if data == "week_full_day":
        context.user_data['week_plan_data'][idx] = {
            'is_day_off': False,
            'start': '10:00',
            'end': '18:00'
        }
        context.user_data['week_plan_idx'] = idx + 1
        await query.edit_message_text(f"✅ {DAYS_RU_FULL[idx]}: 10:00–18:00")
        await ask_week_day(update, context)
        return

    if data == "week_custom":
        context.user_data['state'] = 'week_custom_start'
        await query.edit_message_text(f"📅 {DAYS_RU_FULL[idx]}: своё время")
        await query.message.reply_text(
            f"🕐 Введи <b>время прихода</b> в формате <b>ЧЧ:ММ</b>\nНапример: <code>09:30</code>",
            parse_mode='HTML',
            reply_markup=cancel_keyboard()
        )
        return


async def week_custom_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        start_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как <code>09:30</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard())
        return

    context.user_data['week_temp_start'] = start_time
    idx = context.user_data['week_plan_idx']
    context.user_data['state'] = 'week_custom_end'
    await update.message.reply_text(
        f"📅 <b>{DAYS_RU_FULL[idx]}</b>, приход: <b>{start_time.strftime('%H:%M')}</b>\n\n"
        f"🕕 Введи <b>время ухода</b>:",
        parse_mode='HTML',
        reply_markup=cancel_keyboard()
    )


async def week_custom_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        end_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как <code>18:00</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard())
        return

    start_time = context.user_data['week_temp_start']
    if start_time >= end_time:
        await update.message.reply_text("❌ Время ухода позже прихода!",
                                        reply_markup=cancel_keyboard())
        return

    idx = context.user_data['week_plan_idx']
    context.user_data['week_plan_data'][idx] = {
        'is_day_off': False,
        'start': start_time.strftime('%H:%M'),
        'end': end_time.strftime('%H:%M')
    }
    context.user_data['week_plan_idx'] = idx + 1
    context.user_data['state'] = 'week_plan'

    await update.message.reply_text(
        f"✅ {DAYS_RU_FULL[idx]}: {start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')}"
    )
    await ask_week_day(update, context)


async def save_week_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    week_start = context.user_data['week_start']
    data = context.user_data.get('week_plan_data', {})

    message = update.message if update.message else update.callback_query.message

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await message.reply_text("❌ Привяжи аккаунт /start")
            return

        created = 0
        skipped = []
        for idx, info in data.items():
            day = week_start + timedelta(days=idx)
            existing = Schedule.query.filter_by(user_id=user.id, date=day).first()
            if existing:
                skipped.append(day.strftime('%d.%m'))
                continue

            if info.get('is_day_off'):
                s = Schedule(user_id=user.id, date=day, is_day_off=True, status='approved')
            else:
                s = Schedule(
                    user_id=user.id, date=day,
                    planned_start=parse_time_string(info['start']),
                    planned_end=parse_time_string(info['end']),
                    is_day_off=False, status='approved'
                )
            db.session.add(s)
            created += 1
        db.session.commit()

    text = f"✅ Готово! Сохранено {created} дней на неделю с {week_start.strftime('%d.%m.%Y')}."
    if skipped:
        text += f"\n\n⚠️ Пропущены дни (уже были заняты): {', '.join(skipped)}"

    clear_state(context)
    await message.reply_text(text, reply_markup=main_menu_keyboard())


# ================== ПЛАН НА ДЕНЬ ==================
async def plan_today_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "plan_week":
        await week_plan_start(update, context)
        return

    if query.data == "plan_today":
        d = date.today()
    else:
        d = date.today() + timedelta(days=1)

    context.user_data['plan_date'] = d
    context.user_data['state'] = 'plan_start'

    await query.edit_message_text(f"📅 Дата: {d.strftime('%d.%m.%Y')}")
    await query.message.reply_text(
        f"🕐 Введи <b>время прихода</b> в формате <b>ЧЧ:ММ</b>\nНапример: <code>10:00</code>",
        parse_mode='HTML',
        reply_markup=cancel_keyboard()
    )


async def plan_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    try:
        d = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text(
            "❌ Введи как <code>23.09.2026</code>",
            parse_mode='HTML', reply_markup=cancel_keyboard()
        )
        return

    context.user_data['plan_date'] = d
    context.user_data['state'] = 'plan_start'
    await update.message.reply_text(
        f"📅 Дата: <b>{d.strftime('%d.%m.%Y')}</b>\n\n🕐 Введи <b>время прихода</b> (ЧЧ:ММ)",
        parse_mode='HTML', reply_markup=cancel_keyboard()
    )


async def plan_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        start_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как <code>10:00</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard())
        return

    context.user_data['plan_start'] = start_time
    context.user_data['state'] = 'plan_end'
    await update.message.reply_text(
        f"🕐 Приход: <b>{start_time.strftime('%H:%M')}</b>\n\n🕕 Введи <b>время ухода</b> (ЧЧ:ММ)",
        parse_mode='HTML', reply_markup=cancel_keyboard()
    )


async def plan_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        end_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как <code>18:00</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard())
        return

    start_time = context.user_data['plan_start']
    if start_time >= end_time:
        await update.message.reply_text("❌ Время ухода должно быть позже прихода.",
                                        reply_markup=cancel_keyboard())
        return

    d = context.user_data['plan_date']
    chat_id = update.effective_chat.id

    with app.app_context():
        user = get_user_by_chat(chat_id)
        existing = Schedule.query.filter_by(user_id=user.id, date=d).first()

        if existing:
            if existing.is_day_off:
                existing_text = "🏖 Выходной"
            else:
                existing_text = f"📋 {existing.planned_start.strftime('%H:%M')}–{existing.planned_end.strftime('%H:%M')}"

            context.user_data['pending_plan'] = {'date': d, 'start': start_time, 'end': end_time}
            context.user_data['state'] = 'plan_conflict'

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Заменить", callback_data="plan_replace")],
                [InlineKeyboardButton("❌ Оставить старое", callback_data="plan_keep")],
            ])
            await update.message.reply_text(
                f"⚠️ На <b>{d.strftime('%d.%m.%Y')}</b> уже есть запись:\n"
                f"{existing_text}\n\n"
                f"Новая заявка: <b>{start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')}</b>\n\n"
                f"Что сделать?",
                parse_mode='HTML',
                reply_markup=kb
            )
            return

        schedule = Schedule(
            user_id=user.id, date=d,
            planned_start=start_time, planned_end=end_time,
            is_day_off=False, status='approved'
        )
        db.session.add(schedule)
        db.session.commit()

    clear_state(context)
    await update.message.reply_text(
        f"✅ План сохранён!\n\n📅 {d.strftime('%d.%m.%Y')}\n⏰ {start_time.strftime('%H:%M')} – {end_time.strftime('%H:%M')}",
        reply_markup=main_menu_keyboard()
    )


async def plan_conflict_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "plan_keep":
        clear_state(context)
        await query.edit_message_text("Оставлено как было.")
        await query.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())
        return

    if query.data == "plan_replace":
        data = context.user_data.get('pending_plan')
        if not data:
            await query.edit_message_text("❌ Данные потеряны, попробуй снова.")
            return

        chat_id = query.message.chat_id
        with app.app_context():
            user = get_user_by_chat(chat_id)
            existing = Schedule.query.filter_by(user_id=user.id, date=data['date']).first()
            if existing:
                existing.planned_start = data['start']
                existing.planned_end = data['end']
                existing.is_day_off = False
                existing.status = 'approved'
                db.session.commit()

        clear_state(context)
        await query.edit_message_text("✅ Заменено.")
        await query.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())


# ================== ФАКТ ==================
async def fact_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d = date.today() if query.data == "fact_today" else date.today() - timedelta(days=1)
    context.user_data['fact_date'] = d
    context.user_data['state'] = 'fact_start'

    await query.edit_message_text(f"📅 Дата: {d.strftime('%d.%m.%Y')}")
    await query.message.reply_text(
        "🕐 Введи <b>время прихода</b> (ЧЧ:ММ)",
        parse_mode='HTML',
        reply_markup=cancel_keyboard()
    )


async def fact_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text == CANCEL_TEXT.lower() or text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    if text == 'сегодня':
        d = date.today()
    elif text == 'вчера':
        d = date.today() - timedelta(days=1)
    else:
        try:
            d = datetime.strptime(text, '%d.%m.%Y').date()
        except ValueError:
            await update.message.reply_text(
                "❌ Введи как <code>23.09.2026</code>, или «сегодня»/«вчера».",
                parse_mode='HTML', reply_markup=cancel_keyboard()
            )
            return

    if d > date.today():
        await update.message.reply_text("❌ Нельзя на будущую дату.",
                                        reply_markup=cancel_keyboard())
        return

    context.user_data['fact_date'] = d
    context.user_data['state'] = 'fact_start'
    await update.message.reply_text(
        f"📅 Дата: <b>{d.strftime('%d.%m.%Y')}</b>\n\n🕐 Введи <b>время прихода</b> (ЧЧ:ММ)",
        parse_mode='HTML', reply_markup=cancel_keyboard()
    )


async def fact_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        start_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как <code>10:00</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard())
        return

    context.user_data['fact_start'] = start_time
    context.user_data['state'] = 'fact_end'
    await update.message.reply_text(
        f"🕐 Приход: <b>{start_time.strftime('%H:%M')}</b>\n\n🕕 Введи <b>время ухода</b> (ЧЧ:ММ)",
        parse_mode='HTML', reply_markup=cancel_keyboard()
    )


async def fact_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        end_time = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text("❌ Введи как <code>18:00</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard())
        return

    if context.user_data['fact_start'] >= end_time:
        await update.message.reply_text("❌ Время ухода должно быть позже прихода.",
                                        reply_markup=cancel_keyboard())
        return

    context.user_data['fact_end'] = end_time
    context.user_data['state'] = 'fact_eff'
    await update.message.reply_text(
        f"🕕 Уход: <b>{end_time.strftime('%H:%M')}</b>\n\n📈 Введи <b>эффективность</b> (0-100)",
        parse_mode='HTML', reply_markup=cancel_keyboard()
    )


async def fact_eff_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    try:
        eff = float(text)
        if not (0 <= eff <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи число от 0 до 100",
                                        reply_markup=cancel_keyboard())
        return

    chat_id = update.effective_chat.id
    d = context.user_data['fact_date']
    start_time = context.user_data['fact_start']
    end_time = context.user_data['fact_end']

    with app.app_context():
        user = get_user_by_chat(chat_id)
        existing = Attendance.query.filter_by(user_id=user.id, date=d).first()
        if existing:
            clear_state(context)
            await update.message.reply_text(
                f"⚠️ На {d.strftime('%d.%m.%Y')} уже есть отметка.",
                reply_markup=main_menu_keyboard()
            )
            return

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

    clear_state(context)
    if status == 'pending':
        await update.message.reply_text(
            "✅ Факт сохранён и ждёт подтверждения администратора.",
            reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            f"✅ Факт сохранён!\n\n📅 {d.strftime('%d.%m.%Y')}\n"
            f"⏰ {start_time.strftime('%H:%M')} – {end_time.strftime('%H:%M')}\n"
            f"📈 e%: {int(eff)}",
            reply_markup=main_menu_keyboard()
        )


# ================== ОТПУСК ==================
async def abs_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    types = {"abs_vacation": "vacation", "abs_sick": "sick", "abs_other": "other"}
    abs_type = types.get(query.data, 'vacation')
    context.user_data['abs_type'] = abs_type
    context.user_data['state'] = 'abs_date_start'
    type_ru = ABS_TYPE_RU.get(abs_type, abs_type)

    await query.edit_message_text(f"{type_ru}")
    await query.message.reply_text(
        "📅 Введи <b>дату начала</b> в формате <b>ДД.ММ.ГГГГ</b>\nНапример: <code>25.09.2026</code>",
        parse_mode='HTML',
        reply_markup=cancel_keyboard()
    )


async def abs_date_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    try:
        d = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text("❌ Введи как <code>25.09.2026</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard())
        return

    context.user_data['abs_start'] = d
    context.user_data['state'] = 'abs_date_end'
    await update.message.reply_text(
        f"📅 Начало: <b>{d.strftime('%d.%m.%Y')}</b>\n\n📅 Введи <b>дату окончания</b>",
        parse_mode='HTML', reply_markup=cancel_keyboard()
    )


async def abs_date_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    try:
        d = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError:
        await update.message.reply_text("❌ Введи как <code>30.09.2026</code>",
                                        parse_mode='HTML', reply_markup=cancel_keyboard())
        return

    if d < context.user_data['abs_start']:
        await update.message.reply_text("❌ Дата окончания раньше начала.",
                                        reply_markup=cancel_keyboard())
        return

    context.user_data['abs_end'] = d

    if context.user_data.get('abs_type') == 'other':
        context.user_data['state'] = 'abs_custom'
        await update.message.reply_text(
            "📝 Опиши причину отсутствия\n(например: «отгул за переработку»)",
            reply_markup=cancel_keyboard()
        )
        return

    context.user_data['state'] = 'abs_file'
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Пропустить (без файла)", callback_data="abs_skip_file")],
    ])
    await update.message.reply_text(
        "📎 Прикрепи справку (PDF или фото).\n\nЕсли файла нет — нажми «Пропустить».",
        reply_markup=kb
    )


async def abs_custom_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    if len(text) > 200:
        await update.message.reply_text("❌ Максимум 200 символов.",
                                        reply_markup=cancel_keyboard())
        return

    context.user_data['abs_custom'] = text
    context.user_data['state'] = 'abs_file'
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Пропустить (без файла)", callback_data="abs_skip_file")],
    ])
    await update.message.reply_text(
        "📎 Прикрепи справку или нажми «Пропустить».",
        reply_markup=kb
    )


async def abs_save_final(chat_id, context, file_path=None):
    d_start = context.user_data['abs_start']
    d_end = context.user_data['abs_end']
    abs_type = context.user_data['abs_type']
    custom = context.user_data.get('abs_custom')

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            return "❌ Привяжи аккаунт /start"

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
        type_ru = ABS_TYPE_RU_SHORT.get(abs_type, abs_type)
        if abs_type == 'other' and custom:
            type_ru = custom
        for admin in admins:
            if admin.telegram_chat_id:
                notif = Notification(
                    user_id=admin.id,
                    chat_id=admin.telegram_chat_id,
                    message=f"📩 {user.full_name} — заявка на {type_ru}: {d_start.strftime('%d.%m.%Y')} – {d_end.strftime('%d.%m.%Y')}"
                )
                db.session.add(notif)
        db.session.commit()

    type_ru = ABS_TYPE_RU.get(abs_type, abs_type)
    if abs_type == 'other' and custom:
        type_ru = f"📌 {custom}"

    text = f"✅ <b>Заявка отправлена!</b>\n\n📌 {type_ru}\n📅 {d_start.strftime('%d.%m.%Y')} – {d_end.strftime('%d.%m.%Y')}"
    if file_path:
        text += "\n📎 Файл прикреплён"
    text += "\n\nОжидай подтверждения."
    return text


async def abs_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat_id = update.effective_chat.id
    file_path = None

    if message.document:
        f = await message.document.get_file()
        ext = os.path.splitext(message.document.file_name)[1] or '.dat'
        filename = f"abs_{chat_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
        upload_folder = os.path.join(app.root_path, 'static', 'uploads')
        os.makedirs(upload_folder, exist_ok=True)
        await f.download_to_drive(os.path.join(upload_folder, filename))
        file_path = f'uploads/{filename}'
    elif message.photo:
        f = await message.photo[-1].get_file()
        filename = f"abs_{chat_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
        upload_folder = os.path.join(app.root_path, 'static', 'uploads')
        os.makedirs(upload_folder, exist_ok=True)
        await f.download_to_drive(os.path.join(upload_folder, filename))
        file_path = f'uploads/{filename}'
    elif message.text and message.text.strip().lower() == 'пропустить':
        file_path = None
    else:
        await message.reply_text("❌ Отправь файл или нажми «Пропустить».")
        return

    text = await abs_save_final(chat_id, context, file_path=file_path)
    clear_state(context)
    await message.reply_text(text, parse_mode='HTML', reply_markup=main_menu_keyboard())


async def abs_skip_file_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    text = await abs_save_final(chat_id, context, file_path=None)
    clear_state(context)
    await query.edit_message_text(text, parse_mode='HTML')
    await query.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())


# ================== ГЛАВНЫЙ РОУТЕР ==================
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ''
    chat_id = update.effective_chat.id

    with app.app_context():
        user = get_user_by_chat(chat_id)

    if not user:
        if context.user_data.get('state') == 'ask_email':
            await receive_email(update, context)
        else:
            await update.message.reply_text(
                "Сначала отправь /start и привяжи аккаунт.",
                reply_markup=cancel_keyboard()
            )
        return

    state = context.user_data.get('state')

    if state in ('plan_date', 'plan_start', 'plan_end',
                 'fact_date', 'fact_start', 'fact_end', 'fact_eff',
                 'abs_date_start', 'abs_date_end', 'abs_custom', 'abs_file',
                 'week_custom_start', 'week_custom_end'):

        if text == CANCEL_TEXT:
            clear_state(context)
            await update.message.reply_text("Отменено. Главное меню 👇",
                                            reply_markup=main_menu_keyboard())
            return

        if state == 'plan_date':
            await plan_date_handler(update, context)
        elif state == 'plan_start':
            await plan_start_handler(update, context)
        elif state == 'plan_end':
            await plan_end_handler(update, context)
        elif state == 'fact_date':
            await fact_date_handler(update, context)
        elif state == 'fact_start':
            await fact_start_handler(update, context)
        elif state == 'fact_end':
            await fact_end_handler(update, context)
        elif state == 'fact_eff':
            await fact_eff_handler(update, context)
        elif state == 'abs_date_start':
            await abs_date_start_handler(update, context)
        elif state == 'abs_date_end':
            await abs_date_end_handler(update, context)
        elif state == 'abs_custom':
            await abs_custom_handler(update, context)
        elif state == 'abs_file':
            await abs_file_handler(update, context)
        elif state == 'week_custom_start':
            await week_custom_start_handler(update, context)
        elif state == 'week_custom_end':
            await week_custom_end_handler(update, context)
        return

    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Главное меню 👇", reply_markup=main_menu_keyboard())
        return

    if 'Мой профиль' in text:
        clear_state(context); await cmd_profile(update, context); return
    if 'Расписание недели' in text:
        clear_state(context); await cmd_week(update, context); return
    if 'Расписание на неделю' in text:
        await week_plan_start(update, context); return
    if 'Расписание всех' in text:
        clear_state(context); await cmd_all_week(update, context); return
    if 'Кто работает' in text:
        clear_state(context); await cmd_who(update, context); return
    if 'Мои заявки' in text:
        clear_state(context); await cmd_my_absences(update, context); return
    if 'Итоги месяца' in text:
        clear_state(context); await cmd_summary(update, context); return
    if 'Помощь' in text:
        clear_state(context); await cmd_help(update, context); return
    if 'Отвязать аккаунт' in text:
        clear_state(context); await detach_start(update, context); return

    if 'Плановое время' in text:
        clear_state(context)
        context.user_data['state'] = 'plan_date'
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Сегодня", callback_data="plan_today")],
            [InlineKeyboardButton("📅 Завтра", callback_data="plan_tomorrow")],
            [InlineKeyboardButton("📆 На всю неделю", callback_data="plan_week")],
        ])
        await update.message.reply_text(
            "🕐 <b>Плановое время</b>\n\nНа какой день?",
            parse_mode='HTML', reply_markup=kb
        )
        return

    if 'Фактическое время' in text:
        clear_state(context)
        context.user_data['state'] = 'fact_date'
        today = date.today()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📅 Сегодня ({today.strftime('%d.%m')})", callback_data="fact_today")],
            [InlineKeyboardButton("📅 Вчера", callback_data="fact_yesterday")],
        ])
        await update.message.reply_text(
            "⏰ <b>Фактическое время</b>\n\nВыбери дату:",
            parse_mode='HTML', reply_markup=kb
        )
        return

    if 'Заявка на отпуск' in text:
        clear_state(context)
        context.user_data['state'] = 'abs_type'
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏖 Отпуск", callback_data="abs_vacation")],
            [InlineKeyboardButton("🤒 Больничный", callback_data="abs_sick")],
            [InlineKeyboardButton("📌 Другое", callback_data="abs_other")],
        ])
        await update.message.reply_text(
            "🏖 <b>Заявка на отсутствие</b>\n\nВыбери тип:",
            parse_mode='HTML', reply_markup=kb
        )
        return

    await update.message.reply_text(
        "🤔 Не понимаю команду. Используй кнопки внизу 👇",
        reply_markup=main_menu_keyboard()
    )


# ================== ПАРСИНГ ГРУППЫ ==================
async def group_parser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    tg_user = update.effective_user

    if chat.type not in ('group', 'supergroup'):
        return
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    if not (text.startswith('#план') or text.startswith('#факт') or text.startswith('#отпуск')):
        return

    parts = text.split()
    if len(parts) < 2:
        return

    command = parts[0].lower()

    with app.app_context():
        user = None
        if tg_user.username:
            user = User.query.filter(User.telegram_id == f"@{tg_user.username}").first()
        if not user:
            user = User.query.filter(User.telegram_chat_id == str(tg_user.id)).first()

        if not user:
            await msg.reply_text(
                f"⚠️ {tg_user.full_name}, сначала привяжи аккаунт через /start в личке с ботом."
            )
            return

        try:
            if command == '#план':
                if len(parts) != 4:
                    raise ValueError("формат: #план ДД.ММ.ГГГГ ЧЧ:ММ ЧЧ:ММ")
                d = datetime.strptime(parts[1], '%d.%m.%Y').date()
                start_time = parse_time_string(parts[2])
                end_time = parse_time_string(parts[3])
                if start_time >= end_time:
                    raise ValueError("время ухода должно быть позже")

                existing = Schedule.query.filter_by(user_id=user.id, date=d).first()
                if existing:
                    await msg.reply_text(f"⚠️ {user.full_name}, на {d.strftime('%d.%m.%Y')} уже есть план.")
                    return

                schedule = Schedule(
                    user_id=user.id, date=d,
                    planned_start=start_time, planned_end=end_time,
                    is_day_off=False, status='approved'
                )
                db.session.add(schedule)
                db.session.commit()
                await msg.reply_text(
                    f"✅ {user.full_name}, план на {d.strftime('%d.%m.%Y')}: "
                    f"{start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')}"
                )

            elif command == '#факт':
                if len(parts) != 5:
                    raise ValueError("формат: #факт ДД.ММ.ГГГГ ЧЧ:ММ ЧЧ:ММ ЭФФЕКТИВНОСТЬ")
                d = datetime.strptime(parts[1], '%d.%m.%Y').date()
                start_time = parse_time_string(parts[2])
                end_time = parse_time_string(parts[3])
                eff = float(parts[4])
                if start_time >= end_time:
                    raise ValueError("время ухода должно быть позже")
                if d > date.today():
                    raise ValueError("нельзя на будущую дату")
                if not (0 <= eff <= 100):
                    raise ValueError("эффективность от 0 до 100")

                existing = Attendance.query.filter_by(user_id=user.id, date=d).first()
                if existing:
                    await msg.reply_text(f"⚠️ {user.full_name}, на {d.strftime('%d.%m.%Y')} уже есть факт.")
                    return

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
                await msg.reply_text(
                    f"✅ {user.full_name}, факт на {d.strftime('%d.%m.%Y')}: "
                    f"{start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')} (e% {int(eff)})"
                )

            elif command == '#отпуск':
                if len(parts) < 4:
                    raise ValueError("формат: #отпуск ДД.ММ.ГГГГ ДД.ММ.ГГГГ ТИП")
                d_start = datetime.strptime(parts[1], '%d.%m.%Y').date()
                d_end = datetime.strptime(parts[2], '%d.%m.%Y').date()
                abs_type = parts[3]
                if d_start > d_end:
                    raise ValueError("дата начала позже конца")
                if abs_type not in ('vacation', 'sick', 'other'):
                    raise ValueError("тип: vacation / sick / other")

                custom = ' '.join(parts[4:]) if len(parts) > 4 else None

                absence = Absence(
                    user_id=user.id,
                    date_start=d_start, date_end=d_end,
                    type=abs_type,
                    custom_type=custom if abs_type == 'other' else None,
                    status='pending'
                )
                db.session.add(absence)
                db.session.commit()
                await msg.reply_text(
                    f"✅ {user.full_name}, заявка отправлена: "
                    f"{d_start.strftime('%d.%m.%Y')} – {d_end.strftime('%d.%m.%Y')}"
                )

        except ValueError as e:
            await msg.reply_text(f"❌ Ошибка: {e}")
        except Exception as e:
            logger.error(f"Ошибка парсинга: {e}")
            db.session.rollback()
            await msg.reply_text(f"❌ Ошибка: {e}")


# ================== РАССЫЛКИ ==================
async def morning_who(context: ContextTypes.DEFAULT_TYPE):
    with app.app_context():
        today = date.today()
        next_monday = today + timedelta(days=(7 - today.weekday()))

        plans = Schedule.query.filter_by(date=today, status='approved').all()
        working_lines = []
        for plan in plans:
            if plan.is_day_off or not plan.user or plan.user.status != 'active':
                continue
            if plan.planned_start and plan.planned_end:
                working_lines.append(
                    f"• {plan.user.full_name}: {plan.planned_start.strftime('%H:%M')}–{plan.planned_end.strftime('%H:%M')}"
                )

        if working_lines:
            text_who = "☀️ <b>Доброе утро!</b>\n\n👥 <b>Сегодня работают:</b>\n\n" + "\n".join(working_lines)
        else:
            text_who = "☀️ <b>Доброе утро!</b>\n\nСегодня никто не работает."

        employees = User.query.filter_by(role='employee', status='active').all()
        for emp in employees:
            if not emp.telegram_chat_id:
                continue

            has_plan_next = Schedule.query.filter(
                Schedule.user_id == emp.id,
                Schedule.date >= next_monday
            ).first() is not None

            if not has_plan_next:
                reminder = (
                    f"\n\n📅 <b>Напоминание:</b> у тебя ещё нет расписания на следующую неделю "
                    f"(с {next_monday.strftime('%d.%m.%Y')}).\n"
                    f"Отправь через «📅 Расписание на неделю»"
                )
            else:
                reminder = ""

            try:
                await context.bot.send_message(
                    chat_id=emp.telegram_chat_id,
                    text=text_who + reminder,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Ошибка утренней рассылки {emp.id}: {e}")


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


# ================== ЗАПУСК ==================
def main():
    if not Config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не задан в .env")
        return

    application = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler('start', start))

    # Callback-обработчики
    application.add_handler(CallbackQueryHandler(plan_today_callback, pattern='^plan_(today|tomorrow|week)$'))
    application.add_handler(CallbackQueryHandler(plan_conflict_callback, pattern='^plan_(replace|keep)$'))
    application.add_handler(CallbackQueryHandler(fact_date_callback, pattern='^fact_(today|yesterday)$'))
    application.add_handler(CallbackQueryHandler(abs_type_callback, pattern='^abs_(vacation|sick|other)$'))
    application.add_handler(CallbackQueryHandler(abs_skip_file_callback, pattern='^abs_skip_file$'))
    application.add_handler(CallbackQueryHandler(week_callback, pattern='^week_(prev|next|now)$'))
    application.add_handler(CallbackQueryHandler(all_week_callback, pattern='^allweek_'))
    application.add_handler(CallbackQueryHandler(week_plan_callback, pattern='^week_(full_day|custom|dayoff|skip|cancel)$'))
    application.add_handler(CallbackQueryHandler(summary_choice_callback, pattern='^sum_(my|all)$'))
    application.add_handler(CallbackQueryHandler(summary_callback, pattern='^(sum_|allsum_)(prev|next|now)$'))
    application.add_handler(CallbackQueryHandler(detach_callback, pattern='^detach_'))
    application.add_handler(CallbackQueryHandler(week_pick_callback, pattern='^week_pick_'))

    # Парсинг группы
    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        group_parser
    ))

    # Файлы и фото (для справок)
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, text_router))

    # Все текстовые
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # Уведомления
    application.job_queue.run_repeating(check_notifications, interval=5, first=5)

    # ⚠️ ВРЕМЕННАЯ РАССЫЛКА — сегодня в 12:15 без фильтра по дням
    # После проверки замените на:
    # time=dtime(hour=8, minute=0), days=(0, 1, 2, 3, 4)   # Пн-Пт
    application.job_queue.run_daily(
        morning_who,
        time=dtime(hour=12, minute=15)
    )

    print("🤖 Бот запущен. Нажми Ctrl+C для остановки.")
    application.run_polling()


if __name__ == '__main__':
    main()