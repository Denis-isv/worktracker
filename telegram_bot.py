import logging
import os
import re
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

# --- ИИ-парсер (опционально) ---
try:
    from ai_parser import get_ai_parser_from_env
    AI_PARSER = get_ai_parser_from_env()
    if AI_PARSER:
        logger.info(f"✅ ИИ-парсер активирован: {AI_PARSER.model} @ {AI_PARSER.base_url}")
    else:
        logger.info("ℹ️ ИИ-парсер не настроен (AI_ENABLED=false или нет ключа)")
except ImportError:
    AI_PARSER = None
    logger.info("ℹ️ ai_parser.py не найден — ИИ отключён")

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

SEP = "━━━━━━━━━━━━━━━━━━━━"
CANCEL_TEXT = '❌ Отмена'


# ================== КЛАВИАТУРЫ ==================
def main_menu_keyboard():
    kb = [
        [KeyboardButton('📅 Моё расписание'), KeyboardButton('👤 Мой профиль')],
        [KeyboardButton('⏰ Фактическое время'), KeyboardButton('🏖 Заявка на отпуск')],
        [KeyboardButton('📂 Мои заявки'), KeyboardButton('👥 Кто работает')],
        [KeyboardButton('📊 Итоги месяца'), KeyboardButton('👥 Расписание всех')],
        [KeyboardButton('📋 На неделю'), KeyboardButton('❓ Помощь')],
        [KeyboardButton('🔓 Отвязать аккаунт')],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def cancel_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton(CANCEL_TEXT)]], resize_keyboard=True)


def schedule_week_keyboard(week_start, user, offset):
    today = date.today()
    row_days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
        att = Attendance.query.filter(
            Attendance.user_id == user.id,
            Attendance.date == d,
            Attendance.status != 'rejected'
        ).first()
        if sch and sch.status == 'rejected':
            sch = None

        if sch and sch.is_day_off:
            icon = "🏖"
        elif sch and att:
            icon = "🟢"
        elif sch:
            icon = "📋"
        elif att:
            icon = "⏰"
        else:
            icon = "⚪"

        today_marker = " •" if d == today else ""
        label = f"{DAYS_RU_SHORT[i]} {d.day} {icon}{today_marker}"
        row_days.append(InlineKeyboardButton(label, callback_data=f"sched_day_{d.isoformat()}"))

    end_of_week = week_start + timedelta(days=6)
    nav_row = [
        InlineKeyboardButton("⬅️", callback_data=f"sched_week_{offset - 1}"),
        InlineKeyboardButton(f"{week_start.strftime('%d.%m')}–{end_of_week.strftime('%d.%m')}",
                             callback_data="sched_noop"),
        InlineKeyboardButton("➡️", callback_data=f"sched_week_{offset + 1}"),
    ]
    return InlineKeyboardMarkup([nav_row, row_days[:4], row_days[4:]])


def schedule_day_keyboard(d, sch, att):
    ds = d.isoformat()
    has_plan = bool(sch and (sch.planned_start or sch.is_day_off))
    has_tasks = bool(sch and sch.plan_text)
    has_fact = bool(att and att.actual_start)
    has_note = bool(att and att.note)

    rows = []
    rows.append([InlineKeyboardButton("🕐 Плановое время", callback_data=f"sched_time_{ds}")])

    tasks_label = "📝 Задачи на день" + (" · ✓" if has_tasks else "")
    rows.append([InlineKeyboardButton(tasks_label, callback_data=f"sched_tasks_{ds}")])

    if has_fact:
        note_label = "✅ Что сделал" + (" · ✓" if has_note else "")
        rows.append([InlineKeyboardButton(note_label, callback_data=f"sched_note_{ds}")])
    else:
        rows.append([InlineKeyboardButton("✅ Что сделал (нужен факт)",
                                          callback_data=f"sched_note_{ds}")])

    rows.append([InlineKeyboardButton("🏖 Отметить выходным", callback_data=f"sched_dayoff_{ds}")])

    if has_plan or has_fact or has_tasks or has_note:
        rows.append([InlineKeyboardButton("🗑 Очистить день полностью",
                                          callback_data=f"sched_clear_{ds}")])
    rows.append([InlineKeyboardButton("⬅️ К расписанию недели", callback_data="sched_week_0")])
    return InlineKeyboardMarkup(rows)


# ================== УТИЛИТЫ ==================
def get_user_by_chat(chat_id):
    return User.query.filter_by(telegram_chat_id=str(chat_id)).first()


def find_user_by_email(email):
    return User.query.filter(func.lower(User.email) == email.strip().lower()).first()


def esc(text):
    if text is None:
        return ''
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def normalize_name(s):
    if not s:
        return ''
    return s.lower().replace('.', '').replace(' ', '').replace(',', '')


def find_user_by_name(name_str):
    if not name_str:
        return None
    target = normalize_name(name_str)
    if not target:
        return None
    for u in User.query.filter_by(role='employee', status='active').all():
        if normalize_name(u.full_name) == target:
            return u
    for u in User.query.filter_by(role='employee', status='active').all():
        if normalize_name(u.full_name).startswith(target):
            return u
    for u in User.query.filter_by(role='employee', status='active').all():
        parts = u.full_name.split()
        if not parts:
            continue
        surname = normalize_name(parts[0])
        if surname and target.startswith(surname):
            return u
    return None


def find_user_by_telegram(update_or_user, chat_id=None):
    tg_user = None
    if hasattr(update_or_user, 'effective_user'):
        tg_user = update_or_user.effective_user
        if chat_id is None:
            chat_id = update_or_user.effective_chat.id
    else:
        tg_user = update_or_user

    if chat_id is not None:
        u = User.query.filter_by(telegram_chat_id=str(chat_id)).first()
        if u:
            return u
    if tg_user and getattr(tg_user, 'id', None):
        u = User.query.filter_by(telegram_chat_id=str(tg_user.id)).first()
        if u:
            return u
    if tg_user and getattr(tg_user, 'username', None):
        u = User.query.filter(User.telegram_id == f"@{tg_user.username}").first()
        if u:
            return u
    return None


def clear_state(context):
    keys = [
        'state', 'fact_date', 'fact_start', 'fact_end', 'fact_eff',
        'abs_type', 'abs_start', 'abs_end', 'abs_custom',
        'week_plan_idx', 'week_plan_data', 'week_start', 'week_temp_start',
        'pending_plan', 'pending_fact', 'pending_daily', 'pending_week_plan',
        'sched_edit_date', 'sched_edit_time_start',
        'sched_day_msg_id', 'pending_sched_edit',
        'week_overwrite_idx',
        'sched_tasks_mode',
        'sched_edit_kind',
    ]
    for k in keys:
        context.user_data.pop(k, None)


# ================== ИИ-РАЗБОР СООБЩЕНИЙ ==================
def _ai_parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _ai_parse_time(s):
    if not s:
        return None
    try:
        h, m = s.split(':')
        return parse_time_string(f"{h}:{m}")
    except Exception:
        return None


async def save_ai_result(user, parsed, context):
    """Сохраняет результат ИИ в БД. Возвращает текст ответа."""
    action = parsed.get('action', 'unknown')
    response = parsed.get('response', '')

    if action == 'plan':
        d = _ai_parse_date(parsed.get('date')) or (date.today() + timedelta(days=1))
        start = _ai_parse_time(parsed.get('start'))
        end = _ai_parse_time(parsed.get('end'))
        tasks = parsed.get('tasks') or []
        plan_text = '\n'.join(f"- {t}" for t in tasks) if tasks else None

        if not start or not end:
            return "⚠️ Не указано время. Напиши, например: «завтра с 10 до 18»."

        existing = Schedule.query.filter_by(user_id=user.id, date=d).first()
        if existing:
            existing.planned_start = start
            existing.planned_end = end
            existing.is_day_off = False
            existing.status = 'approved'
            if plan_text:
                existing.plan_text = plan_text
        else:
            sch = Schedule(
                user_id=user.id, date=d,
                planned_start=start, planned_end=end,
                is_day_off=False, status='approved',
                plan_text=plan_text,
            )
            db.session.add(sch)
        db.session.commit()
        txt = f"✅ План на {d.strftime('%d.%m.%Y')}: {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        if tasks:
            txt += f"\n📝 Задачи: {len(tasks)}"
        return txt

    if action == 'fact':
        d = _ai_parse_date(parsed.get('date')) or date.today()
        start = _ai_parse_time(parsed.get('start'))
        end = _ai_parse_time(parsed.get('end'))
        eff = parsed.get('efficiency')
        tasks = parsed.get('tasks') or []

        if not start or not end:
            return "⚠️ Не указано время. Напиши, например: «сегодня работал с 10 до 18»."

        existing = Attendance.query.filter_by(user_id=user.id, date=d).first()
        if existing:
            return ("⚠️ На эту дату уже есть запись факта. "
                    "Измени её через «📅 Моё расписание».")

        eff_val = (float(eff) / 100) if eff else 1.0
        sch = Schedule.query.filter_by(user_id=user.id, date=d, status='approved').first()
        early = 0
        if sch and sch.planned_start and not sch.is_day_off:
            early = time_to_minutes(start) - time_to_minutes(sch.planned_start)
        status = 'confirmed' if d == date.today() else 'pending'
        att = Attendance(
            user_id=user.id, date=d,
            actual_start=start, actual_end=end,
            efficiency=eff_val, early_start=early,
            status=status,
        )
        if tasks:
            att.note = '\n'.join(f"- {t}" for t in tasks)
        db.session.add(att)
        db.session.commit()

        txt = (f"✅ Факт за {d.strftime('%d.%m.%Y')}: "
               f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} "
               f"(e% {int(eff_val * 100)})")
        return txt

    if action == 'note':
        tasks = parsed.get('tasks') or []
        if not tasks:
            return "⚠️ Не понял, что именно ты сделал. Уточни."
        note_text = '\n'.join(f"- {t}" for t in tasks)
        d = _ai_parse_date(parsed.get('date')) or date.today()
        att = Attendance.query.filter_by(user_id=user.id, date=d).first()
        if not att:
            return (f"⚠️ На {d.strftime('%d.%m.%Y')} нет записи факта. "
                    f"Сначала напиши, сколько работал, например: "
                    f"«сегодня с 10 до 18», а потом «сделал: отчёт, встреча».")
        att.note = note_text
        db.session.commit()
        return f"✅ Записал {len(tasks)} задач в отчёт за {d.strftime('%d.%m.%Y')}"

    if action == 'absence':
        d_start = _ai_parse_date(parsed.get('date'))
        d_end = _ai_parse_date(parsed.get('date_end')) or d_start
        abs_type = parsed.get('absence_type', 'other')
        if not d_start:
            return "⚠️ Не понял даты. Напиши, например: «отпуск с 5 по 10 ноября»."
        if abs_type not in ('vacation', 'sick', 'other'):
            abs_type = 'other'
        absence = Absence(
            user_id=user.id, date_start=d_start, date_end=d_end,
            type=abs_type, status='pending',
        )
        db.session.add(absence)
        db.session.commit()
        return (f"✅ Заявка на {ABS_TYPE_RU_SHORT.get(abs_type, abs_type)} "
                f"{d_start.strftime('%d.%m.%Y')} – {d_end.strftime('%d.%m.%Y')}. "
                f"Ждёт подтверждения администратора.")

    if action == 'query':
        qtype = parsed.get('query_type', 'summary')
        if qtype == 'summary':
            return summary_text(user, 0)
        if qtype == 'schedule':
            today = date.today()
            ws = today - timedelta(days=today.weekday())
            text, _ = _schedule_week_view_data(user, ws, 0)
            return text
        if qtype == 'who':
            return who_text()
        return "📊 Уточни, что показать: «итоги месяца», «расписание» или «кто работает»."

    if action == 'chat':
        return response or "😊 Чем помочь?"

    if action == 'error':
        return response

    return response or ("🤔 Не понял. Напиши, например: "
                        "«сегодня работал с 10 до 18» или "
                        "«запиши: сделал отчёт и встречу».")


