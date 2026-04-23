# Skills Integration Plan — ClickHouse Analytics Agent

> Цель: динамическая подгрузка инструкций по сценарию. Системный промпт остаётся коротким.
> Агент может активировать несколько скиллов одновременно.

---

## Что меняется, что нет

**Меняются:**
- `agent.py` — router node, динамический system prompt, StateGraph вместо create_react_agent
- `config.py` — добавить `ROUTER_MODEL`

**Добавляются:**
- `skills/_registry.py` — реестр скиллов
- `skills/*.md` — файлы инструкций
- `router.py` — классификатор запроса

**Не трогать:**
- `tools.py`, `clickhouse_client.py`, `python_sandbox.py`, `api_server.py`, `chat_logger.py`

---

## Шаг 1 — config.py

Добавить одну переменную:

```python
# Модель для роутера — дешёвая/быстрая. Fallback на основную модель.
ROUTER_MODEL: str = os.environ.get(
    "ROUTER_MODEL",
    "anthropic/claude-haiku-3-5"  # или "deepseek/deepseek-chat"
)
```

---

## Шаг 2 — skills/_registry.py

Единственное место для регистрации скиллов. При добавлении нового — только сюда.

```python
"""
Реестр скиллов агента.

Каждая запись:
  router_hint  — ~50 токенов для роутера (не менять стиль: короткие ключевые слова)
  full_path    — путь к .md файлу с полными инструкциями (относительно BASE_DIR)
"""

from pathlib import Path
from config import BASE_DIR

SKILLS: dict[str, dict] = {
    "campaign_analysis": {
        "router_hint": (
            "Эффективность кампаний, ROAS, CPC, CPM, CTR, CPA, сравнение каналов, "
            "расходы, показы, ставки, utm-метки, dm_campaigns, dm_traffic_performance"
        ),
        "full_path": BASE_DIR / "skills" / "campaign_analysis.md",
    },
    "cohort_analysis": {
        "router_hint": (
            "Когорты, удержание, retention, LTV, повторные покупки, "
            "поведение клиентов по времени, dm_client_journey, dm_client_profile"
        ),
        "full_path": BASE_DIR / "skills" / "cohort_analysis.md",
    },
    "anomaly_detection": {
        "router_hint": (
            "Аномалии, резкие изменения, выбросы, неожиданные скачки/падения метрик, "
            "почему упало, почему выросло, необычное поведение"
        ),
        "full_path": BASE_DIR / "skills" / "anomaly_detection.md",
    },
    "weekly_report": {
        "router_hint": (
            "Еженедельный отчёт, сводка за неделю, итоги периода, "
            "регулярный дашборд, отчёт для команды"
        ),
        "full_path": BASE_DIR / "skills" / "weekly_report.md",
    },
}


def load_skill_instructions(active_skills: list[str]) -> str:
    """
    Загрузить и объединить инструкции для активных скиллов.
    Возвращает пустую строку если список пустой.
    Каждый скилл отделён заголовком чтобы агент понимал границы.
    """
    if not active_skills:
        return ""

    blocks: list[str] = []
    for skill_name in active_skills:
        entry = SKILLS.get(skill_name)
        if not entry:
            continue
        path = Path(entry["full_path"])
        if not path.exists():
            print(f"⚠️  Skill file not found: {path}")
            continue
        content = path.read_text(encoding="utf-8").strip()
        blocks.append(f"## Активный сценарий: {skill_name}\n\n{content}")

    return "\n\n---\n\n".join(blocks)
```

---

## Шаг 3 — router.py

