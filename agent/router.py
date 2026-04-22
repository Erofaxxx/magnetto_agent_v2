"""
Query router — классифицирует запрос пользователя и возвращает список активных skills.

Использует дешёвую модель (Haiku) для быстрой классификации (~$0.0001 за вызов).
При любой ошибке возвращает [] — агент продолжает работу без skills (graceful fallback).

Пример:
    from router import classify
    active = classify("Какой ROAS у кампаний за прошлый месяц?")
    # → ["clickhouse_querying", "campaign_analysis"]
"""

import json
import re
from typing import Optional

from langchain_openai import ChatOpenAI

from config import OPENROUTER_API_KEY, ROUTER_MODEL
from skills._registry import SKILLS

# Greeting words that can safely be stripped from the start of a query
# before routing. Stripping prevents the LLM from classifying the whole
# message as a greeting when the real content follows after the first line.
_GREETING_RE = re.compile(
    r"^(привет|hello|hi|добрый\s+день|добрый\s+вечер|доброе\s+утро|"
    r"здравствуй(те)?|салют|hey|хай|ку)[!.,\s]*$",
    re.IGNORECASE,
)


def _strip_greeting_prefix(query: str) -> str:
    """
    Remove leading greeting-only lines/paragraphs before routing.

    Example:
        "Привет\\n\\n1. Схема таблицы X?" → "1. Схема таблицы X?"
        "Hello\\n\\nПокажи выручку"       → "Покажи выручку"
        "Привет, покажи данные"            → unchanged (greeting + content in one line)
    """
    lines = query.strip().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:          # skip empty lines at the top
            i += 1
        elif _GREETING_RE.match(line):  # pure greeting line — skip it
            i += 1
        else:
            break
    if i == 0:
        return query  # nothing stripped
    rest = "\n".join(lines[i:]).strip()
    return rest if rest else query  # never return empty string

# ─── Ленивый синглтон роутера ─────────────────────────────────────────────────
_router_llm: Optional[ChatOpenAI] = None
_router_llm_model: Optional[str] = None  # track which model is cached


def _get_router_llm() -> ChatOpenAI:
    """Создать (или пересоздать при смене модели) ChatOpenAI клиент для роутера."""
    global _router_llm, _router_llm_model
    if _router_llm is None or _router_llm_model != ROUTER_MODEL:
        _router_llm = ChatOpenAI(
            model=ROUTER_MODEL,
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            max_tokens=8000,
            temperature=0,
            default_headers={
                "HTTP-Referer": "https://server.asktab.ru",
                "X-Title": "ClickHouse Analytics Agent Router",
            },
        )
        _router_llm_model = ROUTER_MODEL
    return _router_llm


def _build_router_prompt() -> str:
    """
    Автосгенерировать системный промпт роутера из реестра skills.
    При добавлении нового скилла в _registry.py — промпт обновляется автоматически.
    """
    skill_list = "\n".join(
        f'- "{name}": {info["router_hint"]}'
        for name, info in SKILLS.items()
    )
    return f"""Ты — классификатор запросов аналитического агента. Твоя задача — определить, какие skills нужны для ответа на запрос пользователя.

Доступные skills и когда их активировать:
{skill_list}

Правила:
- Верни JSON-массив с именами нужных skills: ["skill1", "skill2"]
- Если запрос требует данных из ClickHouse → обязательно включи "clickhouse_querying"
- Если запрос требует вычислений, агрегации или сравнения чисел → включи "python_analysis"
- Если запрос явно просит график или визуализацию → включи "visualization"
- Если тема запроса совпадает с доменом конкретного skill (кампании, когорты, аномалии и т.д.) — добавь его
- Не добавляй "python_analysis" если запрос — простое "покажи данные" или "сколько X", без вычислений
- [] верни ТОЛЬКО если сообщение содержит ИСКЛЮЧИТЕЛЬНО приветствие или болтовню — ни одного вопроса о данных, таблицах, метриках или бизнесе
- Отвечай ТОЛЬКО валидным JSON-массивом, без пояснений, без markdown-обёртки

Примеры:
- "Сколько визитов за прошлый месяц?" → ["clickhouse_querying"]
- "Покажи схему таблицы dm_conversion_paths" → ["clickhouse_querying"]
- "Схема dm_conversion_paths и есть ли spend в dm_campaigns?" → ["clickhouse_querying", "campaign_analysis"]
- "1. Схема таблицы X?\n2. Есть ли колонка Y?\n3. Какие UTM?" → ["clickhouse_querying"]
- "Привет, какая выручка за вчера?" → ["clickhouse_querying"]
- "Добрый день! Покажи топ кампаний и построй график" → ["clickhouse_querying", "python_analysis", "visualization", "campaign_analysis"]
- "Какой ROAS у кампаний? Построй график" → ["clickhouse_querying", "python_analysis", "visualization", "campaign_analysis"]
- "Когорты клиентов за 2024 год" → ["clickhouse_querying", "python_analysis", "cohort_analysis"]
- "Посчитай средний чек и динамику по месяцам" → ["clickhouse_querying", "python_analysis"]
- "Привет" → []
- "Как дела?" → []
"""


def classify(query: str, context: list[dict] | None = None) -> list[str]:
    """
    Классифицировать запрос и вернуть список активных skills.

    Args:
        query:   Текст последнего сообщения пользователя.
        context: Предыдущие сообщения диалога в формате
                 [{"role": "user"|"assistant", "content": str}, …].
                 Передаются как история перед последним запросом, чтобы
                 роутер мог понять контекст ("да", "продолжи", "ещё раз" и т.п.).

    Returns:
        Список имён skills из SKILLS. При ошибке — пустой список.
    """
    if not query or not query.strip():
        return []

    try:
        llm = _get_router_llm()
        system_prompt = _build_router_prompt()

        # Strip leading greeting-only lines so the LLM sees the real content.
        # E.g. "Привет\n\n1. Схема таблицы?" → "1. Схема таблицы?"
        routing_query = _strip_greeting_prefix(query)

        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # Inject conversation history so the router understands context-dependent
        # replies like "да", "продолжи", "покажи ещё".
        #
        # Truncation rules (applied only here, main agent is unaffected):
        #   user messages      — passed through fully (no truncation)
        #   assistant messages — first 400 chars (topic) + last 5 non-empty lines
        #                        (where the agent's closing question usually sits)
        if context:
            for msg in context:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if not content:
                    continue
                if role == "assistant" and len(content) > 400:
                    head = content[:400]
                    tail_lines = [l for l in content.splitlines() if l.strip()][-5:]
                    tail = "\n".join(tail_lines)
                    content = head + "\n…\n" + tail
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": routing_query})

        response = llm.invoke(messages)

        raw = response.content if isinstance(response.content, str) else str(response.content)
        raw = raw.strip()

        # Снять возможную markdown-обёртку: ```json [...] ```
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if match:
            raw = match.group(1).strip()

        # Найти первый JSON-массив в ответе
        arr_match = re.search(r"\[.*?\]", raw, re.DOTALL)
        if arr_match:
            raw = arr_match.group(0)

        parsed = json.loads(raw)

        if not isinstance(parsed, list):
            return []

        # Оставить только известные skills
        valid = [s for s in parsed if isinstance(s, str) and s in SKILLS]

        if valid:
            print(f"🎯 Router activated skills: {valid}")
        else:
            print("🎯 Router: no skills needed")

        return valid

    except Exception as exc:
        # Graceful fallback — агент работает без skills
        print(f"⚠️  Router error (using no skills): {exc}")
        return []