async def ai_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка свободного текста через ИИ. Возвращает True, если обработано."""
    if not AI_PARSER:
        return False

    msg = update.message
    chat = update.effective_chat
    if not msg or not msg.text:
        return False

    text = msg.text.strip()
    if not text:
        return False

    # Не мешаем активному диалогу с кнопками
    if context.user_data.get('state'):
        return False

    # В группе — только по триггеру
    if chat.type in ('group', 'supergroup'):
        bot_username = context.bot.username
        triggered = False
        if bot_username:
            for t in (f"@{bot_username}", f"@{bot_username.lower()}"):
                if t in text:
                    text = text.replace(t, '').strip()
                    triggered = True
                    break
        if not triggered:
            for prefix in ('ии,', 'ии ', 'бот,', 'бот ', 'запиши ', 'работал ', 'планирую '):
                if text.lower().startswith(prefix):
                    triggered = True
                    break
        if not triggered:
            return False
    else:
        if text.startswith('/'):
            return False
        btn = ['📅 Моё расписание', '👤 Мой профиль', '⏰ Фактическое время',
               '🏖 Заявка на отпуск', '📂 Мои заявки', '👥 Кто работает',
               '📊 Итоги месяца', '👥 Расписание всех', '📋 На неделю',
               '❓ Помощь', '🔓 Отвязать аккаунт', CANCEL_TEXT]
        if text in btn:
            return False

    with app.app_context():
        user = get_user_by_chat(chat.id)
        if not user:
            return False

        try:
            await msg.chat.send_action("typing")
        except Exception:
            pass

        parsed = await AI_PARSER.analyze(
            text=text,
            user_full_name=user.full_name,
        )

        try:
            result_text = await save_ai_result(user, parsed, context)
        except Exception as e:
            logger.exception(f"AI save error: {e}")
            result_text = f"⚠️ Ошибка сохранения: {e}"

    # Отвечаем
    if chat.type in ('group', 'supergroup'):
        short = result_text.split('\n')[0]
        await msg.reply_text(short)
    else:
        try:
            await msg.reply_text(result_text, parse_mode='HTML')
        except Exception:
            await msg.reply_text(result_text)
    return True

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
                'title': ImageFont.truetype(path, 34),
                'day': ImageFont.truetype(path, 24),
                'header': ImageFont.truetype(path, 20),
                'cell': ImageFont.truetype(path, 18),
                'small': ImageFont.truetype(path, 16),
            }
        except Exception:
            continue
    d = ImageFont.load_default()
    return {'title': d, 'day': d, 'header': d, 'cell': d, 'small': d}


def _tasks_short(tasks, limit=250):
    if not tasks:
        return ''
    lines = []
    for line in tasks.split('\n'):
        s = line.strip()
        if not s:
            continue
        if s.startswith('-'):
            s = s[1:].strip()
        if s:
            lines.append(s)
    joined = ' • '.join(lines)
    if len(joined) > limit:
        joined = joined[:limit - 3] + '...'
    return joined


def _task_lines(text):
    out = []
    if not text:
        return out
    for line in text.split('\n'):
        s = line.strip()
        if not s:
            continue
        if s.startswith('-'):
            s = s[1:].strip()
        if s:
            out.append(s)
    return out


def _norm_task_lines(text):
    out = []
    for line in text.split('\n'):
        s = line.strip()
        if not s:
            continue
        if not s.startswith('-'):
            s = '- ' + s
        out.append(s)
    return out


# ================== ТЕКСТЫ ==================
def profile_text(user):
    today = date.today()
    schedule = Schedule.query.filter_by(user_id=user.id, date=today).first()
    attendance = Attendance.query.filter(
        Attendance.user_id == user.id, Attendance.date == today,
        Attendance.status != 'rejected'
    ).first()
    absence = Absence.query.filter(
        Absence.user_id == user.id,
        Absence.date_start <= today, Absence.date_end >= today,
        Absence.status == 'approved'
    ).first()

    role_ru = ROLE_RU.get(user.role, user.role)
    text = f"👤 <b>{esc(user.full_name)}</b>\n"
    text += f"📧 {esc(user.email)}\n"
    text += f"🎭 Роль: {esc(role_ru)}\n"
    if user.telegram_id:
        text += f"💬 {esc(user.telegram_id)}\n"

    if absence:
        abs_ru = ABS_TYPE_RU.get(absence.type, absence.type)
        if absence.type == 'other' and absence.custom_type:
            abs_ru = f"📌 {esc(absence.custom_type)}"
        text += f"\n{abs_ru} до {absence.date_end.strftime('%d.%m.%Y')}\n"

    text += f"\n{SEP}\n"
    text += f"<b>Сегодня — {DAYS_RU_FULL[today.weekday()]}, {today.strftime('%d.%m.%Y')}</b>\n\n"

    if schedule and schedule.status != 'rejected':
        if schedule.is_day_off:
            text += "🏖 Выходной\n"
        elif schedule.planned_start and schedule.planned_end:
            text += f"📋 План: <b>{schedule.planned_start.strftime('%H:%M')} – {schedule.planned_end.strftime('%H:%M')}</b>\n"
            if schedule.plan_text:
                for t in _task_lines(schedule.plan_text):
                    text += f"   📝 {esc(t)}\n"
    else:
        text += "📋 План: <i>не указан</i>\n"

    if attendance and attendance.actual_start and attendance.actual_end:
        text += f"\n⏰ Факт: <b>{attendance.actual_start.strftime('%H:%M')} – {attendance.actual_end.strftime('%H:%M')}</b>\n"
        text += f"📈 Эффективность: <b>{int(attendance.efficiency * 100)}%</b>\n"
        if attendance.note:
            for t in _task_lines(attendance.note):
                text += f"   ✅ {esc(t)}\n"
        text += f"🎯 {STATUS_RU.get(attendance.status, attendance.status)}"
    return text


def who_text():
    today = date.today()
    plans = Schedule.query.filter_by(date=today, status='approved').all()
    text = f"👥 <b>Кто работает сегодня</b>\n<i>{today.strftime('%d.%m.%Y')}</i>\n{SEP}\n"
    found = False
    for plan in plans:
        if plan.is_day_off or not plan.user or plan.user.status != 'active':
            continue
        if not plan.planned_start or not plan.planned_end:
            continue
        found = True
        st = plan.planned_start.strftime('%H:%M')
        en = plan.planned_end.strftime('%H:%M')
        text += f"\n<b>{esc(plan.user.full_name)}</b>\n🕐 {st} – {en}\n"
        if plan.plan_text:
            for t in _task_lines(plan.plan_text):
                text += f"   📝 {esc(t)}\n"
    if not found:
        text += "\n<i>Сегодня никто не работает.</i>"
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
        f"📊 <b>Мои итоги</b>\n"
        f"<i>{MONTHS_RU_FULL[m-1]} {y}</i>\n"
        f"{SEP}\n\n"
        f"👤 {esc(user.full_name)}\n\n"
        f"📅 Дней отработано: <b>{count_days}</b>\n"
        f"⏱ Всего часов: <b>{total_worked_min / 60:.2f}</b>\n"
        f"📈 Средний e%: <b>{avg_eff:.1f}%</b>\n"
        f"⏰ Опоздания: <b>{total_late / 60:.2f} ч</b>\n"
        f"✅ Итого: <b>{final_min / 60:.2f} ч</b>"
    )


def absences_text(user):
    absences = Absence.query.filter_by(user_id=user.id).order_by(Absence.date_start.desc()).limit(10).all()
    if not absences:
        return "📂 У тебя пока нет заявок на отпуск/больничный."
    text = f"📂 <b>Мои последние заявки</b>\n{SEP}\n"
    for ab in absences:
        if ab.type == 'other' and ab.custom_type:
            type_ru = f"📌 {esc(ab.custom_type)}"
        else:
            type_ru = ABS_TYPE_RU.get(ab.type, ab.type)
        status = STATUS_RU.get(ab.status, ab.status)
        text += (
            f"\n{type_ru}\n"
            f"📅 {ab.date_start.strftime('%d.%m.%Y')} – {ab.date_end.strftime('%d.%m.%Y')}\n"
            f"📊 {esc(status)}\n"
        )
        if ab.file_path:
            text += "📎 файл прикреплён\n"
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
            Attendance.date >= start_date, Attendance.date <= end_date,
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
                elif sch.planned_start and sch.planned_end:
                    emp_row.append(f"{sch.planned_start.strftime('%H:%M')}–{sch.planned_end.strftime('%H:%M')}")
                    has_any = True
                else:
                    emp_row.append('—')
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
        draw.text((30, y0 + table_header_height + 30),
                  'Пока никто не составил расписание на эту неделю.',
                  fill='#6b7280', font=f['header'])

    draw.text((30, img_height - 40),
              f'Сформировано: {datetime.now().strftime("%d.%m.%Y %H:%M")}',
              fill='#6b7280', font=f['small'])

    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


def generate_my_week_image(user, offset=0):
    """Красивая карточка «Моё расписание»."""
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    end_of_week = start_of_week + timedelta(days=6)

    img_width = 1120
    header_height = 165
    day_height = 165
    footer_height = 60
    img_height = header_height + day_height * 7 + footer_height

    img = Image.new('RGB', (img_width, img_height), '#f8fafc')
    draw = ImageDraw.Draw(img)
    f = get_fonts()

    # Заголовок
    draw.rectangle([0, 0, img_width, header_height], fill='#4f46e5')
    draw.text((40, 25), 'МОЁ РАСПИСАНИЕ', fill='white', font=f['title'])
    draw.text((40, 85),
              f"{start_of_week.strftime('%d.%m')} – {end_of_week.strftime('%d.%m.%Y')}",
              fill='#e0e7ff', font=f['header'])
    draw.text((40, 122), f"👤 {user.full_name}", fill='#c7d2fe', font=f['small'])

    for i in range(7):
        d = start_of_week + timedelta(days=i)
        y = header_height + i * day_height
        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
        att = Attendance.query.filter(
            Attendance.user_id == user.id,
            Attendance.date == d,
            Attendance.status != 'rejected'
        ).first()
        if sch and sch.status == 'rejected':
            sch = None

        is_today = (d == today)
        if is_today:
            bg = '#eef2ff'
        else:
            bg = '#ffffff' if i % 2 == 0 else '#f9fafb'
        draw.rectangle([0, y, img_width, y + day_height], fill=bg)

        if is_today:
            draw.rectangle([0, y, img_width - 1, y + day_height - 1], outline='#4f46e5', width=3)

        # Разделительная линия
        draw.line([(0, y), (img_width, y)], fill='#e5e7eb', width=1)

        # Левая колонка: день
        day_name = DAYS_RU_FULL[d.weekday()]
        draw.text((40, y + 22), day_name, fill='#111827', font=f['day'])
        draw.text((40, y + 58), d.strftime('%d.%m.%Y'), fill='#6b7280', font=f['cell'])
        if is_today:
            draw.text((40, y + 90), 'СЕГОДНЯ', fill='#4f46e5', font=f['small'])

        # Вертикальный разделитель
        draw.line([(300, y + 20), (300, y + day_height - 20)], fill='#e5e7eb', width=2)

        info_x = 330
        line_y = y + 18

        # План
        if sch and sch.is_day_off:
            draw.text((info_x, line_y), "🏖 Выходной день", fill='#6b7280', font=f['cell'])
            line_y += 30
        else:
            if sch and sch.planned_start and sch.planned_end:
                plan_txt = f"📋 План:  {sch.planned_start.strftime('%H:%M')} – {sch.planned_end.strftime('%H:%M')}"
                draw.text((info_x, line_y), plan_txt, fill='#1e40af', font=f['cell'])
            else:
                draw.text((info_x, line_y), "📋 План:  —", fill='#9ca3af', font=f['cell'])
            line_y += 30

            # Факт
            if att and att.actual_start and att.actual_end:
                eff = int(att.efficiency * 100)
                fact_txt = (f"⏰ Факт:  {att.actual_start.strftime('%H:%M')} – "
                            f"{att.actual_end.strftime('%H:%M')}   ·   e% {eff}")
                draw.text((info_x, line_y), fact_txt, fill='#047857', font=f['cell'])
            else:
                draw.text((info_x, line_y), "⏰ Факт:  —", fill='#9ca3af', font=f['cell'])
            line_y += 30

            # Задачи плана
            if sch and sch.plan_text:
                short = _tasks_short(sch.plan_text, limit=80)
                draw.text((info_x, line_y), f"📝 {short}", fill='#4f46e5', font=f['small'])
                line_y += 24

            # Что сделал
            if att and att.note:
                short = _tasks_short(att.note, limit=80)
                draw.text((info_x, line_y), f"✅ {short}", fill='#047857', font=f['small'])

    draw.text((40, img_height - 32),
              f'Сформировано: {datetime.now().strftime("%d.%m.%Y %H:%M")}',
              fill='#9ca3af', font=f['small'])

    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


# ================== ПАРСИНГ ==================
def parse_time_range(text):
    m = re.search(r'(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})', text)
    if not m:
        return None, None
    try:
        return parse_time_string(m.group(1)), parse_time_string(m.group(2))
    except Exception:
        return None, None


def parse_weekly_structured(text):
    m = re.search(r'[Сс]принт\s+(\d{1,2}\.\d{1,2})(?:\.\d{2,4})?\s*[-–—]\s*(\d{1,2}\.\d{1,2})(?:\.\d{2,4})?', text)
    if not m:
        return None, None

    today = date.today()

    def parse_ddmm(s):
        d, mm = s.split('.')
        cand = date(today.year, int(mm), int(d))
        if cand < today - timedelta(days=30):
            cand = date(today.year + 1, int(mm), int(d))
        return cand

    week_start = parse_ddmm(m.group(1))
    week_start = week_start - timedelta(days=week_start.weekday())

    day_map = {'ПН': 0, 'ВТ': 1, 'СР': 2, 'ЧТ': 3, 'ПТ': 4, 'СБ': 5, 'ВС': 6}
    pattern = r'(ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\s*:\s*([^\n]*)((?:\n-[^\n]*)*)'
    matches = re.findall(pattern, text)

    days_data = []
    for day_code, time_part, tasks_part in matches:
        idx = day_map.get(day_code)
        if idx is None:
            continue
        time_part = time_part.strip()
        tasks_lines = [l.strip() for l in tasks_part.split('\n') if l.strip().startswith('-')]
        tasks_text = '\n'.join(tasks_lines)

        if '—' in time_part and not re.search(r'\d', time_part):
            days_data.append({'idx': idx, 'is_day_off': True, 'plan_text': tasks_text or None})
        else:
            st, et = parse_time_range(time_part)
            if st and et:
                days_data.append({
                    'idx': idx, 'is_day_off': False,
                    'start': st, 'end': et,
                    'plan_text': tasks_text or None
                })
    return week_start, days_data


def parse_daily_structured(text):
    dm = re.search(r'(\d{2}\.\d{2}\.\d{4})', text)
    if not dm:
        return None
    try:
        d = datetime.strptime(dm.group(1), '%d.%m.%Y').date()
    except ValueError:
        return None
    st, et = parse_time_range(text)
    if not st or not et:
        return None
    em = re.search(r'[Ээ]ффективность\s*:?\s*(\d+)', text)
    eff = int(em.group(1)) if em else 100
    tasks_lines = []
    for line in text.split('\n'):
        s = line.strip()
        if s.startswith('-'):
            tasks_lines.append(s)
    tasks_text = '\n'.join(tasks_lines)
    return {'date': d, 'start': st, 'end': et, 'eff': eff, 'plan_text': tasks_text}


def extract_name_from_message(text, key):
    lines = text.split('\n')
    if key == 'расписание':
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#') or line.lower().startswith('спринт'):
                continue
            if re.search(r'^[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.?', line) or \
               re.match(r'^[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+$', line):
                return line
    elif key == 'дейли':
        found_eff = False
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if 'Эффективность' in line:
                found_eff = True
                continue
            if found_eff and re.match(r'^[А-ЯЁ][а-яё]+', line):
                return line
            if re.match(r'^[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+', line):
                return line
    return None


async def send_personal_summary(bot, user, text, reply_markup=None):
    if not user.telegram_chat_id:
        return False
    try:
        await bot.send_message(
            chat_id=user.telegram_chat_id,
            text=text,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        return True
    except Exception as e:
        logger.error(f"Не отправить личное сообщение {user.id}: {e}")
        return False


def _ser_day(day):
    if day['is_day_off']:
        return {'is_day_off': True, 'plan_text': day.get('plan_text')}
    return {
        'is_day_off': False,
        'start': day['start'].strftime('%H:%M'),
        'end': day['end'].strftime('%H:%M'),
        'plan_text': day.get('plan_text'),
    }


# ================== #расписание / #дейли ==================
async def handle_structured_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat

    if not msg or not msg.text:
        return
    if chat.type not in ('group', 'supergroup'):
        return

    text = msg.text
    if '#расписание' not in text and '#дейли' not in text:
        return

    with app.app_context():
        user = find_user_by_telegram(update)
        if not user:
            name_str = extract_name_from_message(
                text, 'расписание' if '#расписание' in text else 'дейли'
            )
            if name_str:
                user = find_user_by_name(name_str)
        if not user:
            return

        # ---------------- #расписание ----------------
        if '#расписание' in text:
            week_start, days_data = parse_weekly_structured(text)
            if not week_start or not days_data:
                await send_personal_summary(
                    context.bot, user,
                    "⚠️ Не удалось разобрать #расписание. Проверь формат."
                )
                return

            new_days = []
            conflict_days = []
            for day in days_data:
                target_date = week_start + timedelta(days=day['idx'])
                existing = Schedule.query.filter_by(user_id=user.id, date=target_date).first()
                if existing:
                    conflict_days.append((target_date, day, existing))
                else:
                    new_days.append((target_date, day))

            if not conflict_days:
                for target_date, day in new_days:
                    if day['is_day_off']:
                        s = Schedule(user_id=user.id, date=target_date,
                                     is_day_off=True, status='approved',
                                     plan_text=day.get('plan_text'))
                    else:
                        s = Schedule(user_id=user.id, date=target_date,
                                     planned_start=day['start'], planned_end=day['end'],
                                     is_day_off=False, status='approved',
                                     plan_text=day.get('plan_text'))
                    db.session.add(s)
                db.session.commit()

                personal_text = (
                    f"📋 <b>Расписание сохранено</b>\n"
                    f"📅 Неделя с {week_start.strftime('%d.%m.%Y')}\n"
                    f"✅ Дней: {len(new_days)}\n\n"
                    f"Открой «📅 Моё расписание»."
                )
                await send_personal_summary(context.bot, user, personal_text)
                return

            context.user_data['pending_week_plan'] = {
                'user_id': user.id,
                'week_start': week_start.isoformat(),
                'new_days': [
                    {'date': d.isoformat(), 'data': _ser_day(day)}
                    for d, day in new_days
                ],
                'conflict_days': [
                    {'date': d.isoformat(), 'data': _ser_day(day)}
                    for d, day, _ in conflict_days
                ],
            }

            conflict_lines = []
            for d, day, ex in conflict_days:
                if ex.is_day_off:
                    line = f"• {d.strftime('%d.%m')} — 🏖 выходной"
                elif ex.planned_start and ex.planned_end:
                    st = ex.planned_start.strftime('%H:%M')
                    en = ex.planned_end.strftime('%H:%M')
                    line = f"• {d.strftime('%d.%m')} — 📋 {st}–{en}"
                else:
                    line = f"• {d.strftime('%d.%m')} — есть запись"
                conflict_lines.append(line)
            conflict_list = "\n".join(conflict_lines)

            new_lines = []
            for d, day in new_days:
                if day['is_day_off']:
                    line = f"• {d.strftime('%d.%m')} — 🏖 выходной"
                else:
                    st = day['start'].strftime('%H:%M')
                    en = day['end'].strftime('%H:%M')
                    line = f"• {d.strftime('%d.%m')} — 📋 {st}–{en}"
                new_lines.append(line)
            new_list = "\n".join(new_lines) or "<i>нет новых</i>"

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Заменить конфликты", callback_data="wplan_replace")],
                [InlineKeyboardButton("⏭ Только новые дни", callback_data="wplan_skip")],
                [InlineKeyboardButton("❌ Отменить всё", callback_data="wplan_cancel")],
            ])

            await send_personal_summary(
                context.bot, user,
                f"⚠️ <b>На некоторые дни уже есть план:</b>\n\n{conflict_list}\n\n"
                f"<b>Новые дни:</b>\n{new_list}\n\n"
                f"Что сделать?",
                reply_markup=kb
            )
            return

        # ---------------- #дейли ----------------
        if '#дейли' in text:
            daily = parse_daily_structured(text)
            if not daily:
                await send_personal_summary(
                    context.bot, user,
                    "⚠️ Не удалось разобрать #дейли. Проверь формат."
                )
                return

            existing = Attendance.query.filter_by(user_id=user.id, date=daily['date']).first()
            schedule = Schedule.query.filter_by(
                user_id=user.id, date=daily['date'], status='approved'
            ).first()
            early_start = 0
            if schedule and schedule.planned_start and not schedule.is_day_off:
                early_start = time_to_minutes(daily['start']) - time_to_minutes(schedule.planned_start)
            status = 'confirmed' if daily['date'] == date.today() else 'pending'

            if existing:
                context.user_data['pending_daily'] = {
                    'user_id': user.id,
                    'date': daily['date'].isoformat(),
                    'start': daily['start'].strftime('%H:%M'),
                    'end': daily['end'].strftime('%H:%M'),
                    'eff': daily['eff'],
                    'plan_text': daily.get('plan_text'),
                    'early_start': early_start,
                    'status': status,
                }

                existing_text = (
                    f"⏰ {existing.actual_start.strftime('%H:%M')}–{existing.actual_end.strftime('%H:%M')}\n"
                    f"📈 e% {int(existing.efficiency * 100)}"
                )
                new_text = (
                    f"⏰ {daily['start'].strftime('%H:%M')}–{daily['end'].strftime('%H:%M')}, "
                    f"e% {daily['eff']}"
                )

                if user.telegram_chat_id:
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("✏️ Заменить", callback_data="daily_replace")],
                        [InlineKeyboardButton("❌ Оставить старое", callback_data="daily_keep")],
                    ])
                    try:
                        await context.bot.send_message(
                            chat_id=user.telegram_chat_id,
                            text=(
                                f"⚠️ На <b>{daily['date'].strftime('%d.%m.%Y')}</b> "
                                f"уже есть отметка:\n\n{existing_text}\n\n"
                                f"<b>Новая:</b>\n{new_text}\n\nЗаменить?"
                            ),
                            parse_mode='HTML',
                            reply_markup=kb
                        )
                    except Exception as e:
                        logger.error(f"Не отправить кнопки дейли: {e}")
                return

            att = Attendance(
                user_id=user.id, date=daily['date'],
                actual_start=daily['start'], actual_end=daily['end'],
                efficiency=daily['eff'] / 100, early_start=early_start,
                note=daily.get('plan_text'), status=status
            )
            db.session.add(att)
            db.session.commit()

            personal_text = (
                f"📊 <b>Дейли сохранён</b>\n"
                f"📅 {daily['date'].strftime('%d.%m.%Y')}\n"
                f"⏰ {daily['start'].strftime('%H:%M')} – {daily['end'].strftime('%H:%M')}\n"
                f"📈 e% {daily['eff']}\n"
                f"📌 {('✅ подтверждено' if status == 'confirmed' else '⏳ ожидает')}\n"
            )
            if daily.get('plan_text'):
                personal_text += f"\n<b>Что сделал:</b>\n{esc(daily['plan_text'][:800])}\n"
            await send_personal_summary(context.bot, user, personal_text)
            return


# ================== КОНФЛИКТЫ #расписание / #дейли ==================
async def weekly_plan_conflict_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data.get('pending_week_plan')
    if not data:
        await query.edit_message_text("❌ Данные устарели, пришли #расписание заново.")
        return

    action = query.data
    user_id = data['user_id']

    if action == "wplan_cancel":
        context.user_data.pop('pending_week_plan', None)
        await query.edit_message_text("Отменено. Ничего не сохранено.")
        return

    with app.app_context():
        user = User.query.get(user_id)
        if not user:
            context.user_data.pop('pending_week_plan', None)
            await query.edit_message_text("❌ Пользователь не найден.")
            return

        saved_new = 0
        for item in data['new_days']:
            d = datetime.strptime(item['date'], '%Y-%m-%d').date()
            day = item['data']
            if day['is_day_off']:
                s = Schedule(user_id=user.id, date=d, is_day_off=True,
                             status='approved', plan_text=day.get('plan_text'))
            else:
                s = Schedule(user_id=user.id, date=d,
                             planned_start=parse_time_string(day['start']),
                             planned_end=parse_time_string(day['end']),
                             is_day_off=False, status='approved',
                             plan_text=day.get('plan_text'))
            db.session.add(s)
            saved_new += 1

        replaced = 0
        if action == "wplan_replace":
            for item in data['conflict_days']:
                d = datetime.strptime(item['date'], '%Y-%m-%d').date()
                day = item['data']
                existing = Schedule.query.filter_by(user_id=user.id, date=d).first()
                if not existing:
                    continue
                if day['is_day_off']:
                    existing.is_day_off = True
                    existing.planned_start = None
                    existing.planned_end = None
                    existing.plan_text = day.get('plan_text')
                    existing.status = 'approved'
                else:
                    existing.is_day_off = False
                    existing.planned_start = parse_time_string(day['start'])
                    existing.planned_end = parse_time_string(day['end'])
                    existing.plan_text = day.get('plan_text')
                    existing.status = 'approved'
                replaced += 1

        db.session.commit()

    context.user_data.pop('pending_week_plan', None)
    if action == "wplan_replace":
        await query.edit_message_text(
            f"✅ Сохранено новых: {saved_new}\n♻️ Заменено: {replaced}"
        )
    else:
        await query.edit_message_text(
            f"✅ Сохранено новых: {saved_new}\n⏭ Конфликты пропущены."
        )


async def daily_conflict_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data.get('pending_daily')
    if not data:
        await query.edit_message_text("❌ Данные устарели. Пришли #дейли заново.")
        return

    if query.data == "daily_keep":
        context.user_data.pop('pending_daily', None)
        await query.edit_message_text("Оставлено старое значение.")
        return

    if query.data == "daily_replace":
        with app.app_context():
            d = datetime.strptime(data['date'], '%Y-%m-%d').date()
            existing = Attendance.query.filter_by(
                user_id=data['user_id'], date=d
            ).first()
            if not existing:
                context.user_data.pop('pending_daily', None)
                await query.edit_message_text("❌ Запись уже удалена.")
                return
            existing.actual_start = parse_time_string(data['start'])
            existing.actual_end = parse_time_string(data['end'])
            existing.efficiency = data['eff'] / 100
            existing.early_start = data['early_start']
            existing.note = data.get('plan_text')
            existing.status = data['status']
            db.session.commit()
        context.user_data.pop('pending_daily', None)
        await query.edit_message_text(
            f"✅ Заменено на {data['start']}–{data['end']}, e% {data['eff']}"
        )


# ================== СТАРТ / РЕГИСТРАЦИЯ ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if user:
            await update.message.reply_text(
                f"👋 С возвращением, <b>{esc(user.full_name)}</b>!\n\nВыбери действие 👇",
                parse_mode='HTML',
                reply_markup=main_menu_keyboard()
            )
            return
    context.user_data['state'] = 'ask_email'
    await update.message.reply_text(
        "👋 Привет! Я — бот <b>WorkTracker</b>.\n\n"
        "Чтобы привязать Telegram к учётной записи,\n"
        "пришли свой email (тот, под которым ты входишь на сайт).",
        parse_mode='HTML',
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
                f"👋 С возвращением, <b>{esc(current.full_name)}</b>!",
                parse_mode='HTML',
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
                "⚠️ Этот email уже привязан к другому Telegram-аккаунту."
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
        f"✅ Отлично, <b>{esc(user.full_name)}</b>!\nАккаунт привязан.\n\nВыбери действие 👇",
        parse_mode='HTML',
        reply_markup=main_menu_keyboard()
    )


# ================== ПРОСТЫЕ КОМАНДЫ ==================
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


async def cmd_all_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        offset = context.user_data.get('all_week_offset', 0)
        with app.app_context():
            buf = generate_all_week_image(offset)
        today = date.today()
        start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        await update.message.reply_photo(
            photo=buf,
            caption=f"👥 <b>Расписание всех сотрудников</b>\nНеделя с {start_of_week.strftime('%d.%m.%Y')}",
            parse_mode='HTML'
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⬅️", callback_data="allweek_prev"),
                InlineKeyboardButton("Текущая", callback_data="allweek_now"),
                InlineKeyboardButton("➡️", callback_data="allweek_next"),
            ]
        ])
        await update.message.reply_text("Навигация по неделям:", reply_markup=kb)
    except Exception as e:
        logger.exception(f"cmd_all_week: {e}")
        await update.message.reply_text("⚠️ Не удалось сформировать картинку.",
                                        reply_markup=main_menu_keyboard())


async def all_week_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        if query.data == "allweek_prev":
            context.user_data['all_week_offset'] = context.user_data.get('all_week_offset', 0) - 1
        elif query.data == "allweek_next":
            context.user_data['all_week_offset'] = context.user_data.get('all_week_offset', 0) + 1
        else:
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
    except Exception as e:
        logger.exception(f"all_week_callback: {e}")


# ================== «МОЁ РАСПИСАНИЕ» ==================
def _schedule_week_view_data(user, week_start, week_offset):
    end_of_week = week_start + timedelta(days=6)
    today = date.today()

    text = "📅 <b>Моё расписание</b>\n"
    text += f"<i>{week_start.strftime('%d.%m')} – {end_of_week.strftime('%d.%m.%Y')}</i>\n"
    text += SEP

    for i in range(7):
        d = week_start + timedelta(days=i)
        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
        att = Attendance.query.filter(
            Attendance.user_id == user.id,
            Attendance.date == d,
            Attendance.status != 'rejected'
        ).first()
        if sch and sch.status == 'rejected':
            sch = None

        is_today = (d == today)
        day_short = DAYS_RU_SHORT[i]

        if sch and sch.is_day_off:
            icon = "🏖"
        elif is_today:
            icon = "🔸"
        elif (sch and sch.planned_start) or (att and att.actual_start):
            icon = "📌"
        else:
            icon = "▫️"

        text += "\n"
        header = f"{icon} <b>{day_short} {d.strftime('%d.%m')}</b>"
        if is_today:
            header += " · <i>сегодня</i>"
        text += header + "\n"

        if sch and sch.is_day_off:
            text += "🏖 Выходной день\n"
            continue

        has_any = False

        if sch and sch.planned_start and sch.planned_end:
            st = sch.planned_start.strftime('%H:%M')
            en = sch.planned_end.strftime('%H:%M')
            text += f"📋 План: <b>{st} – {en}</b>\n"
            has_any = True

        if att and att.actual_start and att.actual_end:
            st = att.actual_start.strftime('%H:%M')
            en = att.actual_end.strftime('%H:%M')
            eff = int(att.efficiency * 100)
            text += f"⏰ Факт: <b>{st} – {en}</b>  ·  e% <b>{eff}</b>\n"
            has_any = True

        if sch and sch.plan_text:
            short = _tasks_short(sch.plan_text, 220)
            text += f"📝 {esc(short)}\n"
            has_any = True

        if att and att.note:
            short = _tasks_short(att.note, 220)
            text += f"✅ {esc(short)}\n"
            has_any = True

        if not has_any:
            text += "<i>свободно</i>\n"

    text += f"\n{SEP}\n"
    text += "👇 Нажми на день ниже, чтобы изменить."

    kb = schedule_week_keyboard(week_start, user, week_offset)
    rows = list(kb.inline_keyboard)
    rows.append([InlineKeyboardButton("🖼 Показать картинкой", callback_data="mysch_image")])
    rows.append([InlineKeyboardButton("🗑 Удалить всю неделю",
    callback_data=f"mysch_clearweek_{week_start.isoformat()}")])
    return text, InlineKeyboardMarkup(rows)


def _schedule_day_view_data(user, d):
    sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
    att = Attendance.query.filter(
        Attendance.user_id == user.id,
        Attendance.date == d,
        Attendance.status != 'rejected'
    ).first()
    if sch and sch.status == 'rejected':
        sch = None

    is_today = (d == date.today())
    day_name = DAYS_RU_FULL[d.weekday()]

    text = f"📅 <b>{day_name}, {d.strftime('%d.%m.%Y')}</b>"
    if is_today:
        text += " · 🔸 <i>сегодня</i>"
    text += f"\n{SEP}\n\n"

    # План
    text += "📋 <b>План</b>\n"
    if sch and sch.is_day_off:
        text += "🏖 Выходной день\n"
    elif sch and sch.planned_start and sch.planned_end:
        st = sch.planned_start.strftime('%H:%M')
        en = sch.planned_end.strftime('%H:%M')
        text += f"🕐 <b>{st} – {en}</b>\n"
    else:
        text += "<i>не заполнен</i>\n"

    plan_tasks = _task_lines(sch.plan_text) if sch and sch.plan_text else []
    if plan_tasks:
        text += "\n📝 <b>Задачи на день</b>\n"
        for t in plan_tasks:
            text += f"   • {esc(t)}\n"

    text += f"\n{SEP}\n\n"

    # Факт
    text += "⏰ <b>Факт</b>\n"
    if att and att.actual_start and att.actual_end:
        st = att.actual_start.strftime('%H:%M')
        en = att.actual_end.strftime('%H:%M')
        eff = int(att.efficiency * 100)
        status = STATUS_RU.get(att.status, att.status)
        text += f"🕐 <b>{st} – {en}</b>\n"
        text += f"📈 Эффективность: <b>{eff}%</b>\n"
        text += f"🎯 {status}\n"

        done_tasks = _task_lines(att.note) if att.note else []
        if done_tasks:
            text += "\n✅ <b>Что сделал</b>\n"
            for t in done_tasks:
                text += f"   • {esc(t)}\n"
    else:
        text += "<i>нет данных</i>\n"

    kb = schedule_day_keyboard(d, sch, att)
    return text, kb


async def cmd_my_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Отправь /start и привяжи аккаунт.")
            return
        offset = context.user_data.get('sched_week_offset', 0)
        today = date.today()
        week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        text, kb = _schedule_week_view_data(user, week_start, offset)

    msg = await update.message.reply_text(text, parse_mode='HTML', reply_markup=kb)
    context.user_data['sched_day_msg_id'] = msg.message_id


async def sched_week_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat_id

    if data == "sched_noop":
        await query.answer()
        return

    try:
        offset = int(data.replace("sched_week_", ""))
    except ValueError:
        await query.answer()
        return

    context.user_data['sched_week_offset'] = offset
    today = date.today()
    week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.answer("Нет привязки", show_alert=True)
            return
        text, kb = _schedule_week_view_data(user, week_start, offset)

    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)


async def sched_day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat_id
    ds = data.replace("sched_day_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await query.answer()
        return

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.answer("Нет привязки", show_alert=True)
            return
        text, kb = _schedule_day_view_data(user, d)

    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)


# --- предупреждение перед изменением заполненного дня ---
async def _warn_before_edit(query, context, d, action):
    chat_id = query.message.chat_id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.answer("Нет привязки", show_alert=True)
            return
        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
        if sch and sch.status != 'rejected':
            if sch.is_day_off:
                current = "🏖 выходной"
            elif sch.planned_start and sch.planned_end:
                st = sch.planned_start.strftime('%H:%M')
                en = sch.planned_end.strftime('%H:%M')
                current = f"📋 {st} – {en}"
            else:
                current = "есть запись"
            if sch.plan_text:
                current += f"\n📝 {esc(_tasks_short(sch.plan_text, 200))}"
        else:
            current = "—"

        context.user_data['pending_sched_edit'] = {
            'action': action,
            'date': d.isoformat(),
            'user_id': user.id,
        }
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Продолжить", callback_data=f"sched_warn_yes_{action}")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"sched_warn_no_{action}")],
        ])

    await query.answer()
    msg = (
        f"⚠️ На <b>{d.strftime('%d.%m.%Y')}</b> уже есть план:\n\n{current}\n\n"
        f"Продолжить редактирование?"
    )
    try:
        await query.edit_message_text(msg, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(msg, parse_mode='HTML', reply_markup=kb)


async def sched_warn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    m = re.match(r'^sched_warn_(yes|no)_(\w+)$', data)
    if not m:
        await query.answer()
        return
    decision, action = m.group(1), m.group(2)
    pending = context.user_data.get('pending_sched_edit')
    if not pending:
        await query.answer("Данные устарели", show_alert=True)
        return

    d = datetime.strptime(pending['date'], '%Y-%m-%d').date()
    ds = pending['date']

    if decision == 'no':
        context.user_data.pop('pending_sched_edit', None)
        chat_id = query.message.chat_id
        with app.app_context():
            user = get_user_by_chat(chat_id)
            if user:
                text, kb = _schedule_day_view_data(user, d)
            else:
                text, kb = "❌ Нет привязки", None
        await query.answer("Отменено")
        try:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)
        return

    context.user_data.pop('pending_sched_edit', None)
    await query.answer("Продолжаем")

    if action == 'time':
        context.user_data['sched_edit_date'] = ds
        context.user_data['state'] = 'sched_edit_start'
        msg = (
            f"🕐 <b>Плановое время — {d.strftime('%d.%m.%Y')}</b>\n\n"
            f"Введи <b>время прихода</b> в формате <code>ЧЧ:ММ</code>\n"
            f"Например: <code>10:00</code>"
        )
        try:
            await query.edit_message_text(msg, parse_mode='HTML')
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.chat.send_message(msg, parse_mode='HTML')
        await query.message.chat.send_message("Жду время прихода 👇",
                                              reply_markup=cancel_keyboard())
    elif action == 'tasks':
        await show_tasks_menu(query, context, d, ds)
    elif action == 'dayoff':
        chat_id = query.message.chat_id
        with app.app_context():
            user = get_user_by_chat(chat_id)
            if not user:
                return
            sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
            if sch:
                sch.is_day_off = True
                sch.planned_start = None
                sch.planned_end = None
                sch.status = 'approved'
            else:
                sch = Schedule(user_id=user.id, date=d, is_day_off=True, status='approved')
                db.session.add(sch)
            db.session.commit()
            text, kb = _schedule_day_view_data(user, d)
        try:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)


# --- задачи на день (план) ---
async def show_tasks_menu(query, context, d, ds):
    chat_id = query.message.chat_id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            return
        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
        tasks = _task_lines(sch.plan_text) if sch and sch.plan_text else []

    context.user_data['sched_edit_date'] = ds

    if tasks:
        text = f"📝 <b>Задачи на день — {d.strftime('%d.%m.%Y')}</b>\n{SEP}\n\n"
        for i, t in enumerate(tasks, 1):
            text += f"{i}. {esc(t)}\n"
        text += "\n<b>Что сделать?</b>"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить задачи", callback_data="sched_tasks_add")],
            [InlineKeyboardButton("✏️ Переписать полностью", callback_data="sched_tasks_replace")],
            [InlineKeyboardButton("🗑 Очистить всё", callback_data="sched_tasks_clear")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"sched_day_{ds}")],
        ])
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        await query.answer()
        return

    # Нет задач → сразу ввод
    context.user_data['sched_tasks_mode'] = 'replace'
    context.user_data['state'] = 'sched_edit_tasks'
    msg = (
        f"📝 <b>Задачи на день — {d.strftime('%d.%m.%Y')}</b>\n{SEP}\n\n"
        f"Отправь список задач — каждая с новой строки, начиная с «-».\n\n"
        f"<b>Пример:</b>\n"
        f"<code>- Созвон с клиентом\n"
        f"- Доделать отчёт</code>"
    )
    try:
        await query.edit_message_text(msg, parse_mode='HTML')
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(msg, parse_mode='HTML')
    await query.message.chat.send_message("Жду список задач 👇",
                                          reply_markup=cancel_keyboard())
    await query.answer()


async def sched_tasks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ds = query.data.replace("sched_tasks_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await query.answer()
        return
    await show_tasks_menu(query, context, d, ds)


async def sched_tasks_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    ds = context.user_data.get('sched_edit_date')
    if not ds:
        await query.answer("Сессия устарела", show_alert=True)
        return
    d = datetime.strptime(ds, '%Y-%m-%d').date()
    chat_id = query.message.chat_id

    if data == "sched_tasks_clear":
        with app.app_context():
            user = get_user_by_chat(chat_id)
            if not user:
                return
            sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
            if sch:
                sch.plan_text = None
                db.session.commit()
            text, kb = _schedule_day_view_data(user, d)
        await query.answer("🗑 Задачи очищены")
        try:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)
        return

    # Показываем текущие задачи перед вводом
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            return
        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
        current_tasks = _task_lines(sch.plan_text) if sch and sch.plan_text else []

    if data == "sched_tasks_add":
        context.user_data['sched_tasks_mode'] = 'add'
        context.user_data['state'] = 'sched_edit_tasks'
        title = "➕ <b>Добавить задачи</b>"
        hint = "Новые задачи добавятся <b>к текущему списку</b>."
    elif data == "sched_tasks_replace":
        context.user_data['sched_tasks_mode'] = 'replace'
        context.user_data['state'] = 'sched_edit_tasks'
        title = "✏️ <b>Переписать задачи</b>"
        hint = "Старые задачи будут <b>удалены</b>."
    else:
        return

    await query.answer()

    text = f"{title}\n📅 {d.strftime('%d.%m.%Y')}\n{SEP}\n\n"
    if current_tasks:
        text += "<b>Сейчас в плане:</b>\n"
        for t in current_tasks:
            text += f"   • {esc(t)}\n"
        text += "\n"
    else:
        text += "<i>Пока задач нет.</i>\n\n"

    text += hint + "\n\n"
    text += "Отправь задачи — каждая с новой строки, начиная с «-».\n"
    text += "<i>Например:</i> <code>- Созвон с клиентом</code>"

    try:
        await query.edit_message_text(text, parse_mode='HTML')
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(text, parse_mode='HTML')

    await query.message.chat.send_message("Жду список 👇", reply_markup=cancel_keyboard())


# --- «Что сделал» (факт) ---
async def sched_note_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ds = query.data.replace("sched_note_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await query.answer()
        return

    chat_id = query.message.chat_id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.answer("Нет привязки", show_alert=True)
            return
        att = Attendance.query.filter_by(user_id=user.id, date=d).first()
        if not att:
            await query.answer()
            msg = (
                f"⚠️ На <b>{d.strftime('%d.%m.%Y')}</b> ещё нет факта.\n\n"
                f"Сначала добавь фактическое время через\n"
                f"«⏰ Фактическое время» или отправь <code>#дейли</code> в группе."
            )
            try:
                await query.edit_message_text(msg, parse_mode='HTML',
                                              reply_markup=InlineKeyboardMarkup([
                                                  [InlineKeyboardButton("⬅️ Назад",
                                                                        callback_data=f"sched_day_{ds}")],
                                              ]))
            except Exception:
                pass
            return
        note_lines = _task_lines(att.note) if att.note else []

    context.user_data['sched_edit_date'] = ds
    context.user_data['sched_edit_kind'] = 'note'

    if note_lines:
        text = f"✅ <b>Что сделал — {d.strftime('%d.%m.%Y')}</b>\n{SEP}\n\n"
        for i, t in enumerate(note_lines, 1):
            text += f"{i}. {esc(t)}\n"
        text += "\n<b>Что сделать?</b>"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Добавить", callback_data="sched_note_add")],
            [InlineKeyboardButton("✏️ Переписать", callback_data="sched_note_replace")],
            [InlineKeyboardButton("🗑 Очистить", callback_data="sched_note_clear")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"sched_day_{ds}")],
        ])
        await query.answer()
        try:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)
        return

    context.user_data['sched_tasks_mode'] = 'replace'
    context.user_data['state'] = 'sched_edit_note'
    await query.answer()
    msg = (
        f"✅ <b>Что сделал — {d.strftime('%d.%m.%Y')}</b>\n{SEP}\n\n"
        f"Отправь список — каждая строка с новой, начиная с «-».\n\n"
        f"<b>Пример:</b>\n"
        f"<code>- Доработал парсер\n"
        f"- Провёл встречу</code>"
    )
    try:
        await query.edit_message_text(msg, parse_mode='HTML')
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(msg, parse_mode='HTML')
    await query.message.chat.send_message("Жду список 👇", reply_markup=cancel_keyboard())


async def sched_note_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    ds = context.user_data.get('sched_edit_date')
    if not ds:
        await query.answer("Сессия устарела", show_alert=True)
        return
    d = datetime.strptime(ds, '%Y-%m-%d').date()
    chat_id = query.message.chat_id

    if data == "sched_note_clear":
        with app.app_context():
            user = get_user_by_chat(chat_id)
            if not user:
                return
            att = Attendance.query.filter_by(user_id=user.id, date=d).first()
            if att:
                att.note = None
                db.session.commit()
            text, kb = _schedule_day_view_data(user, d)
        await query.answer("🗑 Очищено")
        try:
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)
        return

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            return
        att = Attendance.query.filter_by(user_id=user.id, date=d).first()
        current = _task_lines(att.note) if att and att.note else []

    if data == "sched_note_add":
        context.user_data['sched_tasks_mode'] = 'add'
        context.user_data['state'] = 'sched_edit_note'
        context.user_data['sched_edit_kind'] = 'note'
        title = "➕ <b>Добавить в «что сделал»</b>"
        hint = "Новые строки добавятся <b>к текущему списку</b>."
    elif data == "sched_note_replace":
        context.user_data['sched_tasks_mode'] = 'replace'
        context.user_data['state'] = 'sched_edit_note'
        context.user_data['sched_edit_kind'] = 'note'
        title = "✏️ <b>Переписать «что сделал»</b>"
        hint = "Старые строки будут <b>удалены</b>."
    else:
        return

    await query.answer()

    text = f"{title}\n📅 {d.strftime('%d.%m.%Y')}\n{SEP}\n\n"
    if current:
        text += "<b>Сейчас отмечено:</b>\n"
        for t in current:
            text += f"   • {esc(t)}\n"
        text += "\n"
    else:
        text += "<i>Пока пусто.</i>\n\n"
    text += hint + "\n\n"
    text += "Отправь строки, каждая с новой, начиная с «-»."

    try:
        await query.edit_message_text(text, parse_mode='HTML')
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(text, parse_mode='HTML')
    await query.message.chat.send_message("Жду список 👇", reply_markup=cancel_keyboard())


# --- выходной ---
async def sched_dayoff_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ds = query.data.replace("sched_dayoff_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await query.answer()
        return

    chat_id = query.message.chat_id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.answer("Нет привязки", show_alert=True)
            return
        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()

    if sch and sch.status != 'rejected' and not sch.is_day_off:
        await _warn_before_edit(query, context, d, 'dayoff')
        return

    with app.app_context():
        sch2 = Schedule.query.filter_by(user_id=user.id, date=d).first()
        if sch2:
            sch2.is_day_off = True
            sch2.planned_start = None
            sch2.planned_end = None
            sch2.status = 'approved'
        else:
            sch2 = Schedule(user_id=user.id, date=d, is_day_off=True, status='approved')
            db.session.add(sch2)
        db.session.commit()
        text, kb = _schedule_day_view_data(user, d)
    await query.answer("🏖 Отмечено выходным")
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)


# --- ПОЛНАЯ очистка дня ---
async def sched_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ds = query.data.replace("sched_clear_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await query.answer()
        return

    chat_id = query.message.chat_id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.answer("Нет привязки", show_alert=True)
            return
        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
        att = Attendance.query.filter_by(user_id=user.id, date=d).first()

    parts = []
    if sch:
        if sch.is_day_off:
            parts.append("🏖 запись выходного")
        elif sch.planned_start and sch.planned_end:
            st = sch.planned_start.strftime('%H:%M')
            en = sch.planned_end.strftime('%H:%M')
            parts.append(f"📋 плановое время ({st}–{en})")
        if sch.plan_text:
            parts.append("📝 задачи на день")
    if att:
        if att.actual_start and att.actual_end:
            st = att.actual_start.strftime('%H:%M')
            en = att.actual_end.strftime('%H:%M')
            parts.append(f"⏰ факт ({st}–{en})")
        if att.note:
            parts.append("✅ что сделал")

    if not parts:
        await query.answer("Нечего удалять", show_alert=True)
        return

    summary = "\n".join(f"   • {p}" for p in parts)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить всё", callback_data=f"sched_clear_yes_{ds}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"sched_day_{ds}")],
    ])
    text = (
        f"🗑 <b>Очистить день полностью?</b>\n\n"
        f"📅 <b>{d.strftime('%d.%m.%Y')}</b>\n{SEP}\n\n"
        f"<b>Будет удалено:</b>\n{summary}"
    )
    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)

async def sched_week_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления всей недели."""
    query = update.callback_query
    raw = query.data.replace("mysch_clearweek_", "")
    try:
        week_start = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        await query.answer()
        return

    week_end = week_start + timedelta(days=6)
    chat_id = query.message.chat_id

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.answer("Нет привязки", show_alert=True)
            return

        plans = Schedule.query.filter(
            Schedule.user_id == user.id,
            Schedule.date >= week_start,
            Schedule.date <= week_end
        ).all()

        facts = Attendance.query.filter(
            Attendance.user_id == user.id,
            Attendance.date >= week_start,
            Attendance.date <= week_end
        ).all()

    plan_count = len(plans)
    fact_count = len(facts)
    plan_tasks = sum(1 for p in plans if p.plan_text)
    fact_notes = sum(1 for f in facts if f.note)

    if plan_count == 0 and fact_count == 0:
        await query.answer("На эту неделю нет записей", show_alert=True)
        return

    lines = []
    if plan_count:
        lines.append(f"   • 📋 планов на дни: <b>{plan_count}</b>")
    if plan_tasks:
        lines.append(f"   • 📝 дней с задачами: <b>{plan_tasks}</b>")
    if fact_count:
        lines.append(f"   • ⏰ фактов: <b>{fact_count}</b>")
    if fact_notes:
        lines.append(f"   • ✅ записей «что сделал»: <b>{fact_notes}</b>")

    summary = "\n".join(lines)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить всю неделю",
                              callback_data=f"mysch_clearweek_yes_{raw}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="sched_week_0")],
    ])
    text = (
        f"🗑 <b>Удалить всю неделю?</b>\n"
        f"{SEP}\n\n"
        f"📅 <b>{week_start.strftime('%d.%m.%Y')} – {week_end.strftime('%d.%m.%Y')}</b>\n\n"
        f"<b>Будет удалено:</b>\n{summary}\n\n"
        f"<i>Это действие нельзя отменить.</i>"
    )
    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)


