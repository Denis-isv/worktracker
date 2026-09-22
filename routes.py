from flask import render_template, redirect, url_for, request, flash, send_file, abort
from flask_login import login_user, logout_user, login_required, current_user
from models import User, Schedule, Attendance, Absence, ChangeLog
from werkzeug.security import check_password_hash, generate_password_hash
from app import app, db
from utils import (
    time_to_minutes, minutes_to_time, timedelta_to_minutes,
    calculate_worked_hours, format_timedelta_hhmm, parse_time_string
)
from datetime import datetime, date, time, timedelta
import calendar
import io
import openpyxl
import os

DAYS_RU = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
MONTHS_RU = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
             'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']

@app.template_filter('status_ru')
def status_ru_filter(value):
    mapping = {
        'pending': 'Ожидает',
        'approved': 'Подтверждено',
        'rejected': 'Отклонено',
        'confirmed': 'Подтверждено',
        'vacation': 'Отпуск',
        'sick': 'Больничный',
        'other': 'Другое',
        'active': 'Активен',
        'blocked': 'Заблокирован',
        'day_off': 'Выходной'
    }
    return mapping.get(value, value)

@app.template_filter('month_ru')
def month_ru_filter(month_num):
    try:
        return MONTHS_RU[int(month_num) - 1]
    except:
        return month_num

