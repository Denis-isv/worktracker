from app import app, db
from models import User
from werkzeug.security import generate_password_hash

with app.app_context():
    emp = User.query.filter_by(email='emp@example.com').first()
    if not emp:
        emp = User(
            full_name='Тестовый Сотрудник',
            email='emp@example.com',
            password_hash=generate_password_hash('emp123'),
            role='employee',
            work_rate=80
        )
        db.session.add(emp)
        db.session.commit()
        print("Сотрудник создан: emp@example.com / emp123")
    else:
        print("Сотрудник уже существует")