async def sched_week_clear_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление всей недели — планы и факты."""
    query = update.callback_query
    raw = query.data.replace("mysch_clearweek_yes_", "")
    try:
        week_start = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        await query.answer()
        return

    week_end = week_start + timedelta(days=6)
    chat_id = query.message.chat_id

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.answer("Нет привязки", show_alert=True)
            return

        deleted_plans = Schedule.query.filter(
            Schedule.user_id == user.id,
            Schedule.date >= week_start,
            Schedule.date <= week_end
        ).delete(synchronize_session=False)

        deleted_facts = Attendance.query.filter(
            Attendance.user_id == user.id,
            Attendance.date >= week_start,
            Attendance.date <= week_end
        ).delete(synchronize_session=False)

        db.session.commit()

        offset = context.user_data.get('sched_week_offset', 0)
        today = date.today()
        cur_week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        text, kb = _schedule_week_view_data(user, cur_week_start, offset)

    await query.answer(
        f"🗑 Удалено: планов {deleted_plans}, фактов {deleted_facts}",
        show_alert=False
    )
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)

async def sched_clear_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ds = query.data.replace("sched_clear_yes_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await query.answer()
        return

    chat_id = query.message.chat_id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.answer("Нет привязки", show_alert=True)
            return
        Schedule.query.filter_by(user_id=user.id, date=d).delete()
        Attendance.query.filter_by(user_id=user.id, date=d).delete()
        db.session.commit()
        text, kb = _schedule_day_view_data(user, d)

    await query.answer("🗑 День полностью очищен")
    try:
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)


# --- плановое время ---
async def sched_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ds = query.data.replace("sched_time_", "")
    try:
        d = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        await query.answer()
        return

    chat_id = query.message.chat_id
    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.answer("Нет привязки", show_alert=True)
            return
        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()

    if sch and sch.status != 'rejected':
        await _warn_before_edit(query, context, d, 'time')
        return

    context.user_data['sched_edit_date'] = ds
    context.user_data['state'] = 'sched_edit_start'
    await query.answer()
    msg = (
        f"🕐 <b>Плановое время — {d.strftime('%d.%m.%Y')}</b>\n\n"
        f"Введи <b>время прихода</b> в формате <code>ЧЧ:ММ</code>\n"
        f"Например: <code>10:00</code>"
    )
    try:
        await query.edit_message_text(msg, parse_mode='HTML')
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.message.chat.send_message(msg, parse_mode='HTML')
    await query.message.chat.send_message("Жду время прихода 👇", reply_markup=cancel_keyboard())


# --- «Моё расписание»: текстом / картинкой ---
async def mysch_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            return
        offset = context.user_data.get('sched_week_offset', 0)
        today = date.today()
        week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        end_of_week = week_start + timedelta(days=6)

        if query.data == "mysch_image":
            try:
                buf = generate_my_week_image(user, offset)
            except Exception as e:
                logger.exception(f"Ошибка картинки: {e}")
                return
            try:
                await query.message.delete()
            except Exception:
                pass
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️", callback_data="mysch_img_prev"),
                    InlineKeyboardButton("Текущая", callback_data="mysch_img_now"),
                    InlineKeyboardButton("➡️", callback_data="mysch_img_next"),
                ],
                [InlineKeyboardButton("📄 Показать текстом", callback_data="mysch_text")],
            ])
            await query.message.chat.send_photo(
                photo=buf,
                caption=(f"📅 <b>Моё расписание</b>\n"
                         f"{week_start.strftime('%d.%m')} – {end_of_week.strftime('%d.%m.%Y')}"),
                parse_mode='HTML',
                reply_markup=kb
            )
        elif query.data == "mysch_text":
            text, kb = _schedule_week_view_data(user, week_start, offset)
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.chat.send_message(text, parse_mode='HTML', reply_markup=kb)


async def mysch_img_nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data
    offset = context.user_data.get('sched_week_offset', 0)
    if data.endswith('prev'):
        offset -= 1
    elif data.endswith('next'):
        offset += 1
    else:
        offset = 0
    context.user_data['sched_week_offset'] = offset

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            return
        today = date.today()
        week_start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        end_of_week = week_start + timedelta(days=6)
        try:
            buf = generate_my_week_image(user, offset)
        except Exception as e:
            logger.exception(f"Ошибка картинки: {e}")
            return

    try:
        await query.message.delete()
    except Exception:
        pass

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️", callback_data="mysch_img_prev"),
            InlineKeyboardButton("Текущая", callback_data="mysch_img_now"),
            InlineKeyboardButton("➡️", callback_data="mysch_img_next"),
        ],
        [InlineKeyboardButton("📄 Показать текстом", callback_data="mysch_text")],
    ])
    await query.message.chat.send_photo(
        photo=buf,
        caption=(f"📅 <b>Моё расписание</b>\n"
                 f"{week_start.strftime('%d.%m')} – {end_of_week.strftime('%d.%m.%Y')}"),
        parse_mode='HTML',
        reply_markup=kb
    )


# ================== ВВОД ДЛЯ РАСПИСАНИЯ ==================
async def sched_edit_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return
    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        st = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text(
            "❌ Введи как <code>10:00</code>",
            parse_mode='HTML', reply_markup=cancel_keyboard()
        )
        return

    context.user_data['sched_edit_time_start'] = st.strftime('%H:%M')
    context.user_data['state'] = 'sched_edit_end'
    await update.message.reply_text(
        f"🕐 Приход: <b>{st.strftime('%H:%M')}</b>\n\n"
        f"Теперь введи <b>время ухода</b>:",
        parse_mode='HTML', reply_markup=cancel_keyboard()
    )


async def sched_edit_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return
    try:
        h, m = text.split(':')
        if len(h) == 1:
            h = '0' + h
        et = parse_time_string(f"{h}:{m}")
    except Exception:
        await update.message.reply_text(
            "❌ Введи как <code>18:00</code>",
            parse_mode='HTML', reply_markup=cancel_keyboard()
        )
        return

    start_str = context.user_data.get('sched_edit_time_start')
    st = parse_time_string(start_str)
    if st >= et:
        await update.message.reply_text(
            "❌ Время ухода должно быть позже прихода.",
            reply_markup=cancel_keyboard()
        )
        return

    ds = context.user_data.get('sched_edit_date')
    chat_id = update.effective_chat.id

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Нет привязки.")
            return
        d = datetime.strptime(ds, '%Y-%m-%d').date()
        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
        if sch:
            sch.planned_start = st
            sch.planned_end = et
            sch.is_day_off = False
            sch.status = 'approved'
        else:
            sch = Schedule(user_id=user.id, date=d,
                           planned_start=st, planned_end=et,
                           is_day_off=False, status='approved')
            db.session.add(sch)
        db.session.commit()
        text_view, kb = _schedule_day_view_data(user, d)

    clear_state(context)
    await update.message.reply_text(
        f"✅ Сохранено: <b>{st.strftime('%H:%M')} – {et.strftime('%H:%M')}</b>",
        parse_mode='HTML'
    )
    await update.message.reply_text(text_view, parse_mode='HTML', reply_markup=kb)


async def sched_edit_tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод задач на день (Schedule.plan_text)."""
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    ds = context.user_data.get('sched_edit_date')
    mode = context.user_data.get('sched_tasks_mode', 'replace')
    chat_id = update.effective_chat.id

    if ds is None:
        clear_state(context)
        await update.message.reply_text("❌ Сессия устарела. Открой «📅 Моё расписание» заново.",
                                        reply_markup=main_menu_keyboard())
        return

    if text == '-':
        new_lines = []
    else:
        new_lines = _norm_task_lines(text)
        if not new_lines:
            await update.message.reply_text(
                "❌ Пустой ввод. Отправь хотя бы одну задачу с «-».",
                reply_markup=cancel_keyboard()
            )
            return

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Нет привязки.")
            return
        try:
            d = datetime.strptime(ds, '%Y-%m-%d').date()
        except ValueError:
            clear_state(context)
            await update.message.reply_text("❌ Некорректная дата.",
                                            reply_markup=main_menu_keyboard())
            return

        sch = Schedule.query.filter_by(user_id=user.id, date=d).first()
        existing = sch.plan_text if sch and sch.plan_text else ""

        if mode == 'add':
            existing_lines = _norm_task_lines(existing) if existing else []
            final_lines = existing_lines + new_lines
        else:
            final_lines = new_lines

        final_text = '\n'.join(final_lines) if final_lines else None

        if sch:
            sch.plan_text = final_text
        else:
            sch = Schedule(user_id=user.id, date=d, plan_text=final_text,
                           status='approved')
            db.session.add(sch)
        db.session.commit()
        text_view, kb = _schedule_day_view_data(user, d)

    clear_state(context)
    if final_text:
        await update.message.reply_text("✅ Задачи сохранены.")
    else:
        await update.message.reply_text("✅ Задачи очищены.")
    await update.message.reply_text(text_view, parse_mode='HTML', reply_markup=kb)