# ---------- ОБЩИЕ ----------
@app.route('/')
@login_required
def index():
    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))
    else:
        return redirect(url_for('employee_dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            flash('Вы успешно вошли!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Неверный email или пароль', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('login'))

# ---------- СОТРУДНИК ----------


@app.route('/employee/dashboard')
@login_required
def employee_dashboard():
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)
    if month < 1 or month > 12:
        month = date.today().month

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    start_date = date(year, month, 1)
    end_date = date(year, month, calendar.monthrange(year, month)[1])

    schedules = Schedule.query.filter(
        Schedule.user_id == current_user.id,
        Schedule.date >= start_date,
        Schedule.date <= end_date
    ).all()
    attendances = Attendance.query.filter(
        Attendance.user_id == current_user.id,
        Attendance.date >= start_date,
        Attendance.date <= end_date
    ).all()
    absences = Absence.query.filter(
        Absence.user_id == current_user.id,
        Absence.date_start <= end_date,
        Absence.date_end >= start_date
    ).all()

    schedule_by_day = {s.date.day: s for s in schedules}
    attendance_by_day = {a.date.day: a for a in attendances}
    absence_by_day = {}
    for absence in absences:
        d = absence.date_start
        while d <= absence.date_end:
            if d.year == year and d.month == month:
                absence_by_day[d.day] = absence
            d += timedelta(days=1)

    cal = calendar.Calendar()
    month_days = cal.monthdayscalendar(year, month)

    return render_template(
        'employee/dashboard.html',
        user=current_user,
        year=year,
        month=month,
        month_name=MONTHS_RU[month - 1],
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        month_days=month_days,
        schedule_by_day=schedule_by_day,
        attendance_by_day=attendance_by_day,
        absence_by_day=absence_by_day,
        date=date
    )

@app.route('/employee/history')
@login_required
def employee_history():
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    view = request.args.get('view', 'month')  # 'month' или 'week'
    today = date.today()

    if view == 'week':
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
    else:
        month = request.args.get('month', today.month, type=int)
        if month < 1 or month > 12:
            month = today.month
        start_date = date(today.year, month, 1)
        end_date = date(today.year, month, calendar.monthrange(today.year, month)[1])

    schedules = Schedule.query.filter(
        Schedule.user_id == current_user.id,
        Schedule.date >= start_date,
        Schedule.date <= end_date
    ).order_by(Schedule.date.desc()).all()

    attendances = Attendance.query.filter(
        Attendance.user_id == current_user.id,
        Attendance.date >= start_date,
        Attendance.date <= end_date
    ).order_by(Attendance.date.desc()).all()

    absences = Absence.query.filter(
        Absence.user_id == current_user.id,
        Absence.date_start <= end_date,
        Absence.date_end >= start_date
    ).order_by(Absence.date_start.desc()).all()

    return render_template(
        'employee/history.html',
        schedules=schedules,
        attendances=attendances,
        absences=absences,
        view=view,
        month=today.month,
        date=date
    )

@app.route('/employee/who_works')
@login_required
def employee_who_works():
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    today = date.today()
    plans_today = Schedule.query.filter_by(date=today, status='approved').all()
    attendances_today = Attendance.query.filter_by(date=today).all()

    working_employees = {}
    for sch in plans_today:
        emp = sch.user
        if emp.status == 'active':
            working_employees[emp.id] = {
                'name': emp.full_name,
                'plan_start': sch.planned_start.strftime('%H:%M') if sch.planned_start else None,
                'plan_end': sch.planned_end.strftime('%H:%M') if sch.planned_end else None,
                'is_day_off': sch.is_day_off,
                'actual_start': None,
                'actual_end': None
            }
    for att in attendances_today:
        emp = att.user
        if emp.id in working_employees:
            working_employees[emp.id]['actual_start'] = att.actual_start.strftime('%H:%M') if att.actual_start else None
            working_employees[emp.id]['actual_end'] = att.actual_end.strftime('%H:%M') if att.actual_end else None
        else:
            if emp.status == 'active':
                working_employees[emp.id] = {
                    'name': emp.full_name,
                    'plan_start': None,
                    'plan_end': None,
                    'is_day_off': False,
                    'actual_start': att.actual_start.strftime('%H:%M') if att.actual_start else None,
                    'actual_end': att.actual_end.strftime('%H:%M') if att.actual_end else None
                }

    return render_template(
        'employee/who_works.html',
        today=today,
        working_employees=working_employees.values()
    )

@app.route('/employee/weekly_schedule')
@login_required
def employee_weekly_schedule():
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    week_offset = request.args.get('offset', 0, type=int)
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    end_of_week = start_of_week + timedelta(days=6)

    employees = User.query.filter_by(role='employee', status='active').all()

    days = []
    for i in range(7):
        day = start_of_week + timedelta(days=i)
        plans = {}
        for emp in employees:
            sch = Schedule.query.filter_by(user_id=emp.id, date=day).first()
            att = Attendance.query.filter_by(user_id=emp.id, date=day).first()
            plans[emp.id] = {'schedule': sch, 'attendance': att}
        days.append({
            'date': day,
            'day_name': DAYS_RU[day.weekday()],
            'plans': plans
        })

    month = start_of_week.month
    year = start_of_week.year
    cal = calendar.Calendar()
    month_days = cal.monthdayscalendar(year, month)
    num_days = calendar.monthrange(year, month)[1]

    month_plans = {}
    for day in range(1, num_days + 1):
        d = date(year, month, day)
        day_plans = []
        for emp in employees:
            sch = Schedule.query.filter_by(user_id=emp.id, date=d).first()
            att = Attendance.query.filter_by(user_id=emp.id, date=d).first()
            day_plans.append({
                'name': emp.full_name,
                'schedule_start': sch.planned_start.strftime('%H:%M') if sch and sch.planned_start else None,
                'schedule_end': sch.planned_end.strftime('%H:%M') if sch and sch.planned_end else None,
                'schedule_is_day_off': sch.is_day_off if sch else False,
                'attendance_start': att.actual_start.strftime('%H:%M') if att and att.actual_start else None,
                'attendance_end': att.actual_end.strftime('%H:%M') if att and att.actual_end else None,
            })
        month_plans[day] = day_plans

    return render_template(
        'employee/weekly_schedule.html',
        days=days,
        employees=employees,
        start_of_week=start_of_week,
        end_of_week=end_of_week,
        week_offset=week_offset,
        month_days=month_days,
        month=month,
        year=year,
        month_name=MONTHS_RU[month-1],
        month_plans=month_plans,
        num_days=num_days
    )

@app.route('/employee/month_summary')
@login_required
def employee_month_summary():
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)
    if month < 1 or month > 12:
        month = date.today().month

    start_date = date(year, month, 1)
    end_date = date(year, month, calendar.monthrange(year, month)[1])

    total_worked_minutes = 0
    total_eff_minutes = 0
    total_late_minutes = 0

    attendances = Attendance.query.filter(
        Attendance.user_id == current_user.id,
        Attendance.date >= start_date,
        Attendance.date <= end_date
    ).all()

    for att in attendances:
        if att.actual_start and att.actual_end:
            worked = calculate_worked_hours(att.actual_start, att.actual_end)
            worked_min = int(worked.total_seconds() // 60)
            eff_min = int(worked_min * att.efficiency)
            total_worked_minutes += worked_min
            total_eff_minutes += eff_min
            if att.early_start and att.early_start > 0:
                total_late_minutes += att.early_start

    avg_efficiency = (total_eff_minutes / total_worked_minutes * 100) if total_worked_minutes > 0 else 0
    final_minutes = total_eff_minutes - total_late_minutes

    summary = {
        'worked_hours': total_worked_minutes / 60,
        'avg_efficiency': avg_efficiency,
        'late_hours': total_late_minutes / 60,
        'final_hours': final_minutes / 60
    }

    return render_template(
        'employee/month_summary.html',
        summary=summary,
        year=year,
        month=month,
        month_name=MONTHS_RU[month - 1]
    )

@app.route('/employee/schedule/add', methods=['GET', 'POST'])
@login_required
def employee_schedule_add():
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    default_date = request.args.get('date', '')
    form_data = {}

    if request.method == 'POST':
        date_str = request.form.get('date')
        start_str = request.form.get('start')
        end_str = request.form.get('end')
        is_day_off = request.form.get('is_day_off') == 'on'
        form_data = {
            'date_str': date_str,
            'start_str': start_str,
            'end_str': end_str,
            'is_day_off': is_day_off
        }

        if not date_str or (not is_day_off and (not start_str or not end_str)):
            flash('Заполните все поля', 'danger')
            return render_template('employee/schedule_add.html', default_date=default_date, form_data=form_data)

        try:
            d = datetime.strptime(date_str, '%Y-%m-%d').date()
            if not is_day_off:
                planned_start = parse_time_string(start_str)
                planned_end = parse_time_string(end_str)
                if planned_start >= planned_end:
                    flash('Время ухода должно быть позже времени прихода', 'danger')
                    return render_template('employee/schedule_add.html', default_date=default_date, form_data=form_data)
            else:
                planned_start = None
                planned_end = None

            existing = Schedule.query.filter_by(user_id=current_user.id, date=d).first()
            if existing:
                flash('На эту дату уже есть заявка. Удалите или измените её.', 'warning')
                return redirect(url_for('employee_dashboard'))

            schedule = Schedule(
                user_id=current_user.id,
                date=d,
                planned_start=planned_start,
                planned_end=planned_end,
                is_day_off=is_day_off,
                status='approved'
            )
            db.session.add(schedule)
            db.session.commit()

            change = ChangeLog(
                user_id=current_user.id,
                target_user_id=current_user.id,
                field_changed='schedule_add',
                old_value='',
                new_value=f'{d} {start_str}-{end_str}'
            )
            db.session.add(change)
            db.session.commit()

            flash('План сохранён (подтверждён автоматически)', 'success')
            return redirect(url_for('employee_dashboard'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка: {e}', 'danger')
            return render_template('employee/schedule_add.html', default_date=default_date, form_data=form_data)

    return render_template('employee/schedule_add.html', default_date=default_date, form_data=form_data)

@app.route('/employee/schedule/<int:schedule_id>/edit', methods=['GET', 'POST'])
@login_required
def employee_schedule_edit(schedule_id):
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    schedule = Schedule.query.get_or_404(schedule_id)
    if schedule.user_id != current_user.id:
        abort(403)

    if request.method == 'POST':
        date_str = request.form.get('date')
        start_str = request.form.get('start')
        end_str = request.form.get('end')
        is_day_off = request.form.get('is_day_off') == 'on'

        if not date_str or (not is_day_off and (not start_str or not end_str)):
            flash('Заполните все поля', 'danger')
            return redirect(url_for('employee_schedule_edit', schedule_id=schedule.id))

        try:
            d = datetime.strptime(date_str, '%Y-%m-%d').date()
            if not is_day_off:
                planned_start = parse_time_string(start_str)
                planned_end = parse_time_string(end_str)
                if planned_start >= planned_end:
                    flash('Время ухода должно быть позже времени прихода', 'danger')
                    return redirect(url_for('employee_schedule_edit', schedule_id=schedule.id))
            else:
                planned_start = None
                planned_end = None

            existing = Schedule.query.filter(
                Schedule.user_id == current_user.id,
                Schedule.date == d,
                Schedule.id != schedule.id
            ).first()
            if existing:
                flash('На эту дату уже есть другая заявка.', 'warning')
                return redirect(url_for('employee_dashboard'))

            schedule.date = d
            schedule.planned_start = planned_start
            schedule.planned_end = planned_end
            schedule.is_day_off = is_day_off
            schedule.status = 'approved'

            db.session.commit()
            flash('План обновлён', 'success')
            return redirect(url_for('employee_dashboard'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка: {e}', 'danger')

    return render_template('employee/schedule_edit.html', schedule=schedule)

@app.route('/employee/schedule/<int:schedule_id>/delete', methods=['POST'])
@login_required
def employee_schedule_delete(schedule_id):
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    schedule = Schedule.query.get_or_404(schedule_id)
    if schedule.user_id != current_user.id:
        abort(403)

    db.session.delete(schedule)
    db.session.commit()
    flash('План удалён', 'success')
    return redirect(url_for('employee_dashboard'))

@app.route('/employee/attendance/add', methods=['GET', 'POST'])
@login_required
def employee_attendance_add():
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    default_date = request.args.get('date', date.today().isoformat())  # автоматически сегодня
    today = date.today()
    form_data = {}

    if request.method == 'POST':
        date_str = request.form.get('date')
        start_str = request.form.get('start')
        end_str = request.form.get('end')
        efficiency_str = request.form.get('efficiency')
        form_data = {
            'date_str': date_str,
            'start_str': start_str,
            'end_str': end_str,
            'efficiency_str': efficiency_str
        }

        if not date_str or not start_str or not end_str or not efficiency_str:
            flash('Заполните все поля', 'danger')
            return render_template('employee/attendance_add.html', today=today.isoformat(), default_date=default_date, form_data=form_data)

        try:
            d = datetime.strptime(date_str, '%Y-%m-%d').date()
            actual_start = parse_time_string(start_str)
            actual_end = parse_time_string(end_str)
            efficiency = float(efficiency_str) / 100

            if d > today:
                flash('Нельзя отмечать фактическое время на будущую дату', 'danger')
                return render_template('employee/attendance_add.html', today=today.isoformat(), default_date=default_date, form_data=form_data)

            if actual_start >= actual_end:
                flash('Время ухода должно быть позже времени прихода', 'danger')
                return render_template('employee/attendance_add.html', today=today.isoformat(), default_date=default_date, form_data=form_data)

            existing = Attendance.query.filter_by(user_id=current_user.id, date=d).first()
            if existing:
                flash('На эту дату уже есть фактическая отметка', 'warning')
                return redirect(url_for('employee_dashboard'))

            early_start = 0
            schedule = Schedule.query.filter_by(
                user_id=current_user.id,
                date=d,
                status='approved'
            ).first()
            if schedule and schedule.planned_start:
                planned_minutes = time_to_minutes(schedule.planned_start)
                actual_minutes = time_to_minutes(actual_start)
                early_start = actual_minutes - planned_minutes

            status = 'confirmed' if d == today else 'pending'

            attendance = Attendance(
                user_id=current_user.id,
                date=d,
                actual_start=actual_start,
                actual_end=actual_end,
                efficiency=efficiency,
                early_start=early_start,
                status=status
            )
            db.session.add(attendance)
            db.session.commit()

            change = ChangeLog(
                user_id=current_user.id,
                target_user_id=current_user.id,
                field_changed='attendance_add',
                old_value='',
                new_value=f'{d} {start_str}-{end_str} eff={efficiency_str}%'
            )
            db.session.add(change)
            db.session.commit()

            if status == 'pending':
                flash('Фактическое время сохранено и ожидает подтверждения администратора', 'warning')
            else:
                flash('Фактическое время записано', 'success')
            return redirect(url_for('employee_dashboard'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка: {e}', 'danger')
            return render_template('employee/attendance_add.html', today=today.isoformat(), default_date=default_date, form_data=form_data)

    return render_template('employee/attendance_add.html', today=today.isoformat(), default_date=default_date, form_data=form_data)

@app.route('/employee/attendance/<int:attendance_id>/edit', methods=['GET', 'POST'])
@login_required
def employee_attendance_edit(attendance_id):
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    attendance = Attendance.query.get_or_404(attendance_id)
    if attendance.user_id != current_user.id:
        abort(403)

    if request.method == 'POST':
        start_str = request.form.get('start')
        end_str = request.form.get('end')
        efficiency_str = request.form.get('efficiency')

        if not start_str or not end_str or not efficiency_str:
            flash('Заполните все поля', 'danger')
            return redirect(url_for('employee_attendance_edit', attendance_id=attendance.id))

        try:
            actual_start = parse_time_string(start_str)
            actual_end = parse_time_string(end_str)
            efficiency = float(efficiency_str) / 100

            if actual_start >= actual_end:
                flash('Время ухода должно быть позже времени прихода', 'danger')
                return redirect(url_for('employee_attendance_edit', attendance_id=attendance.id))

            schedule = Schedule.query.filter_by(
                user_id=current_user.id,
                date=attendance.date,
                status='approved'
            ).first()
            early_start = 0
            if schedule and schedule.planned_start:
                planned_minutes = time_to_minutes(schedule.planned_start)
                actual_minutes = time_to_minutes(actual_start)
                early_start = actual_minutes - planned_minutes

            attendance.actual_start = actual_start
            attendance.actual_end = actual_end
            attendance.efficiency = efficiency
            attendance.early_start = early_start

            if attendance.date < date.today():
                attendance.status = 'pending'
                flash_message = 'Изменения сохранены и ожидают подтверждения администратора'
            else:
                attendance.status = 'confirmed'
                flash_message = 'Отметка обновлена'

            db.session.commit()

            change = ChangeLog(
                user_id=current_user.id,
                target_user_id=current_user.id,
                field_changed='attendance_edit',
                old_value='',
                new_value=f'{attendance.date} {start_str}-{end_str} eff={efficiency_str}%'
            )
            db.session.add(change)
            db.session.commit()

            flash(flash_message, 'success' if attendance.status == 'confirmed' else 'warning')
            return redirect(url_for('employee_history'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка: {e}', 'danger')

    return render_template(
        'employee/attendance_edit.html',
        attendance=attendance,
        today=date.today().isoformat()
    )

@app.route('/employee/absence/add', methods=['GET', 'POST'])
@login_required
def employee_absence_add():
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    today = date.today()
    year = request.args.get('year', today.year, type=int)
    month = request.args.get('month', today.month, type=int)
    if month < 1 or month > 12:
        month = today.month

    month_days = calendar.Calendar().monthdayscalendar(year, month)

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    form_data = {}
    if request.method == 'POST':
        date_start_str = request.form.get('date_start')
        date_end_str = request.form.get('date_end')
        absence_type = request.form.get('type')
        custom_type = request.form.get('custom_type', '').strip()
        file = request.files.get('file')

        form_data = {
            'date_start_str': date_start_str,
            'date_end_str': date_end_str,
            'absence_type': absence_type,
            'custom_type': custom_type
        }

        if not date_start_str or not date_end_str or not absence_type:
            flash('Заполните все поля', 'danger')
            return render_template('employee/absence_add.html',
                                   form_data=form_data, year=year, month=month,
                                   month_days=month_days, month_name=MONTHS_RU[month-1],
                                   prev_year=prev_year, prev_month=prev_month,
                                   next_year=next_year, next_month=next_month)

        if absence_type == 'other' and not custom_type:
            flash('Укажите, что именно (например, «отгул», «учёба»)', 'danger')
            return render_template('employee/absence_add.html',
                                   form_data=form_data, year=year, month=month,
                                   month_days=month_days, month_name=MONTHS_RU[month-1],
                                   prev_year=prev_year, prev_month=prev_month,
                                   next_year=next_year, next_month=next_month)

        try:
            date_start = datetime.strptime(date_start_str, '%Y-%m-%d').date()
            date_end = datetime.strptime(date_end_str, '%Y-%m-%d').date()
            if date_start > date_end:
                flash('Дата начала позже даты окончания', 'danger')
                return render_template('employee/absence_add.html',
                                       form_data=form_data, year=year, month=month,
                                       month_days=month_days, month_name=MONTHS_RU[month-1],
                                       prev_year=prev_year, prev_month=prev_month,
                                       next_year=next_year, next_month=next_month)

            file_path = None
            if file:
                upload_folder = os.path.join(app.root_path, 'static', 'uploads')
                os.makedirs(upload_folder, exist_ok=True)
                filename = f"{current_user.id}_{date_start}_{date_end}_{file.filename}"
                file.save(os.path.join(upload_folder, filename))
                file_path = f'uploads/{filename}'

            absence = Absence(
                user_id=current_user.id,
                date_start=date_start,
                date_end=date_end,
                type=absence_type,
                custom_type=custom_type if absence_type == 'other' else None,
                status='pending',
                file_path=file_path
            )
            db.session.add(absence)
            db.session.commit()

            flash('Заявка отправлена', 'success')
            return redirect(url_for('employee_dashboard'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка: {e}', 'danger')

    form_data = {
        'date_start_str': request.args.get('date_start', ''),
        'date_end_str': request.args.get('date_end', ''),
        'absence_type': request.args.get('type', ''),
        'custom_type': ''
    }
    return render_template('employee/absence_add.html',
                           form_data=form_data, year=year, month=month,
                           month_days=month_days, month_name=MONTHS_RU[month-1],
                           prev_year=prev_year, prev_month=prev_month,
                           next_year=next_year, next_month=next_month)

@app.route('/employee/absence/<int:absence_id>/edit', methods=['GET', 'POST'])
@login_required
def employee_absence_edit(absence_id):
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    absence = Absence.query.get_or_404(absence_id)
    if absence.user_id != current_user.id:
        abort(403)

    today = date.today()
    # По умолчанию открываем месяц начала отпуска
    default_year = absence.date_start.year if absence.date_start else today.year
    default_month = absence.date_start.month if absence.date_start else today.month

    year = request.args.get('year', default_year, type=int)
    month = request.args.get('month', default_month, type=int)
    if month < 1 or month > 12:
        month = today.month

    month_days = calendar.Calendar().monthdayscalendar(year, month)

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    if request.method == 'POST':
        date_start_str = request.form.get('date_start')
        date_end_str = request.form.get('date_end')
        absence_type = request.form.get('type')
        file = request.files.get('file')

        if not date_start_str or not date_end_str or not absence_type:
            flash('Заполните все поля', 'danger')
            return redirect(url_for('employee_absence_edit', absence_id=absence.id,
                                    year=year, month=month))

        try:
            date_start = datetime.strptime(date_start_str, '%Y-%m-%d').date()
            date_end = datetime.strptime(date_end_str, '%Y-%m-%d').date()
            if date_start > date_end:
                flash('Дата начала позже даты окончания', 'danger')
                return redirect(url_for('employee_absence_edit', absence_id=absence.id,
                                        year=year, month=month))

            file_path = absence.file_path
            if file:
                upload_folder = os.path.join(app.root_path, 'static', 'uploads')
                os.makedirs(upload_folder, exist_ok=True)
                filename = f"{current_user.id}_{date_start}_{date_end}_{file.filename}"
                file.save(os.path.join(upload_folder, filename))
                file_path = f'uploads/{filename}'

            absence.date_start = date_start
            absence.date_end = date_end
            absence.type = absence_type
            absence.file_path = file_path
            absence.status = 'pending'

            db.session.commit()

            change = ChangeLog(
                user_id=current_user.id,
                target_user_id=current_user.id,
                field_changed='absence_edit',
                old_value='',
                new_value=f'{date_start} - {date_end} ({absence_type})'
            )
            db.session.add(change)
            db.session.commit()

            flash('Заявка обновлена и отправлена на подтверждение', 'success')
            return redirect(url_for('employee_history'))
        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка: {e}', 'danger')

    return render_template(
        'employee/absence_edit.html',
        absence=absence,
        year=year,
        month=month,
        month_days=month_days,
        month_name=MONTHS_RU[month-1],
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        selected_start=absence.date_start.isoformat() if absence.date_start else '',
        selected_end=absence.date_end.isoformat() if absence.date_end else ''
    )

# ---------- АДМИНИСТРАТОР ----------

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    pending_schedules = Schedule.query.filter_by(status='pending').count()
    pending_absences = Absence.query.filter_by(status='pending').count()
    pending_attendances = Attendance.query.filter_by(status='pending').count()
    total_pending = pending_schedules + pending_absences + pending_attendances

    today = date.today()
    today_plans = Schedule.query.filter_by(date=today, status='approved').all()
    today_attendance = Attendance.query.filter_by(date=today).all()

    return render_template(
        'admin/admin_dashboard.html',
        pending_schedules=pending_schedules,
        pending_absences=pending_absences,
        pending_attendances=pending_attendances,
        total_pending=total_pending,
        today_plans=today_plans,
        today_attendance=today_attendance
    )

@app.route('/admin/pending_requests')
@login_required
def admin_pending_requests():
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    today = date.today()
    cal_year = request.args.get('cal_year', today.year, type=int)
    cal_month = request.args.get('cal_month', today.month, type=int)
    if cal_month < 1 or cal_month > 12:
        cal_month = today.month

    # Флаг: показать все заявки (без фильтрации по месяцу)
    show_all = request.args.get('show_all', '0') == '1'

    first_day = date(cal_year, cal_month, 1)
    last_day = date(cal_year, cal_month, calendar.monthrange(cal_year, cal_month)[1])

    if show_all:
        pending_schedules = Schedule.query.filter_by(status='pending').order_by(Schedule.date).all()
        pending_attendances = Attendance.query.filter_by(status='pending').order_by(Attendance.date).all()
        pending_absences = Absence.query.filter_by(status='pending').order_by(Absence.date_start).all()
        pending_deletion_absences = Absence.query.filter_by(status='pending_deletion').order_by(Absence.date_start).all()
    else:
        pending_schedules = Schedule.query.filter(
            Schedule.status == 'pending',
            Schedule.date >= first_day,
            Schedule.date <= last_day
        ).order_by(Schedule.date).all()

        pending_attendances = Attendance.query.filter(
            Attendance.status == 'pending',
            Attendance.date >= first_day,
            Attendance.date <= last_day
        ).order_by(Attendance.date).all()

        pending_absences = Absence.query.filter(
            Absence.status == 'pending',
            Absence.date_start <= last_day,
            Absence.date_end >= first_day
        ).order_by(Absence.date_start).all()

        pending_deletion_absences = Absence.query.filter(
            Absence.status == 'pending_deletion',
            Absence.date_start <= last_day,
            Absence.date_end >= first_day
        ).order_by(Absence.date_start).all()

    # Календарь
    month_days = calendar.Calendar().monthdayscalendar(cal_year, cal_month)

    # Подсветка дней с заявками (всегда по выбранному месяцу)
    days_with_requests = {}
    all_pending_schedules = Schedule.query.filter_by(status='pending').all()
    all_pending_attendances = Attendance.query.filter_by(status='pending').all()
    all_pending_absences = Absence.query.filter_by(status='pending').all()
    all_pending_deletions = Absence.query.filter_by(status='pending_deletion').all()

    for s in all_pending_schedules:
        if s.date.year == cal_year and s.date.month == cal_month:
            d = days_with_requests.setdefault(s.date.day, {'schedule': 0, 'attendance': 0, 'absence': 0})
            d['schedule'] += 1
    for a in all_pending_attendances:
        if a.date.year == cal_year and a.date.month == cal_month:
            d = days_with_requests.setdefault(a.date.day, {'schedule': 0, 'attendance': 0, 'absence': 0})
            d['attendance'] += 1
    for ab in all_pending_absences + all_pending_deletions:
        d_cur = ab.date_start
        while d_cur <= ab.date_end:
            if d_cur.year == cal_year and d_cur.month == cal_month:
                d = days_with_requests.setdefault(d_cur.day, {'schedule': 0, 'attendance': 0, 'absence': 0})
                d['absence'] += 1
            d_cur += timedelta(days=1)

    if cal_month == 1:
        prev_year, prev_month = cal_year - 1, 12
    else:
        prev_year, prev_month = cal_year, cal_month - 1
    if cal_month == 12:
        next_year, next_month = cal_year + 1, 1
    else:
        next_year, next_month = cal_year, cal_month + 1

    return render_template(
        'admin/pending_requests.html',
        pending_schedules=pending_schedules,
        pending_absences=pending_absences,
        pending_attendances=pending_attendances,
        pending_deletion_absences=pending_deletion_absences,
        calendar_year=cal_year,
        calendar_month=cal_month,
        calendar_month_name=MONTHS_RU[cal_month - 1],
        calendar_days=month_days,
        days_with_requests=days_with_requests,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        show_all=show_all
    )

@app.route('/admin/approve_schedule/<int:schedule_id>')
@login_required
def admin_approve_schedule(schedule_id):
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))
    schedule = Schedule.query.get_or_404(schedule_id)
    schedule.status = 'approved'
    db.session.commit()
    change = ChangeLog(user_id=current_user.id, target_user_id=schedule.user_id, field_changed='schedule_approve', old_value='pending', new_value='approved')
    db.session.add(change)
    db.session.commit()
    flash('План подтверждён', 'success')
    return redirect(url_for('admin_pending_requests', cal_year=request.args.get('cal_year'), cal_month=request.args.get('cal_month'), show_all=request.args.get('show_all', '0')))

@app.route('/admin/reject_schedule/<int:schedule_id>')
@login_required
def admin_reject_schedule(schedule_id):
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))
    schedule = Schedule.query.get_or_404(schedule_id)
    schedule.status = 'rejected'
    db.session.commit()
    change = ChangeLog(user_id=current_user.id, target_user_id=schedule.user_id, field_changed='schedule_reject', old_value='pending', new_value='rejected')
    db.session.add(change)
    db.session.commit()
    flash('План отклонён', 'warning')
    return redirect(url_for('admin_pending_requests', cal_year=request.args.get('cal_year'), cal_month=request.args.get('cal_month'), show_all=request.args.get('show_all', '0')))

@app.route('/admin/approve_absence/<int:absence_id>')
@login_required
def admin_approve_absence(absence_id):
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))
    absence = Absence.query.get_or_404(absence_id)
    absence.status = 'approved'
    db.session.commit()
    change = ChangeLog(user_id=current_user.id, target_user_id=absence.user_id, field_changed='absence_approve', old_value='pending', new_value='approved')
    db.session.add(change)
    db.session.commit()
    flash('Отпуск/больничный подтверждён', 'success')
    return redirect(url_for('admin_pending_requests', cal_year=request.args.get('cal_year'), cal_month=request.args.get('cal_month'), show_all=request.args.get('show_all', '0')))

@app.route('/admin/reject_absence/<int:absence_id>')
@login_required
def admin_reject_absence(absence_id):
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))
    absence = Absence.query.get_or_404(absence_id)
    absence.status = 'rejected'
    db.session.commit()
    change = ChangeLog(user_id=current_user.id, target_user_id=absence.user_id, field_changed='absence_reject', old_value='pending', new_value='rejected')
    db.session.add(change)
    db.session.commit()
    flash('Отпуск/больничный отклонён', 'warning')
    return redirect(url_for('admin_pending_requests', cal_year=request.args.get('cal_year'), cal_month=request.args.get('cal_month'), show_all=request.args.get('show_all', '0')))

@app.route('/admin/approve_attendance/<int:attendance_id>')
@login_required
def admin_approve_attendance(attendance_id):
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))
    attendance = Attendance.query.get_or_404(attendance_id)
    attendance.status = 'confirmed'
    db.session.commit()
    change = ChangeLog(user_id=current_user.id, target_user_id=attendance.user_id, field_changed='attendance_approve', old_value='pending', new_value='confirmed')
    db.session.add(change)
    db.session.commit()
    flash('Фактическое время подтверждено', 'success')
    return redirect(url_for('admin_pending_requests', cal_year=request.args.get('cal_year'), cal_month=request.args.get('cal_month'), show_all=request.args.get('show_all', '0')))

@app.route('/admin/reject_attendance/<int:attendance_id>')
@login_required
def admin_reject_attendance(attendance_id):
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))
    attendance = Attendance.query.get_or_404(attendance_id)
    attendance.status = 'rejected'
    db.session.commit()
    change = ChangeLog(user_id=current_user.id, target_user_id=attendance.user_id, field_changed='attendance_reject', old_value='pending', new_value='rejected')
    db.session.add(change)
    db.session.commit()
    flash('Фактическое время отклонено', 'warning')
    return redirect(url_for('admin_pending_requests', cal_year=request.args.get('cal_year'), cal_month=request.args.get('cal_month'), show_all=request.args.get('show_all', '0')))

