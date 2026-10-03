# ai_parser.py
"""
Модуль разбора свободных сообщений через ИИ.
Поддерживает ротацию моделей при ошибке 429 (лимит).
Работает с OpenAI-совместимым API:
  - OpenAI:      https://api.openai.com/v1
  - OpenRouter:  https://openrouter.ai/api/v1
  - Ollama:      http://localhost:11434/v1
"""
import os
import re
import json
import logging
from datetime import date, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)


class AIParser:
    def __init__(self, api_key, base_url, models, proxy=None, timeout=45.0):
        """
        models — строка или список моделей для ротации.
        Пример строки: "qwen/qwen3.8-27b:free,poolside/laguna-s-2.1:free"
        """
        if isinstance(models, str):
            models = [m.strip() for m in models.split(',') if m.strip()]
        self.models = models if models else ['gpt-4o-mini']
        self.current_idx = 0
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.proxy = proxy or None
        self.timeout = timeout

    @property
    def model(self):
        return self.models[self.current_idx]

    @property
    def base_url_(self):
        return self.base_url

    def _next_model(self):
        self.current_idx = (self.current_idx + 1) % len(self.models)
        logger.warning(f"🔄 Переключаюсь на модель: {self.model}")

    async def analyze(self, text, user_full_name, today=None):
        if today is None:
            today = date.today()

        system_prompt = self._build_system_prompt(today)
        user_prompt = f"Сотрудник: {user_full_name}\nСообщение: {text}"

        last_error = None
        attempts = len(self.models) + 1  # +1 чтобы обойти все и вернуться

        for attempt in range(attempts):
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

                    # 429 — лимит, пробуем следующую модель
                    if response.status_code == 429:
                        logger.warning(f"429 на {self.model}, переключаюсь")
                        last_error = "все модели перегружены"
                        self._next_model()
                        continue

                    # 404 или 400 — модель недоступна, тоже пробуем следующую
                    if response.status_code in (400, 404):
                        logger.warning(f"{response.status_code} на {self.model}, переключаюсь")
                        last_error = f"модель недоступна ({response.status_code})"
                        self._next_model()
                        continue

                    response.raise_for_status()
                    data = response.json()
                    content = data['choices'][0]['message']['content']
                    return self._parse_response(content)

            except httpx.HTTPStatusError as e:
                last_error = f"HTTP {e.response.status_code}"
                logger.error(f"AI HTTP error: {e.response.status_code} {e.response.text[:200]}")
                if e.response.status_code in (400, 403, 404, 429):
                    self._next_model()
                    continue
                break
            except Exception as e:
                last_error = str(e)
                logger.error(f"AI analyze error: {e}")
                break

        return {
            'action': 'error',
            'response': f'⚠️ ИИ перегружен ({last_error}). Попробуй через минуту.',
        }

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

Правила:
- Даты в формате YYYY-MM-DD
- Если год не указан — текущий (или следующий, если дата прошла)
- Время в формате HH:MM (24ч)
- Эффективность — ТОЛЬКО если явно указана в тексте (например, "эффективность 80"). Не выдумывай.
- Не выдумывай данные — если не ясно, ставь null
- Только JSON, никаких пояснений

Примеры:
- "сегодня работал с 10 до 18" → {{"action":"fact","date":"{today.isoformat()}","start":"10:00","end":"18:00","efficiency":null,"response":"Записал факт"}}
- "сегодня работал с 10 до 18, эффективность 80" → {{"action":"fact","date":"{today.isoformat()}","start":"10:00","end":"18:00","efficiency":80,"response":"Записал факт"}}
- "завтра с 9 до 17" → {{"action":"plan","date":"{tomorrow.isoformat()}","start":"09:00","end":"17:00","response":"Записал план"}}
- "сделал отчёт и провёл встречу" → {{"action":"note","tasks":["отчёт","встреча"],"response":"Записал задачи"}}
- "хочу в отпуск с 5 по 10 ноября" → {{"action":"absence","absence_type":"vacation","date":"{today.year}-11-05","date_end":"{today.year}-11-10","response":"Заявка на отпуск"}}
- "сколько я наработал за месяц?" → {{"action":"query","query_type":"summary","response":"Смотрю статистику"}}
- "привет" → {{"action":"chat","response":"Привет! Чем помочь?"}}"""

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
    models_str = os.environ.get('AI_MODELS', os.environ.get('AI_MODEL', 'gpt-4o-mini')).strip()
    proxy = os.environ.get('AI_PROXY', '').strip() or None

    if not enabled or not api_key:
        return None

    models = [m.strip() for m in models_str.split(',') if m.strip()]
    return AIParser(api_key=api_key, base_url=base_url, models=models, proxy=proxy)