async def sched_edit_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ввод «что сделал» (Attendance.note)."""
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Отменено.", reply_markup=main_menu_keyboard())
        return

    ds = context.user_data.get('sched_edit_date')
    mode = context.user_data.get('sched_tasks_mode', 'replace')
    chat_id = update.effective_chat.id

    if ds is None:
        clear_state(context)
        await update.message.reply_text("❌ Сессия устарела.",
                                        reply_markup=main_menu_keyboard())
        return

    if text == '-':
        new_lines = []
    else:
        new_lines = _norm_task_lines(text)
        if not new_lines:
            await update.message.reply_text(
                "❌ Пустой ввод. Отправь хотя бы одну строку с «-».",
                reply_markup=cancel_keyboard()
            )
            return

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await update.message.reply_text("❌ Нет привязки.")
            return
        try:
            d = datetime.strptime(ds, '%Y-%m-%d').date()
        except ValueError:
            clear_state(context)
            await update.message.reply_text("❌ Некорректная дата.",
                                            reply_markup=main_menu_keyboard())
            return

        att = Attendance.query.filter_by(user_id=user.id, date=d).first()
        if not att:
            clear_state(context)
            await update.message.reply_text(
                "❌ На этот день нет факта.\n"
                "Сначала добавь факт через «⏰ Фактическое время».",
                reply_markup=main_menu_keyboard()
            )
            return

        existing = att.note if att.note else ""
        if mode == 'add':
            existing_lines = _norm_task_lines(existing) if existing else []
            final_lines = existing_lines + new_lines
        else:
            final_lines = new_lines

        att.note = '\n'.join(final_lines) if final_lines else None
        db.session.commit()
        text_view, kb = _schedule_day_view_data(user, d)

    clear_state(context)
    if final_lines:
        await update.message.reply_text("✅ Сохранено.")
    else:
        await update.message.reply_text("✅ Очищено.")
    await update.message.reply_text(text_view, parse_mode='HTML', reply_markup=kb)


# ================== ИТОГИ ==================
async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Мои итоги", callback_data="sum_my")],
        [InlineKeyboardButton("👥 Итоги всех (картинкой)", callback_data="sum_all")],
    ])
    await update.message.reply_text(
        "📊 <b>Итоги месяца</b>\n\nЧто показать?",
        parse_mode='HTML', reply_markup=kb
    )


async def summary_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    context.user_data['summary_offset'] = 0

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await query.edit_message_text("❌ Нет привязки.")
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
            await query.edit_message_text("🖼 Формирую картинку...")
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
        "❓ <b>Помощь по боту WorkTracker</b>\n"
        f"{SEP}\n\n"
        "📅 <b>Моё расписание</b>\n"
        "   Календарь недели. Тап по дню:\n"
        "   • 🕐 Плановое время\n"
        "   • 📝 Задачи на день (план)\n"
        "   • ✅ Что сделал (факт)\n"
        "   • 🏖 Выходной\n"
        "   • 🗑 Полная очистка дня\n\n"
        "⏰ <b>Фактическое время</b> — приход/уход/e% вручную\n"
        "📊 <b>Итоги месяца</b> — статистика\n"
        "👥 <b>Расписание всех</b> — картинкой\n"
        "📋 <b>На неделю</b> — быстрый мастер\n\n"
        f"{SEP}\n"
        "<b>В группе:</b>\n"
        "• <code>#расписание</code> — планы на неделю\n"
        "• <code>#дейли</code> — что сделал + время + e%\n\n"
        "<i>Если день уже занят — бот спросит подтверждение в личке.</i>"
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
        "⚠️ Отвязать аккаунт?\nTelegram ID будет удалён.",
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
        "✅ Аккаунт отвязан.\nЧтобы привязать снова — /start."
    )


# ================== МАСТЕР «НА НЕДЕЛЮ» ==================
async def week_plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    context.user_data['state'] = 'week_choose'

    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    next_monday = this_monday + timedelta(days=7)
    week_after = this_monday + timedelta(days=14)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"Текущая ({this_monday.strftime('%d.%m')}–{(this_monday+timedelta(days=6)).strftime('%d.%m')})",
            callback_data="week_pick_0")],
        [InlineKeyboardButton(
            f"Следующая ({next_monday.strftime('%d.%m')}–{(next_monday+timedelta(days=6)).strftime('%d.%m')})",
            callback_data="week_pick_1")],
        [InlineKeyboardButton(
            f"Через неделю ({week_after.strftime('%d.%m')}–{(week_after+timedelta(days=6)).strftime('%d.%m')})",
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
    overwrite = context.user_data.get('week_overwrite_idx') == idx

    if not overwrite:
        with app.app_context():
            chat_id = update.effective_chat.id
            user = get_user_by_chat(chat_id)
            existing = None
            if user:
                existing = Schedule.query.filter_by(user_id=user.id, date=day).first()

        if existing and existing.status != 'rejected':
            if existing.is_day_off:
                current = "🏖 выходной"
            elif existing.planned_start and existing.planned_end:
                st = existing.planned_start.strftime('%H:%M')
                en = existing.planned_end.strftime('%H:%M')
                current = f"📋 {st} – {en}"
            else:
                current = "есть запись"
            if existing.plan_text:
                current += f"\n📝 {esc(_tasks_short(existing.plan_text, 180))}"

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Перезаписать день", callback_data="week_overwrite")],
                [InlineKeyboardButton("⏭ Пропустить день", callback_data="week_skip")],
                [InlineKeyboardButton("❌ Отменить всё", callback_data="week_cancel")],
            ])
            text = (
                f"📅 <b>{DAYS_RU_FULL[idx]}, {day.strftime('%d.%m.%Y')}</b>\n"
                f"{SEP}\n\n"
                f"⚠️ На этот день уже есть план:\n{current}\n\n"
                f"Что сделать?"
            )
            if update.callback_query:
                await update.callback_query.message.reply_text(text, parse_mode='HTML', reply_markup=kb)
            else:
                await update.message.reply_text(text, parse_mode='HTML', reply_markup=kb)
            return

    await ask_week_day_choice(update, context)


async def ask_week_day_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx = context.user_data.get('week_plan_idx', 0)
    week_start = context.user_data['week_start']
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
        f"📅 <b>Планирование недели</b>\n"
        f"{SEP}\n\n"
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

    if data == "week_overwrite":
        context.user_data['week_overwrite_idx'] = idx
        await query.edit_message_text("✏️ Перезаписываем...")
        await ask_week_day_choice(update, context)
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
            'is_day_off': False, 'start': '10:00', 'end': '18:00'
        }
        context.user_data['week_plan_idx'] = idx + 1
        await query.edit_message_text(f"✅ {DAYS_RU_FULL[idx]}: 10:00–18:00")
        await ask_week_day(update, context)
        return

    if data == "week_custom":
        context.user_data['state'] = 'week_custom_start'
        await query.edit_message_text(f"📅 {DAYS_RU_FULL[idx]}: своё время")
        await query.message.reply_text(
            f"🕐 Введи <b>время прихода</b> (ЧЧ:ММ)\nНапример: <code>09:30</code>",
            parse_mode='HTML', reply_markup=cancel_keyboard()
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
        parse_mode='HTML', reply_markup=cancel_keyboard()
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
    week_start = context.user_data.get('week_start')
    data = context.user_data.get('week_plan_data', {})
    message = update.message if update.message else update.callback_query.message

    if not week_start:
        clear_state(context)
        await message.reply_text("❌ Сессия устарела.", reply_markup=main_menu_keyboard())
        return

    with app.app_context():
        user = get_user_by_chat(chat_id)
        if not user:
            await message.reply_text("❌ Привяжи аккаунт /start")
            return

        created = 0
        replaced = 0
        for idx, info in data.items():
            day = week_start + timedelta(days=idx)
            existing = Schedule.query.filter_by(user_id=user.id, date=day).first()

            if existing:
                if info.get('is_day_off'):
                    existing.is_day_off = True
                    existing.planned_start = None
                    existing.planned_end = None
                    existing.status = 'approved'
                else:
                    existing.is_day_off = False
                    existing.planned_start = parse_time_string(info['start'])
                    existing.planned_end = parse_time_string(info['end'])
                    existing.status = 'approved'
                replaced += 1
            else:
                if info.get('is_day_off'):
                    s = Schedule(user_id=user.id, date=day,
                                 is_day_off=True, status='approved')
                else:
                    s = Schedule(user_id=user.id, date=day,
                                 planned_start=parse_time_string(info['start']),
                                 planned_end=parse_time_string(info['end']),
                                 is_day_off=False, status='approved')
                db.session.add(s)
                created += 1
        db.session.commit()

    text = f"✅ Готово!\nСоздано: {created}\nПерезаписано: {replaced}"
    clear_state(context)
    await message.reply_text(text, reply_markup=main_menu_keyboard())


# ================== ФАКТ ==================
async def fact_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d = date.today() if query.data == "fact_today" else date.today() - timedelta(days=1)
    context.user_data['fact_date'] = d
    context.user_data['state'] = 'fact_start'
    await query.edit_message_text(f"📅 Дата: <b>{d.strftime('%d.%m.%Y')}</b>",
                                  parse_mode='HTML')
    await query.message.reply_text(
        "🕐 Введи <b>время прихода</b> (ЧЧ:ММ)",
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
        f"🕐 Приход: <b>{start_time.strftime('%H:%M')}</b>\n\n🕕 Введи <b>время ухода</b>",
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
            context.user_data['pending_fact'] = {
                'date': d, 'start': start_time, 'end': end_time, 'eff': eff
            }
            context.user_data['state'] = 'fact_conflict'
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Заменить", callback_data="fact_replace")],
                [InlineKeyboardButton("❌ Оставить старое", callback_data="fact_keep")],
            ])
            ex_st = existing.actual_start.strftime('%H:%M')
            ex_en = existing.actual_end.strftime('%H:%M')
            ex_eff = int(existing.efficiency * 100)
            new_st = start_time.strftime('%H:%M')
            new_en = end_time.strftime('%H:%M')
            await update.message.reply_text(
                f"⚠️ На <b>{d.strftime('%d.%m.%Y')}</b> уже есть отметка:\n\n"
                f"⏰ {ex_st}–{ex_en}, e% {ex_eff}\n\n"
                f"<b>Новая:</b>\n⏰ {new_st}–{new_en}, e% {int(eff)}\n\n"
                f"Что сделать?",
                parse_mode='HTML', reply_markup=kb
            )
            return
        schedule = Schedule.query.filter_by(user_id=user.id, date=d, status='approved').first()
        early_start = 0
        if schedule and schedule.planned_start and not schedule.is_day_off:
            early_start = time_to_minutes(start_time) - time_to_minutes(schedule.planned_start)
        status = 'confirmed' if d == date.today() else 'pending'
        att = Attendance(user_id=user.id, date=d,
                         actual_start=start_time, actual_end=end_time,
                         efficiency=eff / 100, early_start=early_start,
                         status=status)
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
            f"✅ Факт сохранён!\n📅 {d.strftime('%d.%m.%Y')}\n"
            f"⏰ {start_time.strftime('%H:%M')} – {end_time.strftime('%H:%M')}\n"
            f"📈 e%: {int(eff)}",
            reply_markup=main_menu_keyboard()
        )


async def fact_conflict_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "fact_keep":
        clear_state(context)
        await query.edit_message_text("Оставлено как было.")
        await query.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())
        return
    if query.data == "fact_replace":
        data = context.user_data.get('pending_fact')
        if not data:
            await query.edit_message_text("❌ Данные потеряны.")
            return
        chat_id = query.message.chat_id
        with app.app_context():
            user = get_user_by_chat(chat_id)
            existing = Attendance.query.filter_by(user_id=user.id, date=data['date']).first()
            if existing:
                schedule = Schedule.query.filter_by(
                    user_id=user.id, date=data['date'], status='approved'
                ).first()
                early_start = 0
                if schedule and schedule.planned_start and not schedule.is_day_off:
                    early_start = time_to_minutes(data['start']) - time_to_minutes(schedule.planned_start)
                status = 'confirmed' if data['date'] == date.today() else 'pending'
                existing.actual_start = data['start']
                existing.actual_end = data['end']
                existing.efficiency = data['eff'] / 100
                existing.early_start = early_start
                existing.status = status
                db.session.commit()
        clear_state(context)
        await query.edit_message_text("✅ Заменено.")
        await query.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())


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
        parse_mode='HTML', reply_markup=cancel_keyboard()
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
            "📝 Опиши причину отсутствия",
            reply_markup=cancel_keyboard()
        )
        return
    context.user_data['state'] = 'abs_file'
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Пропустить (без файла)", callback_data="abs_skip_file")],
    ])
    await update.message.reply_text(
        "📎 Прикрепи справку (PDF или фото).\nЕсли файла нет — нажми «Пропустить».",
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
    await update.message.reply_text("📎 Прикрепи справку или нажми «Пропустить».",
                                    reply_markup=kb)


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
            user_id=user.id, date_start=d_start, date_end=d_end,
            type=abs_type,
            custom_type=custom if abs_type == 'other' else None,
            status='pending', file_path=file_path
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
                    user_id=admin.id, chat_id=admin.telegram_chat_id,
                    message=f"📩 {user.full_name} — заявка на {type_ru}: "
                            f"{d_start.strftime('%d.%m.%Y')} – {d_end.strftime('%d.%m.%Y')}"
                )
                db.session.add(notif)
        db.session.commit()

    type_ru = ABS_TYPE_RU.get(abs_type, abs_type)
    if abs_type == 'other' and custom:
        type_ru = f"📌 {custom}"
    text = (f"✅ <b>Заявка отправлена!</b>\n\n"
            f"{type_ru}\n"
            f"📅 {d_start.strftime('%d.%m.%Y')} – {d_end.strftime('%d.%m.%Y')}")
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
    chat_type = update.effective_chat.type

    with app.app_context():
        user = get_user_by_chat(chat_id)

    if not user:
        if chat_type in ('group', 'supergroup'):
            return
        if context.user_data.get('state') == 'ask_email':
            await receive_email(update, context)
        else:
            await update.message.reply_text(
                "Сначала отправь /start и привяжи аккаунт.",
                reply_markup=cancel_keyboard()
            )
        return

    state = context.user_data.get('state')

    if state in ('fact_start', 'fact_end', 'fact_eff',
                 'abs_date_start', 'abs_date_end', 'abs_custom', 'abs_file',
                 'week_custom_start', 'week_custom_end',
                 'sched_edit_start', 'sched_edit_end',
                 'sched_edit_tasks', 'sched_edit_note'):

        if text == CANCEL_TEXT:
            clear_state(context)
            await update.message.reply_text("Отменено. Главное меню 👇",
                                            reply_markup=main_menu_keyboard())
            return

        if state == 'fact_start':
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
        elif state == 'sched_edit_start':
            await sched_edit_start_handler(update, context)
        elif state == 'sched_edit_end':
            await sched_edit_end_handler(update, context)
        elif state == 'sched_edit_tasks':
            await sched_edit_tasks_handler(update, context)
        elif state == 'sched_edit_note':
            await sched_edit_note_handler(update, context)
        return

    if text == CANCEL_TEXT:
        clear_state(context)
        await update.message.reply_text("Главное меню 👇", reply_markup=main_menu_keyboard())
        return

    if 'Моё расписание' in text:
        clear_state(context)
        await cmd_my_schedule(update, context)
        return
    if 'Мой профиль' in text:
        clear_state(context)
        await cmd_profile(update, context)
        return
    if 'На неделю' in text:
        await week_plan_start(update, context)
        return
    if 'Расписание всех' in text:
        clear_state(context)
        await cmd_all_week(update, context)
        return
    if 'Кто работает' in text:
        clear_state(context)
        await cmd_who(update, context)
        return
    if 'Мои заявки' in text:
        clear_state(context)
        await cmd_my_absences(update, context)
        return
    if 'Итоги месяца' in text:
        clear_state(context)
        await cmd_summary(update, context)
        return
    if 'Помощь' in text:
        clear_state(context)
        await cmd_help(update, context)
        return
    if 'Отвязать аккаунт' in text:
        clear_state(context)
        await detach_start(update, context)
        return

    if 'Фактическое время' in text:
        clear_state(context)
        context.user_data['state'] = 'fact_date'
        today = date.today()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📅 Сегодня ({today.strftime('%d.%m')})",
                                  callback_data="fact_today")],
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

        # Пробуем ИИ-разбор для свободного текста
    if AI_PARSER:
        handled = await ai_message_handler(update, context)
        if handled:
            return

    await update.message.reply_text(
        "🤔 Не понимаю команду. Используй кнопки внизу 👇\n"
        "Или напиши свободным текстом, например:\n"
        "• <i>сегодня работал с 10 до 18</i>\n"
        "• <i>завтра с 9 до 17, задачи: отчёт, встреча</i>\n"
        "• <i>хочу в отпуск с 5 по 10 ноября</i>",
        parse_mode='HTML',
        reply_markup=main_menu_keyboard()
    )


# ================== КОРОТКИЕ КОМАНДЫ В ГРУППЕ ==================
async def group_parser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
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
        user = find_user_by_telegram(update)
        if not user:
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
                schedule = Schedule(user_id=user.id, date=d,
                                    planned_start=start_time, planned_end=end_time,
                                    is_day_off=False, status='approved')
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
                att = Attendance(user_id=user.id, date=d,
                                 actual_start=start_time, actual_end=end_time,
                                 efficiency=eff / 100, early_start=early_start, status=status)
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
                    user_id=user.id, date_start=d_start, date_end=d_end,
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
        next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        plans = Schedule.query.filter_by(date=today, status='approved').all()
        working_lines = []
        for plan in plans:
            if plan.is_day_off or not plan.user or plan.user.status != 'active':
                continue
            if plan.planned_start and plan.planned_end:
                st = plan.planned_start.strftime('%H:%M')
                en = plan.planned_end.strftime('%H:%M')
                working_lines.append(f"• {plan.user.full_name}: {st}–{en}")
        if working_lines:
            text_who = ("☀️ <b>Доброе утро!</b>\n\n👥 <b>Сегодня работают:</b>\n\n"
                        + "\n".join(working_lines))
        else:
            text_who = "☀️ <b>Доброе утро!</b>\n\nСегодня никто не работает."
        employees = User.query.filter_by(role='employee', status='active').all()
        sent_count = 0
        for emp in employees:
            if not emp.telegram_chat_id:
                continue
            has_plan_next = Schedule.query.filter(
                Schedule.user_id == emp.id,
                Schedule.date >= next_monday
            ).first() is not None
            if not has_plan_next:
                reminder = (f"\n\n📅 <b>Напоминание:</b> у тебя ещё нет расписания "
                            f"на следующую неделю (с {next_monday.strftime('%d.%m.%Y')}).\n"
                            f"Открой «📅 Моё расписание»")
            else:
                reminder = ""
            try:
                await context.bot.send_message(
                    chat_id=emp.telegram_chat_id,
                    text=text_who + reminder,
                    parse_mode='HTML'
                )
                sent_count += 1
            except Exception as e:
                logger.error(f"Ошибка рассылки {emp.id}: {e}")
        logger.info(f"Утренняя рассылка: отправлено {sent_count}")


async def weekly_planning_reminder(context: ContextTypes.DEFAULT_TYPE):
    with app.app_context():
        today = date.today()
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_monday = today + timedelta(days=days_until_monday)
        week_end = next_monday + timedelta(days=6)

        employees = User.query.filter_by(role='employee', status='active').all()
        sent = 0
        for emp in employees:
            if not emp.telegram_chat_id:
                continue
            has_plan = Schedule.query.filter(
                Schedule.user_id == emp.id,
                Schedule.date >= next_monday
            ).first() is not None
            if has_plan:
                continue
            try:
                await context.bot.send_message(
                    chat_id=emp.telegram_chat_id,
                    text=(
                        f"📅 <b>Напоминание о расписании</b>\n\n"
                        f"{esc(emp.full_name)}, пришли своё расписание "
                        f"на неделю с <b>{next_monday.strftime('%d.%m.%Y')}</b>.\n\n"
                        f"<b>Способы:</b>\n"
                        f"• В боте: «📅 Моё расписание» → тапни по дню\n"
                        f"• Быстрый мастер: «📋 На неделю»\n"
                        f"• В рабочей группе:\n"
                        f"<code>#расписание\n"
                        f"Спринт {next_monday.strftime('%d.%m')}-{week_end.strftime('%d.%m')}\n"
                        f"Фамилия И.О.\n"
                        f"ПН: 10:00-18:00\n"
                        f"ВТ: 10:00-18:00\n"
                        f"...</code>"
                    ),
                    parse_mode='HTML'
                )
                sent += 1
            except Exception as e:
                logger.error(f"Ошибка напоминания {emp.id}: {e}")
        logger.info(f"Напоминание о расписании: отправлено {sent}")


async def daily_fill_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Напоминание вечером заполнить дейли (18:00 по будням)."""
    with app.app_context():
        today = date.today()
        if today.weekday() >= 5:
            return
        employees = User.query.filter_by(role='employee', status='active').all()
        sent = 0
        for emp in employees:
            if not emp.telegram_chat_id:
                continue
            att = Attendance.query.filter_by(user_id=emp.id, date=today).first()
            if att:
                continue
            try:
                await context.bot.send_message(
                    chat_id=emp.telegram_chat_id,
                    text=(
                        f"🌆 <b>Конец рабочего дня</b>\n\n"
                        f"Не забудь заполнить, что сделал сегодня.\n\n"
                        f"Способы:\n"
                        f"• В боте: «⏰ Фактическое время»\n"
                        f"• В группе: <code>#дейли</code>"
                    ),
                    parse_mode='HTML'
                )
                sent += 1
            except Exception as e:
                logger.error(f"Ошибка напоминания дейли {emp.id}: {e}")
        logger.info(f"Напоминание дейли: отправлено {sent}")


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