@app.route('/admin/approve_absence_deletion/<int:absence_id>')
@login_required
def admin_approve_absence_deletion(absence_id):
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))
    absence = Absence.query.get_or_404(absence_id)
    if absence.status == 'pending_deletion':
        db.session.delete(absence)
        db.session.commit()
        flash('Отпуск удалён', 'success')
    else:
        flash('Нет запроса на удаление', 'warning')
    return redirect(url_for('admin_pending_requests', cal_year=request.args.get('cal_year'), cal_month=request.args.get('cal_month'), show_all=request.args.get('show_all', '0')))

@app.route('/admin/reject_absence_deletion/<int:absence_id>')
@login_required
def admin_reject_absence_deletion(absence_id):
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))
    absence = Absence.query.get_or_404(absence_id)
    if absence.status == 'pending_deletion':
        absence.status = 'approved' if absence.file_path else 'rejected'
        db.session.commit()
        flash('Запрос на удаление отклонён', 'warning')
    else:
        flash('Нет запроса на удаление', 'warning')
    return redirect(url_for('admin_pending_requests', cal_year=request.args.get('cal_year'), cal_month=request.args.get('cal_month'), show_all=request.args.get('show_all', '0')))

@app.route('/admin/history')
@login_required
def admin_history():
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    schedules = Schedule.query.order_by(Schedule.date.desc()).all()
    absences = Absence.query.order_by(Absence.date_start.desc()).all()
    attendances = Attendance.query.order_by(Attendance.date.desc()).all()

    return render_template(
        'admin/history.html',
        schedules=schedules,
        absences=absences,
        attendances=attendances
    )