```python
"""
Классификатор запроса пользователя.
Определяет какие skills нужны — вызывает дешёвую LLM один раз перед агентом.
"""

import json
from langchain_openai import ChatOpenAI
from config import OPENROUTER_API_KEY, ROUTER_MODEL
from skills._registry import SKILLS

_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://server.asktab.ru",
    "X-Title": "ClickHouse Analytics Agent — Router",
}

# Ленивый синглтон — создаётся при первом вызове
_router_llm: ChatOpenAI | None = None


def _get_router_llm() -> ChatOpenAI:
    global _router_llm
    if _router_llm is None:
        _router_llm = ChatOpenAI(
            model=ROUTER_MODEL,
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            max_tokens=64,           # только JSON-массив
            temperature=0,           # детерминированность важнее креативности
            default_headers=_OPENROUTER_HEADERS,
        )
    return _router_llm


def _build_router_prompt() -> str:
    """Строим промпт роутера из реестра — автоматически подхватывает новые скиллы."""
    skill_lines = "\n".join(
        f"  {name}: {entry['router_hint']}"
        for name, entry in SKILLS.items()
    )
    return f"""Ты — классификатор запросов аналитического агента по рекламным данным.
Определи, какие сценарии (skills) нужны для ответа на вопрос пользователя.
Можно выбрать несколько. Если ни один не подходит — верни пустой список.

Доступные сценарии:
{skill_lines}

Отвечай ТОЛЬКО валидным JSON-массивом строк, без пояснений.
Примеры: ["campaign_analysis"] или ["campaign_analysis", "cohort_analysis"] или []"""


def classify(query: str) -> list[str]:
    """
    Классифицировать запрос пользователя.

    Args:
        query: последний вопрос пользователя

    Returns:
        Список имён активных скиллов. Пустой список если ни один не подошёл.
        При любой ошибке возвращает [] — агент работает без скиллов.
    """
    try:
        llm = _get_router_llm()
        prompt = _build_router_prompt()
        response = llm.invoke([
            {"role": "system", "content": prompt},
            {"role": "user", "content": query},
        ])
        raw = response.content.strip()

        # Убрать возможные markdown-обёртки ```json ... ```
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        skills = json.loads(raw)

        # Валидация: только известные скиллы
        valid = [s for s in skills if s in SKILLS]
        if valid:
            print(f"🎯 Router: {valid}")
        else:
            print("🎯 Router: no skill matched")
        return valid

    except Exception as exc:
        print(f"⚠️  Router error (non-fatal, continuing without skills): {exc}")
        return []
```

---

## Шаг 4 — agent.py

### 4.1 Импорты — добавить

```python
# Добавить к существующим импортам:
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

import router as skill_router
from skills._registry import load_skill_instructions
```

### 4.2 AgentState — новый класс

Добавить после импортов, до класса AnalyticsAgent:

```python
class AgentState(TypedDict):
    messages: list
    active_skills: list[str]      # заполняет router_node
    skill_instructions: str       # заполняет router_node, читает _build_messages
```

### 4.3 _SYSTEM_PROMPT_TEMPLATE — изменить

Переименовать в `_SYSTEM_PROMPT_CORE`. Убрать из него разделы которые переедут в .md скиллы.

В конец добавить placeholder:

```python
_SYSTEM_PROMPT_CORE = """...(существующий текст без специфичных сценариев)...

{skill_section}
"""
```

### 4.4 _build_system_prompt — сделать динамическим

```python
def _build_system_prompt(self, skill_instructions: str = "") -> str:
    """Собрать системный промпт: ядро + схема + активные скиллы."""
    skill_section = ""
    if skill_instructions:
        skill_section = f"\n\n{skill_instructions}"

    return _SYSTEM_PROMPT_CORE.format(
        schema_section=self.schema_section,   # сохранить как self.schema_section
        skill_section=skill_section,
    )
```

### 4.5 router_node — новый метод AnalyticsAgent

```python
def _router_node(self, state: AgentState) -> dict:
    """
    Классифицировать последний запрос, загрузить инструкции скиллов.
    Не меняет messages — только active_skills и skill_instructions.
    """
    messages = state.get("messages", [])
    last_human = next(
        (m for m in reversed(messages) if isinstance(m, HumanMessage)),
        None
    )
    if last_human is None:
        return {"active_skills": [], "skill_instructions": ""}

    query_text = last_human.content
    if isinstance(query_text, list):
        # content может быть списком блоков (Anthropic format)
        query_text = " ".join(
            b.get("text", "") for b in query_text
            if isinstance(b, dict) and b.get("type") == "text"
        )

    active = skill_router.classify(query_text)
    instructions = load_skill_instructions(active)

    return {
        "active_skills": active,
        "skill_instructions": instructions,
    }
```