async def test_morning_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧪 Запускаю рассылку вручную...")
    await morning_who(context)
    await update.message.reply_text("✅ Готово.")


async def test_weekly_reminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧪 Напоминание о расписании...")
    await weekly_planning_reminder(context)
    await update.message.reply_text("✅ Готово.")


async def test_daily_reminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧪 Напоминание заполнить дейли...")
    await daily_fill_reminder(context)
    await update.message.reply_text("✅ Готово.")


# ================== ЗАПУСК ==================
def main():
    if not Config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не задан в .env")
        return

    application = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('test_morning', test_morning_cmd))
    application.add_handler(CommandHandler('test_weekly', test_weekly_reminder_cmd))
    application.add_handler(CommandHandler('test_daily', test_daily_reminder_cmd))

    # «Моё расписание» — картинка/текст
    application.add_handler(CallbackQueryHandler(mysch_view_callback, pattern=r'^mysch_(text|image)$'))
    application.add_handler(CallbackQueryHandler(mysch_img_nav_callback,
    pattern=r'^mysch_img_(prev|now|next)$'))

    # Неделя / день
    application.add_handler(CallbackQueryHandler(sched_week_callback, pattern=r'^sched_(week_-?\d+|noop)$'))
    application.add_handler(CallbackQueryHandler(sched_day_callback, pattern=r'^sched_day_'))

    # Очистка дня (полная)
    application.add_handler(CallbackQueryHandler(sched_clear_yes_callback,
    pattern=r'^sched_clear_yes_'))
    application.add_handler(CallbackQueryHandler(sched_clear_callback,
    pattern=r'^sched_clear_\d'))
            
    # Очистка всей недели
    application.add_handler(CallbackQueryHandler(sched_week_clear_yes_callback,
    pattern=r'^mysch_clearweek_yes_'))
    application.add_handler(CallbackQueryHandler(sched_week_clear_callback,
    pattern=r'^mysch_clearweek_'))

    # Задачи на день (план)
    application.add_handler(CallbackQueryHandler(sched_tasks_action_callback,
    pattern=r'^sched_tasks_(add|replace|clear)$'))
    application.add_handler(CallbackQueryHandler(sched_tasks_callback,
    pattern=r'^sched_tasks_\d'))

    # «Что сделал» (факт)
    application.add_handler(CallbackQueryHandler(sched_note_action_callback,
    pattern=r'^sched_note_(add|replace|clear)$'))
    application.add_handler(CallbackQueryHandler(sched_note_callback,
    pattern=r'^sched_note_\d'))

    # Время / выходной / warn
    application.add_handler(CallbackQueryHandler(sched_time_callback, pattern=r'^sched_time_'))
    application.add_handler(CallbackQueryHandler(sched_dayoff_callback, pattern=r'^sched_dayoff_'))
    application.add_handler(CallbackQueryHandler(sched_warn_callback, pattern=r'^sched_warn_'))

    # Конфликты
    application.add_handler(CallbackQueryHandler(weekly_plan_conflict_callback, pattern=r'^wplan_'))
    application.add_handler(CallbackQueryHandler(daily_conflict_callback,
                                                 pattern=r'^daily_(replace|keep)$'))

    # Прочие
    application.add_handler(CallbackQueryHandler(fact_date_callback,
    pattern=r'^fact_(today|yesterday)$'))
    application.add_handler(CallbackQueryHandler(fact_conflict_callback,
    pattern=r'^fact_(replace|keep)$'))
    application.add_handler(CallbackQueryHandler(abs_type_callback,
    pattern=r'^abs_(vacation|sick|other)$'))
    application.add_handler(CallbackQueryHandler(abs_skip_file_callback, pattern=r'^abs_skip_file$'))
    application.add_handler(CallbackQueryHandler(all_week_callback, pattern=r'^allweek_'))
    application.add_handler(CallbackQueryHandler(
        week_plan_callback,
    pattern=r'^week_(full_day|custom|dayoff|skip|cancel|overwrite)$'))
    application.add_handler(CallbackQueryHandler(summary_choice_callback,
    pattern=r'^sum_(my|all)$'))
    application.add_handler(CallbackQueryHandler(summary_callback,
    pattern=r'^(sum_|allsum_)(prev|next|now)$'))
    application.add_handler(CallbackQueryHandler(detach_callback, pattern=r'^detach_'))
    application.add_handler(CallbackQueryHandler(week_pick_callback, pattern=r'^week_pick_'))

    # Группа: #расписание/#дейли и #план/#факт/#отпуск
    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        handle_structured_message
    ), group=-2)
    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        group_parser
    ), group=-1)

        # ИИ в группе — реагирует только на @botname или ключевые слова
    application.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        ai_message_handler
    ), group=0)

    # Личка
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, text_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # Job queue
    application.job_queue.run_repeating(check_notifications, interval=5, first=5)
    application.job_queue.run_daily(
        morning_who,
        time=dtime(hour=8, minute=0),
        days=(0, 1, 2, 3, 4)
    )
    application.job_queue.run_daily(
        weekly_planning_reminder,
        time=dtime(hour=17, minute=0),
        days=(4,)
    )
    application.job_queue.run_daily(
        daily_fill_reminder,
        time=dtime(hour=18, minute=0),
        days=(0, 1, 2, 3, 4)
    )

    print("🤖 Бот запущен. Нажми Ctrl+C для остановки.")
    application.run_polling()


if __name__ == '__main__':
    main()