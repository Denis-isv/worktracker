from app import app, db
from sqlalchemy import text

with app.app_context():
    try:
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE schedules ADD COLUMN plan_text TEXT;"))
            conn.commit()
        print("✅ Колонка plan_text добавлена в таблицу schedules")
    except Exception as e:
        if "duplicate column name" in str(e).lower():
            print("ℹ️ Колонка plan_text уже существует")
        else:
            print(f"❌ Ошибка: {e}")