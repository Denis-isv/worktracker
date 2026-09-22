from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    monthly_hours = db.Column(db.Integer, default=160)  # норма часов в месяц
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    role = db.Column(db.String(20), default='employee')
    telegram_id = db.Column(db.String(50), unique=True)
    work_rate = db.Column(db.Integer, default=80)  # ставка часов в месяц
    status = db.Column(db.String(20), default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    schedules = db.relationship('Schedule', backref='user', lazy=True)
    attendances = db.relationship('Attendance', backref='user', lazy=True)
    absences = db.relationship('Absence', backref='user', lazy=True)
    changes = db.relationship('ChangeLog', backref='user', lazy=True)

class Schedule(db.Model):
    __tablename__ = 'schedules'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    planned_start = db.Column(db.Time, nullable=True)
    planned_end = db.Column(db.Time, nullable=True)
    efficiency = db.Column(db.Float, default=1.0)
    status = db.Column(db.String(20), default='pending')
    is_day_off = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)

class Attendance(db.Model):
    __tablename__ = 'attendance'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    actual_start = db.Column(db.Time)
    actual_end = db.Column(db.Time)
    efficiency = db.Column(db.Float, default=1.0)  # работник ставит после смены
    status = db.Column(db.String(20), default='pending')  # pending, confirmed
    note = db.Column(db.String(200))
    early_start = db.Column(db.Integer)  # разница в минутах (положит = опоздание, отриц = ранний приход)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)

class Absence(db.Model):
    __tablename__ = 'absences'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    date_start = db.Column(db.Date, nullable=False)
    date_end = db.Column(db.Date, nullable=False)
<<<<<<< HEAD
    type = db.Column(db.String(20))
    custom_type = db.Column(db.String(200))  # для «другое»
=======
    type = db.Column(db.String(20))  # vacation, sick, other
>>>>>>> 5f8a7730096432ab1a144800397b81ce1675c230
    status = db.Column(db.String(20), default='pending')
    file_path = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ChangeLog(db.Model):
    __tablename__ = 'change_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # кто изменил
    target_user_id = db.Column(db.Integer)
    field_changed = db.Column(db.String(100))
    old_value = db.Column(db.String(200))
    new_value = db.Column(db.String(200))
    changed_at = db.Column(db.DateTime, default=datetime.utcnow)

class Notification(db.Model):
    __tablename__ = 'notifications'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # кому отправлять
    chat_id = db.Column(db.String(50))  # telegram chat id (можно брать из User)
    message = db.Column(db.String(500))
    status = db.Column(db.String(20), default='pending')  # pending, sent
    created_at = db.Column(db.DateTime, default=datetime.utcnow)