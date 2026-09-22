from flask import Flask
from config import Config
from models import db
from flask_login import LoginManager

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
@app.template_filter('time_diff')
def time_diff_filter(end, start):
    if not end or not start:
        return ''
    from utils import calculate_worked_hours, format_timedelta_hhmm
    return format_timedelta_hhmm(calculate_worked_hours(start, end))

# Функция загрузки пользователя по ID
@login_manager.user_loader
def load_user(user_id):
    from models import User
    return User.query.get(int(user_id))

# Импорт маршрутов после инициализации
from routes import *

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