@app.route('/admin/clear_history', methods=['POST'])
@login_required
def admin_clear_history():
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    Schedule.query.delete()
    Attendance.query.delete()
    Absence.query.delete()
    ChangeLog.query.delete()
    db.session.commit()
    flash('История заявок очищена', 'success')
    return redirect(url_for('admin_history'))

@app.route('/admin/users')
@login_required
def admin_users():
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    users = User.query.all()
    return render_template('admin/users.html', users=users)


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
def admin_delete_user(user_id):
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('Нельзя удалить самого себя', 'danger')
        return redirect(url_for('admin_users'))

    Schedule.query.filter_by(user_id=user.id).delete()
    Attendance.query.filter_by(user_id=user.id).delete()
    Absence.query.filter_by(user_id=user.id).delete()
    ChangeLog.query.filter((ChangeLog.user_id == user.id) | (ChangeLog.target_user_id == user.id)).delete()

    db.session.delete(user)
    db.session.commit()
    flash(f'Пользователь {user.full_name} удалён', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/create', methods=['POST'])
@login_required
def admin_create_user():
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    full_name = request.form.get('full_name')
    email = request.form.get('email')
    password = request.form.get('password')
    role = request.form.get('role', 'employee')
    telegram_id = request.form.get('telegram_id', '').strip()

    # Нормализуем Telegram ID: добавляем @, если не указан
    if telegram_id and not telegram_id.startswith('@'):
        telegram_id = '@' + telegram_id

    if not full_name or not email or not password:
        flash('Заполните все поля', 'danger')
        return redirect(url_for('admin_users'))

    if User.query.filter_by(email=email).first():
        flash('Пользователь с таким email уже существует', 'danger')
        return redirect(url_for('admin_users'))

    # Проверка уникальности Telegram ID
    if telegram_id and User.query.filter_by(telegram_id=telegram_id).first():
        flash('Пользователь с таким Telegram ID уже существует', 'danger')
        return redirect(url_for('admin_users'))

    user = User(
        full_name=full_name,
        email=email,
        password_hash=generate_password_hash(password),
        role=role,
        telegram_id=telegram_id or None
    )
    db.session.add(user)
    db.session.commit()

    flash('Пользователь создан', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def admin_edit_user(user_id):
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    user = User.query.get_or_404(user_id)

    if request.method == 'POST':
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        password = request.form.get('password')
        role = request.form.get('role')
        telegram_id = request.form.get('telegram_id', '').strip()

        # Нормализация @
        if telegram_id and not telegram_id.startswith('@'):
            telegram_id = '@' + telegram_id

        if not full_name or not email:
            flash('Имя и email обязательны', 'danger')
            return redirect(url_for('admin_edit_user', user_id=user.id))

        existing = User.query.filter(User.email == email, User.id != user.id).first()
        if existing:
            flash('Пользователь с таким email уже существует', 'danger')
            return redirect(url_for('admin_edit_user', user_id=user.id))

        # Проверка уникальности Telegram ID
        if telegram_id:
            existing_tg = User.query.filter(User.telegram_id == telegram_id, User.id != user.id).first()
            if existing_tg:
                flash('Пользователь с таким Telegram ID уже существует', 'danger')
                return redirect(url_for('admin_edit_user', user_id=user.id))

        user.full_name = full_name
        user.email = email
        if password:
            user.password_hash = generate_password_hash(password)
        user.role = role
        user.telegram_id = telegram_id or None

        db.session.commit()
        flash('Данные пользователя обновлены', 'success')
        return redirect(url_for('admin_users'))

    return render_template('admin/edit_user.html', user=user)

@app.route('/admin/month_summary', methods=['GET'])
@login_required
def admin_month_summary():
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)
    if month < 1 or month > 12:
        month = date.today().month

    start_date = date(year, month, 1)
    end_date = date(year, month, calendar.monthrange(year, month)[1])

    employees = User.query.filter_by(role='employee', status='active').all()

    summary = []
    for emp in employees:
        total_worked_minutes = 0
        total_eff_minutes = 0
        total_late_minutes = 0

        attendances = Attendance.query.filter(
            Attendance.user_id == emp.id,
            Attendance.date >= start_date,
            Attendance.date <= end_date,
            Attendance.status == 'confirmed'
        ).all()

        for att in attendances:
            if att.actual_start and att.actual_end:
                worked = calculate_worked_hours(att.actual_start, att.actual_end)
                worked_min = int(worked.total_seconds() // 60)
                eff_min = int(worked_min * att.efficiency)
                total_worked_minutes += worked_min
                total_eff_minutes += eff_min
                if att.early_start and att.early_start > 0:
                    total_late_minutes += att.early_start

        avg_efficiency = (total_eff_minutes / total_worked_minutes * 100) if total_worked_minutes > 0 else 0
        final_minutes = total_eff_minutes - total_late_minutes

        summary.append({
            'user': emp,
            'worked_hours': total_worked_minutes / 60,
            'avg_efficiency': avg_efficiency,
            'late_hours': total_late_minutes / 60,
            'final_hours': final_minutes / 60
        })

    return render_template(
        'admin/month_summary.html',
        summary=summary,
        year=year,
        month=month,
        month_name=MONTHS_RU[month - 1]
    )

@app.route('/admin/export')
@login_required
def admin_export():
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)
    if month < 1 or month > 12:
        month = date.today().month

    start_date = date(year, month, 1)
    end_date = date(year, month, calendar.monthrange(year, month)[1])

    employees = User.query.filter_by(role='employee', status='active').all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Итоги {month}.{year}"

    headers = ['Сотрудник', 'Отработано часов', 'Средний e%', 'Опоздания (часов)', 'Итого часов']
    ws.append(headers)

    for emp in employees:
        total_worked_minutes = 0
        total_eff_minutes = 0
        total_late_minutes = 0
        attendances = Attendance.query.filter(
            Attendance.user_id == emp.id,
            Attendance.date >= start_date,
            Attendance.date <= end_date,
            Attendance.status == 'confirmed'
        ).all()
        for att in attendances:
            if att.actual_start and att.actual_end:
                worked = calculate_worked_hours(att.actual_start, att.actual_end)
                worked_min = int(worked.total_seconds() // 60)
                eff_min = int(worked_min * att.efficiency)
                total_worked_minutes += worked_min
                total_eff_minutes += eff_min
                if att.early_start and att.early_start > 0:
                    total_late_minutes += att.early_start

        avg_efficiency = (total_eff_minutes / total_worked_minutes * 100) if total_worked_minutes > 0 else 0
        final_minutes = total_eff_minutes - total_late_minutes

        ws.append([
            emp.full_name,
            round(total_worked_minutes / 60, 2),
            round(avg_efficiency, 1),
            round(total_late_minutes / 60, 2),
            round(final_minutes / 60, 2)
        ])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=f'summary_{year}_{month}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/employee/day_edit', methods=['GET', 'POST'])
@login_required
def employee_day_edit():
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    date_str = request.args.get('date')
    if not date_str:
        flash('Не указана дата', 'danger')
        return redirect(url_for('employee_dashboard'))

    try:
        current_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Некорректная дата', 'danger')
        return redirect(url_for('employee_dashboard'))

    is_future = current_date > date.today()

    schedule = Schedule.query.filter_by(user_id=current_user.id, date=current_date).first()
    attendance = Attendance.query.filter_by(user_id=current_user.id, date=current_date).first()

    absence = Absence.query.filter(
        Absence.user_id == current_user.id,
        Absence.date_start <= current_date,
        Absence.date_end >= current_date,
        Absence.status == 'approved'
    ).first()

    if request.method == 'POST':
        is_day_off = request.form.get('is_day_off') == 'on'
        plan_start_str = request.form.get('plan_start')
        plan_end_str = request.form.get('plan_end')
        actual_start_str = request.form.get('actual_start')
        actual_end_str = request.form.get('actual_end')
        efficiency_str = request.form.get('efficiency')

        if is_future:
            actual_start_str = None
            actual_end_str = None
            efficiency_str = None

        error_message = None
        if not is_day_off and (not plan_start_str or not plan_end_str):
            error_message = 'Заполните время плана или отметьте выходной'
        elif actual_start_str and actual_end_str:
            actual_start = parse_time_string(actual_start_str)
            actual_end = parse_time_string(actual_end_str)
            if actual_start >= actual_end:
                error_message = 'Время ухода должно быть позже времени прихода'
        elif (actual_start_str and not actual_end_str) or (not actual_start_str and actual_end_str):
            error_message = 'Заполните оба фактических времени'

        if error_message:
            flash(error_message, 'danger')
            form_data = {
                'is_day_off': is_day_off,
                'plan_start_str': plan_start_str,
                'plan_end_str': plan_end_str,
                'actual_start_str': actual_start_str,
                'actual_end_str': actual_end_str,
                'efficiency_str': efficiency_str
            }
            return render_template(
                'employee/day_edit.html',
                current_date=current_date,
                schedule=schedule,
                attendance=attendance,
                is_future=is_future,
                absence=absence,
                form_data=form_data,
                error_message=error_message
            )

        planned_start = parse_time_string(plan_start_str) if not is_day_off and plan_start_str else None
        planned_end = parse_time_string(plan_end_str) if not is_day_off and plan_end_str else None

        actual_start = None
        actual_end = None
        efficiency = 1.0
        if not is_future and actual_start_str and actual_end_str:
            actual_start = parse_time_string(actual_start_str)
            actual_end = parse_time_string(actual_end_str)
            if efficiency_str:
                efficiency = float(efficiency_str) / 100

        plan_status = 'pending' if absence else 'approved'
        attendance_status = 'pending' if absence or (not is_future and current_date < date.today()) else 'confirmed'

        if schedule:
            schedule.is_day_off = is_day_off
            schedule.planned_start = planned_start
            schedule.planned_end = planned_end
            schedule.status = plan_status
        else:
            schedule = Schedule(
                user_id=current_user.id,
                date=current_date,
                planned_start=planned_start,
                planned_end=planned_end,
                is_day_off=is_day_off,
                status=plan_status
            )
            db.session.add(schedule)

        if not is_future and actual_start and actual_end:
            early_start = 0
            if schedule and schedule.planned_start and not schedule.is_day_off:
                planned_minutes = time_to_minutes(schedule.planned_start)
                actual_minutes = time_to_minutes(actual_start)
                early_start = actual_minutes - planned_minutes

            if attendance:
                attendance.actual_start = actual_start
                attendance.actual_end = actual_end
                attendance.efficiency = efficiency
                attendance.early_start = early_start
                attendance.status = attendance_status
            else:
                attendance = Attendance(
                    user_id=current_user.id,
                    date=current_date,
                    actual_start=actual_start,
                    actual_end=actual_end,
                    efficiency=efficiency,
                    early_start=early_start,
                    status=attendance_status
                )
                db.session.add(attendance)
        elif attendance and not is_future:
            db.session.delete(attendance)
        elif is_future and attendance:
            db.session.delete(attendance)

        db.session.commit()

        change = ChangeLog(
            user_id=current_user.id,
            target_user_id=current_user.id,
            field_changed='day_edit',
            old_value='',
            new_value=f'{current_date} plan={plan_start_str}-{plan_end_str}, fact={actual_start_str}-{actual_end_str}'
        )
        db.session.add(change)
        db.session.commit()

        if attendance_status == 'pending' or plan_status == 'pending':
            flash('Данные сохранены и ожидают подтверждения администратора', 'warning')
        else:
            flash('Данные сохранены', 'success')
        return redirect(url_for('employee_dashboard', year=current_date.year, month=current_date.month))

    form_data = {
        'is_day_off': schedule.is_day_off if schedule else False,
        'plan_start_str': schedule.planned_start.strftime('%H:%M') if schedule and schedule.planned_start else '',
        'plan_end_str': schedule.planned_end.strftime('%H:%M') if schedule and schedule.planned_end else '',
        'actual_start_str': attendance.actual_start.strftime('%H:%M') if attendance and attendance.actual_start else '',
        'actual_end_str': attendance.actual_end.strftime('%H:%M') if attendance and attendance.actual_end else '',
        'efficiency_str': str(int(attendance.efficiency * 100)) if attendance and attendance.efficiency else '100'
    }

    return render_template(
        'employee/day_edit.html',
        current_date=current_date,
        schedule=schedule,
        attendance=attendance,
        is_future=is_future,
        absence=absence,
        form_data=form_data,
        error_message=None
    )

@app.route('/employee/day_clear/<string:date_str>', methods=['POST'])
@login_required
def employee_day_clear(date_str):
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Некорректная дата', 'danger')
        return redirect(url_for('employee_dashboard'))

    Schedule.query.filter_by(user_id=current_user.id, date=d).delete()
    Attendance.query.filter_by(user_id=current_user.id, date=d).delete()
    db.session.commit()

    flash(f'Данные за {d.strftime("%d.%m.%Y")} удалены', 'success')
    return redirect(url_for('employee_dashboard', year=d.year, month=d.month))

@app.route('/employee/clear_history', methods=['POST'])
@login_required
def employee_clear_history():
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())

    Schedule.query.filter(
        Schedule.user_id == current_user.id,
        Schedule.date < start_of_week
    ).delete()
    Attendance.query.filter(
        Attendance.user_id == current_user.id,
        Attendance.date < start_of_week
    ).delete()
    Absence.query.filter(
        Absence.user_id == current_user.id,
        Absence.date_end < start_of_week
    ).delete()

    db.session.commit()

    flash('История очищена (данные текущей недели сохранены)', 'success')
    return redirect(url_for('employee_history'))