### 4.6 agent_node — обновить _build_messages

В существующем `_build_messages` заменить строку где строится `system_msg`:

```python
# БЫЛО:
system_msg = SystemMessage(content=system_prompt)  # или с cache_control

# СТАЛО:
current_system_prompt = self._build_system_prompt(
    skill_instructions=state.get("skill_instructions", "")
)
if is_anthropic:
    system_msg = SystemMessage(content=[{
        "type": "text",
        "text": current_system_prompt,
        "cache_control": {"type": "ephemeral"},
    }])
else:
    system_msg = SystemMessage(content=current_system_prompt)
```

### 4.7 Граф — заменить create_react_agent

```python
# БЫЛО:
self.graph = create_react_agent(
    model=self.llm,
    tools=TOOLS,
    prompt=_build_messages,
    checkpointer=self.memory,
)

# СТАЛО:
tool_node = ToolNode(TOOLS)

def agent_node(state: AgentState) -> dict:
    messages = _build_messages(state)
    response = self.llm.bind_tools(TOOLS).invoke(messages)
    return {"messages": [response]}

def should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return END

graph = StateGraph(AgentState)
graph.add_node("router", self._router_node)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)

graph.set_entry_point("router")
graph.add_edge("router", "agent")
graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
graph.add_edge("tools", "agent")

self.graph = graph.compile(checkpointer=self.memory)
```

---

## Шаг 5 — Создать .md файлы скиллов

Создать папку `skills/` и файлы. Структура каждого файла:

```markdown
# Скилл: [Название]

## Когда применяется
[1–2 предложения]

## Шаги выполнения
1. ...
2. ...

## SQL-шаблоны
\```sql
-- шаблон 1
\```

## Специфичные предупреждения
- ...
```

**Что перенести из текущего системного промпта в скиллы:**
- Раздел про типы запросов (факт / анализ / drill-down) → общие правила, остаются в ядре
- Специфичные SQL-паттерны для конкретных витрин → в соответствующий скилл
- Правила про dm_campaign_funnel, воронки → `campaign_analysis.md`
- Правила про когорты, клиентский путь → `cohort_analysis.md`

---

## Шаг 6 — Тестирование

```python
# Быстрая проверка роутера без запуска агента:
from router import classify

print(classify("Какой ROAS у кампаний за прошлый месяц?"))
# → ['campaign_analysis']

print(classify("Сравни удержание когорт и расходы по кампаниям"))
# → ['cohort_analysis', 'campaign_analysis']

print(classify("Привет, как дела?"))
# → []
```

**Чеклист перед деплоем:**
- [ ] Роутер возвращает правильные скиллы на 5–7 типовых вопросах
- [ ] Multi-skill: вопрос с двумя сценариями активирует оба
- [ ] Fallback: ошибка роутера → агент работает без скиллов (не падает)
- [ ] SqliteSaver: новые поля state сериализуются без ошибок
- [ ] Prompt caching: `cache_control` проставляется корректно при активном скилле

---

## Добавление нового скилла (после внедрения)

Два действия — код агента не меняется:

1. Создать `skills/new_scenario.md`
2. Добавить в `skills/_registry.py`:

```python
"new_scenario": {
    "router_hint": "ключевые слова, по которым роутер распознает этот сценарий",
    "full_path": BASE_DIR / "skills" / "new_scenario.md",
},
```

---

## Ожидаемый эффект

| Метрика | До | После |
|---|---|---|
| Токены системного промпта (базово) | 6 000–8 000 | ~1 500 |
| Токены с 1 скиллом | — | ~2 500 |
| Токены с 2 скиллами | — | ~3 500 |
| Стоимость вызова роутера (haiku) | — | ~$0.0001 |
| Добавление нового сценария | правка agent.py | 2 файла, 0 кода |
