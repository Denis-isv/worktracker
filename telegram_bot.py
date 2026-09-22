import logging
from datetime import datetime, date

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from config import Config
from app import app, db
from models import User, Schedule, Attendance, Absence, Notification
from utils import parse_time_string, time_to_minutes

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def get_user_by_telegram(telegram_id):
    return User.query.filter_by(telegram_id=str(telegram_id)).first()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    with app.app_context():
        user = get_user_by_telegram(telegram_id)
        if user:
            await update.message.reply_text(
                f"👋 Привет, {user.full_name}!\n\n"
                f"Ты уже зарегистрирован. Введи /help, чтобы увидеть список команд."
            )
            return
    await update.message.reply_text(
        "👋 Привет! Чтобы привязать Telegram к учётной записи WorkTracker,\n"
        "пришли свой email (тот, под которым ты входишь на сайт)."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Доступные команды:*\n\n"
        "/start — привязать Telegram к аккаунту\n\n"
        "/plan ДАТА НАЧАЛО КОНЕЦ — предложить план\n"
        "   Пример: `/plan 2026-09-23 10:00 18:00`\n\n"
        "/dayoff ДАТА — отметить выходной\n"
        "   Пример: `/dayoff 2026-09-23`\n\n"
        "/fact ДАТА НАЧАЛО КОНЕЦ ЭФФЕКТИВНОСТЬ — отметить факт\n"
        "   Пример: `/fact 2026-09-23 10:10 18:05 90`\n\n"
        "/vacation НАЧАЛО КОНЕЦ ТИП — отпуск/больничный\n"
        "   Тип: vacation, sick, other\n"
        "   Пример: `/vacation 2026-09-25 2026-09-30 vacation`\n\n"
        "/status — мои данные на сегодня\n"
        "/who — кто работает сегодня\n"
        "/help — эта справка"
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip().lower()
    telegram_id = str(update.effective_user.id)

    with app.app_context():
        user = User.query.filter_by(email=email).first()
        if not user:
            await update.message.reply_text(
                "❌ Пользователь с таким email не найден.\n"
                "Обратитесь к администратору или проверьте email."
            )
            return

        user.telegram_id = telegram_id
        db.session.commit()

        await update.message.reply_text(
            f"✅ Отлично, {user.full_name}!\n"
            f"Telegram привязан к твоей учётной записи.\n\n"
            f"Введи /help, чтобы увидеть доступные команды."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    with app.app_context():
        user = get_user_by_telegram(telegram_id)
        if user:
            await update.message.reply_text("Введи /help, чтобы увидеть список команд.")
            return
    await handle_email(update, context)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    with app.app_context():
        user = get_user_by_telegram(telegram_id)
        if not user:
            await update.message.reply_text("❌ Сначала привяжи аккаунт через /start")
            return

        args = context.args
        if len(args) != 3:
            await update.message.reply_text(
                "❌ Формат: /plan ДАТА НАЧАЛО КОНЕЦ\n"
                "Пример: `/plan 2026-09-23 10:00 18:00`",
                parse_mode='Markdown'
            )
            return

        try:
            d = datetime.strptime(args[0], '%Y-%m-%d').date()
            start = parse_time_string(args[1])
            end = parse_time_string(args[2])
        except Exception:
            await update.message.reply_text("❌ Неверный формат даты или времени.")
            return

        if start >= end:
            await update.message.reply_text("❌ Время ухода должно быть позже времени прихода.")
            return

        existing = Schedule.query.filter_by(user_id=user.id, date=d).first()
        if existing:
            await update.message.reply_text(f"⚠️ На {d.strftime('%d.%m.%Y')} уже есть план.")
            return

        schedule = Schedule(
            user_id=user.id, date=d,
            planned_start=start, planned_end=end,
            is_day_off=False, status='approved'
        )
        db.session.add(schedule)
        db.session.commit()

        await update.message.reply_text(
            f"✅ План на {d.strftime('%d.%m.%Y')} сохранён:\n"
            f"⏰ {start.strftime('%H:%M')} – {end.strftime('%H:%M')}"
        )


async def cmd_dayoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    with app.app_context():
        user = get_user_by_telegram(telegram_id)
        if not user:
            await update.message.reply_text("❌ Сначала привяжи аккаунт через /start")
            return

        args = context.args
        if len(args) != 1:
            await update.message.reply_text("❌ Формат: /dayoff ДАТА\nПример: `/dayoff 2026-09-23`", parse_mode='Markdown')
            return

        try:
            d = datetime.strptime(args[0], '%Y-%m-%d').date()
        except Exception:
            await update.message.reply_text("❌ Неверный формат даты.")
            return

        existing = Schedule.query.filter_by(user_id=user.id, date=d).first()
        if existing:
            await update.message.reply_text(f"⚠️ На {d.strftime('%d.%m.%Y')} уже есть заявка.")
            return

        schedule = Schedule(user_id=user.id, date=d, is_day_off=True, status='approved')
        db.session.add(schedule)
        db.session.commit()

        await update.message.reply_text(f"✅ Выходной на {d.strftime('%d.%m.%Y')} отмечен.")


async def cmd_fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    with app.app_context():
        user = get_user_by_telegram(telegram_id)
        if not user:
            await update.message.reply_text("❌ Сначала привяжи аккаунт через /start")
            return

        args = context.args
        if len(args) != 4:
            await update.message.reply_text(
                "❌ Формат: /fact ДАТА НАЧАЛО КОНЕЦ ЭФФЕКТИВНОСТЬ\n"
                "Пример: `/fact 2026-09-23 10:10 18:05 90`",
                parse_mode='Markdown'
            )
            return

        try:
            d = datetime.strptime(args[0], '%Y-%m-%d').date()
            start = parse_time_string(args[1])
            end = parse_time_string(args[2])
            efficiency = float(args[3]) / 100
        except Exception:
            await update.message.reply_text("❌ Неверный формат.")
            return

        if start >= end:
            await update.message.reply_text("❌ Время ухода должно быть позже времени прихода.")
            return

        if d > date.today():
            await update.message.reply_text("❌ Нельзя отмечать факт на будущую дату.")
            return

        existing = Attendance.query.filter_by(user_id=user.id, date=d).first()
        if existing:
            await update.message.reply_text(f"⚠️ На {d.strftime('%d.%m.%Y')} уже есть отметка.")
            return

        schedule = Schedule.query.filter_by(user_id=user.id, date=d, status='approved').first()
        early_start = 0
        if schedule and schedule.planned_start and not schedule.is_day_off:
            planned_minutes = time_to_minutes(schedule.planned_start)
            actual_minutes = time_to_minutes(start)
            early_start = actual_minutes - planned_minutes

        status = 'confirmed' if d == date.today() else 'pending'

        att = Attendance(
            user_id=user.id, date=d,
            actual_start=start, actual_end=end,
            efficiency=efficiency, early_start=early_start,
            status=status
        )
        db.session.add(att)
        db.session.commit()

        if status == 'pending':
            await update.message.reply_text(
                f"✅ Факт за {d.strftime('%d.%m.%Y')} сохранён и ожидает подтверждения администратора."
            )
        else:
            await update.message.reply_text(
                f"✅ Факт за {d.strftime('%d.%m.%Y')} сохранён:\n"
                f"⏰ {start.strftime('%H:%M')} – {end.strftime('%H:%M')} (e% {args[3]})"
            )


async def cmd_vacation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    with app.app_context():
        user = get_user_by_telegram(telegram_id)
        if not user:
            await update.message.reply_text("❌ Сначала привяжи аккаунт через /start")
            return

        args = context.args
        if len(args) != 3:
            await update.message.reply_text(
                "❌ Формат: /vacation НАЧАЛО КОНЕЦ ТИП\n"
                "Типы: vacation, sick, other\n"
                "Пример: `/vacation 2026-09-25 2026-09-30 vacation`",
                parse_mode='Markdown'
            )
            return

        try:
            date_start = datetime.strptime(args[0], '%Y-%m-%d').date()
            date_end = datetime.strptime(args[1], '%Y-%m-%d').date()
            absence_type = args[2]
        except Exception:
            await update.message.reply_text("❌ Неверный формат.")
            return

        if date_start > date_end:
            await update.message.reply_text("❌ Дата начала позже даты окончания.")
            return

        if absence_type not in ('vacation', 'sick', 'other'):
            await update.message.reply_text("❌ Тип должен быть: vacation, sick или other")
            return

        absence = Absence(
            user_id=user.id,
            date_start=date_start,
            date_end=date_end,
            type=absence_type,
            status='pending'
        )
        db.session.add(absence)
        db.session.commit()

        admins = User.query.filter_by(role='admin').all()
        for admin in admins:
            if admin.telegram_id:
                notif = Notification(
                    user_id=admin.id,
                    chat_id=admin.telegram_id,
                    message=f"📩 {user.full_name} подал заявку на {absence_type} с {date_start.strftime('%d.%m.%Y')} по {date_end.strftime('%d.%m.%Y')}"
                )
                db.session.add(notif)
        db.session.commit()

        await update.message.reply_text(
            f"✅ Заявка на {absence_type} отправлена.\n"
            f"📅 {date_start.strftime('%d.%m.%Y')} – {date_end.strftime('%d.%m.%Y')}\n"
            f"Ожидай подтверждения администратора."
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = str(update.effective_user.id)
    with app.app_context():
        user = get_user_by_telegram(telegram_id)
        if not user:
            await update.message.reply_text("❌ Сначала привяжи аккаунт через /start")
            return

        today = date.today()
        schedule = Schedule.query.filter_by(user_id=user.id, date=today).first()
        attendance = Attendance.query.filter_by(user_id=user.id, date=today).first()

        text = f"📊 *Твои данные на {today.strftime('%d.%m.%Y')}:*\n\n"

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
            text += f"🎯 Статус: {attendance.status}\n"
        else:
            text += "⏰ Факт: не отмечен\n"

        await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with app.app_context():
        today = date.today()
        plans = Schedule.query.filter_by(date=today, status='approved').all()

        if not plans:
            await update.message.reply_text("Сегодня никто не работает.")
            return

        text = f"👥 *Кто работает сегодня ({today.strftime('%d.%m.%Y')}):*\n\n"
        for plan in plans:
            if plan.is_day_off:
                continue
            text += f"• {plan.user.full_name}: {plan.planned_start.strftime('%H:%M')} – {plan.planned_end.strftime('%H:%M')}\n"

        await update.message.reply_text(text, parse_mode='Markdown')


async def check_notifications(context: ContextTypes.DEFAULT_TYPE):
    with app.app_context():
        pending = Notification.query.filter_by(status='pending').all()
        for n in pending:
            try:
                await context.bot.send_message(chat_id=n.chat_id, text=n.message)
                n.status = 'sent'
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление {n.id}: {e}")
        db.session.commit()


def main():
    if not Config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не задан в .env")
        return

    application = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('plan', cmd_plan))
    application.add_handler(CommandHandler('dayoff', cmd_dayoff))
    application.add_handler(CommandHandler('fact', cmd_fact))
    application.add_handler(CommandHandler('vacation', cmd_vacation))
    application.add_handler(CommandHandler('status', cmd_status))
    application.add_handler(CommandHandler('who', cmd_who))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.job_queue.run_repeating(check_notifications, interval=5, first=5)

    print("🤖 Бот запущен. Нажми Ctrl+C для остановки.")
    application.run_polling()


if __name__ == '__main__':
    main()