@app.route('/admin/weekly_schedule')
@login_required
def admin_weekly_schedule():
    if current_user.role != 'admin':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))

    week_offset = request.args.get('offset', 0, type=int)
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    end_of_week = start_of_week + timedelta(days=6)

    employees = User.query.filter_by(role='employee', status='active').all()

    days = []
    for i in range(7):
        day = start_of_week + timedelta(days=i)
        plans = {}
        for emp in employees:
            sch = Schedule.query.filter_by(user_id=emp.id, date=day).first()
            att = Attendance.query.filter_by(user_id=emp.id, date=day).first()
            plans[emp.id] = {'schedule': sch, 'attendance': att}
        days.append({
            'date': day,
            'day_name': DAYS_RU[day.weekday()],
            'plans': plans
        })

    month = start_of_week.month
    year = start_of_week.year
    cal = calendar.Calendar()
    month_days = cal.monthdayscalendar(year, month)
    num_days = calendar.monthrange(year, month)[1]

    month_plans = {}
    for day in range(1, num_days + 1):
        d = date(year, month, day)
        day_plans = []
        for emp in employees:
            sch = Schedule.query.filter_by(user_id=emp.id, date=d).first()
            att = Attendance.query.filter_by(user_id=emp.id, date=d).first()
            day_plans.append({
                'name': emp.full_name,
                'schedule_start': sch.planned_start.strftime('%H:%M') if sch and sch.planned_start else None,
                'schedule_end': sch.planned_end.strftime('%H:%M') if sch and sch.planned_end else None,
                'schedule_is_day_off': sch.is_day_off if sch else False,
                'attendance_start': att.actual_start.strftime('%H:%M') if att and att.actual_start else None,
                'attendance_end': att.actual_end.strftime('%H:%M') if att and att.actual_end else None,
            })
        month_plans[day] = day_plans

    return render_template(
        'admin/weekly_schedule.html',
        days=days,
        employees=employees,
        start_of_week=start_of_week,
        end_of_week=end_of_week,
        week_offset=week_offset,
        month_days=month_days,
        month=month,
        year=year,
        month_name=MONTHS_RU[month-1],
        month_plans=month_plans,
        num_days=num_days
    )

@app.route('/employee/absence/<int:absence_id>/request_delete', methods=['POST'])
@login_required
def employee_absence_request_delete(absence_id):
    if current_user.role != 'employee':
        flash('Доступ запрещён', 'danger')
        return redirect(url_for('index'))
    absence = Absence.query.get_or_404(absence_id)
    if absence.user_id != current_user.id:
        abort(403)
    if absence.status == 'pending_deletion':
        flash('Запрос на удаление уже отправлен', 'warning')
    else:
        absence.status = 'pending_deletion'
        db.session.commit()
        flash('Запрос на удаление отправлен администратору', 'warning')
    return redirect(url_for('employee_history'))
