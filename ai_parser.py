# ai_parser.py
"""
Модуль разбора свободных сообщений через ИИ.
Работает с любым OpenAI-совместимым API:
  - OpenAI:      https://api.openai.com/v1
  - OpenRouter:  https://openrouter.ai/api/v1
  - Ollama:      http://localhost:11434/v1
  - GigaChat:    https://gigachat.devices.sberbank.ru/api/v1 (через /chat/completions)
"""
import os
import re
import json
import logging
from datetime import date, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)


class AIParser:
    def __init__(self, api_key, base_url, model, proxy=None, timeout=45.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.proxy = proxy or None
        self.timeout = timeout

    async def analyze(self, text, user_full_name, today=None):
        if today is None:
            today = date.today()

        system_prompt = self._build_system_prompt(today)
        user_prompt = f"Сотрудник: {user_full_name}\nСообщение: {text}"

        try:
            async with httpx.AsyncClient(proxy=self.proxy, timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://worktracker.local",
                        "X-Title": "WorkTracker",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
                data = response.json()
                content = data['choices'][0]['message']['content']
                return self._parse_response(content)
        except httpx.HTTPStatusError as e:
            logger.error(f"AI HTTP error: {e.response.status_code} {e.response.text[:200]}")
            return {'action': 'error',
                    'response': f'⚠️ Ошибка ИИ: {e.response.status_code}'}
        except Exception as e:
            logger.error(f"AI analyze error: {e}")
            return {'action': 'error', 'response': f'⚠️ Ошибка ИИ: {e}'}

    def _build_system_prompt(self, today):
        days_ru = ['понедельник', 'вторник', 'среда', 'четверг',
                   'пятница', 'суббота', 'воскресенье']
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)

        return f"""Ты — ассистент системы WorkTracker для учёта рабочего времени.
Разбирай сообщения сотрудников и возвращай СТРОГО JSON (без markdown).

Контекст:
- Сегодня: {today.isoformat()} ({days_ru[today.weekday()]})
- Вчера: {yesterday.isoformat()}
- Завтра: {tomorrow.isoformat()}

Формат ответа:
{{
  "action": "plan" | "fact" | "note" | "absence" | "query" | "chat" | "unknown",
  "date": "YYYY-MM-DD" | null,
  "start": "HH:MM" | null,
  "end": "HH:MM" | null,
  "efficiency": 0-100 | null,
  "tasks": ["..."] | null,
  "absence_type": "vacation" | "sick" | "other" | null,
  "date_end": "YYYY-MM-DD" | null,
  "query_type": "summary" | "schedule" | "who" | null,
  "response": "краткий ответ пользователю на русском"
}}

Что означает action:
- "plan" — план на будущий день (что собирается делать)
- "fact" — факт работы (сколько часов работал)
- "note" — перечень того, что уже сделал (без времени)
- "absence" — заявка на отпуск/больничный/отгул
- "query" — вопрос про статистику/расписание
- "chat" — обычная беседа
- "unknown" — не удалось понять

Примеры:
- "сегодня работал с 10 до 18" → {{"action":"fact","date":"{today.isoformat()}","start":"10:00","end":"18:00","response":"Записал факт"}}
- "завтра с 9 до 17" → {{"action":"plan","date":"{tomorrow.isoformat()}","start":"09:00","end":"17:00","response":"Записал план"}}
- "сделал отчёт и провёл встречу" → {{"action":"note","tasks":["отчёт","встреча"],"response":"Записал задачи"}}
- "хочу в отпуск с 5 по 10 ноября" → {{"action":"absence","absence_type":"vacation","date":"2026-11-05","date_end":"2026-11-10","response":"Заявка на отпуск"}}
- "сколько я наработал за месяц?" → {{"action":"query","query_type":"summary","response":"Смотрю статистику"}}
- "привет" → {{"action":"chat","response":"Привет! Чем помочь?"}}

Правила:
- Даты в формате YYYY-MM-DD
- Если год не указан — текущий (или следующий, если дата прошла)
- Время в формате HH:MM (24ч)
- Не выдумывай — если не ясно, ставь null
- Только JSON, никаких пояснений"""

    def _parse_response(self, content):
        content = content.strip()
        if content.startswith('```'):
            content = re.sub(r'^```(?:json)?\s*', '', content)
            content = re.sub(r'\s*```$', '', content)

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        return {'action': 'unknown', 'response': '⚠️ ИИ вернул нечитаемый ответ.'}


def get_ai_parser_from_env():
    """Создаёт AIParser из .env, если он настроен."""
    enabled = os.environ.get('AI_ENABLED', '').strip().lower() in ('1', 'true', 'yes')
    api_key = os.environ.get('AI_API_KEY', '').strip()
    base_url = os.environ.get('AI_BASE_URL', 'https://api.openai.com/v1').strip()
    model = os.environ.get('AI_MODEL', 'gpt-4o-mini').strip()
    proxy = os.environ.get('AI_PROXY', '').strip() or None

    if not enabled or not api_key:
        return None

    return AIParser(api_key=api_key, base_url=base_url, model=model, proxy=proxy)