from app import app, db
from models import User
from werkzeug.security import generate_password_hash

with app.app_context():
    db.create_all()  # создаём таблицы, если их нет
    admin = User.query.filter_by(role='admin').first()
    if not admin:
        admin = User(
            full_name='Администратор',
            email='admin@example.com',
            password_hash=generate_password_hash('admin123'),
            role='admin',
            work_rate=80
        )
        db.session.add(admin)
        db.session.commit()
        print("Администратор создан: admin@example.com / admin123")
    else:
        print("Админ уже существует")