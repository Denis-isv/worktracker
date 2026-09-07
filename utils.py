from datetime import datetime, timedelta, time

def time_to_minutes(t):
    """Переводит time в минуты от полуночи."""
    return t.hour * 60 + t.minute

def minutes_to_time(minutes):
    """Переводит минуты от полуночи в time."""
    minutes = minutes % (24 * 60)
    hours = minutes // 60
    mins = minutes % 60
    return time(hours, mins)

def timedelta_to_minutes(td):
    """Переводит timedelta в целые минуты."""
    return int(td.total_seconds() // 60)

def calculate_worked_hours(start: time, end: time) -> timedelta:
    """Разница между end и start (если end < start, считаем, что конец на следующий день)."""
    start_min = time_to_minutes(start)
    end_min = time_to_minutes(end)
    if end_min < start_min:
        end_min += 24 * 60
    diff = end_min - start_min
    return timedelta(minutes=diff)

def format_timedelta_hhmm(td: timedelta) -> str:
    """Форматирует timedelta в ЧЧ:ММ."""
    total_minutes = int(td.total_seconds() // 60)
    hours = total_minutes // 60
    mins = total_minutes % 60
    return f"{hours:02d}:{mins:02d}"

def parse_time_string(s: str) -> time:
    """Парсит строку формата HH:MM в time."""
    h, m = map(int, s.split(':'))
    return time(h, m)