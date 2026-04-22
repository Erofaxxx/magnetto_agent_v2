"""
`delegate_to_generalist(task, tables, skills)` — explicit delegation tool
для универсального помощника. Main agent передаёт:
  - task:   текст задачи
  - tables: имена таблиц (схема подтянется из SchemaCache)
  - skills: slugs скиллов (их body загрузится в system prompt)

Мы создаём одноразового generalist subagent с детерминистически
построенным system prompt:

    <base-instructions>         ← стабильное, в кэше всегда
    <schema of table A>          ← меняется по tables, сортируется
    <schema of table B>
    <SKILL X body>               ← меняется по skills, сортируется
    <SKILL Y body>

Subagent выполняется через LangGraph-подобную runnable (create_agent),
возвращает финальный текст + parquet_paths.

Для prompt caching: одинаковый (tables, skills) → идентичный byte-stream
system prompt → cache hit после первого вызова.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from .caching_middleware import CachingMiddleware
from .schema_cache import get_schema_cache
from .tools import clickhouse_query, python_analysis, think_tool


# ─── Base instructions (STABLE — always cached) ────────────────────────────

_GENERALIST_BASE = """Ты — универсальный аналитик-помощник главного агента Magnetto.

Главный агент делегирует тебе подзадачу и передаёт:
  - task: что нужно сделать
  - tables: какие таблицы использовать (их схемы ниже)
  - skills: какие доменные навыки применять (их инструкции ниже)

## Принципы работы

- Ты работаешь изолированно: промежуточные SQL и ошибки не видны главному.
- Возвращаешь только финальный ответ + ссылки на созданные parquet/plot файлы.
- SQL — только через `clickhouse_query`. Всегда с LIMIT, без SELECT *.
- Для транзакционных таблиц (`date`-колонка) фильтруй `WHERE date < today()`.
- Для snapshot-таблиц (`snapshot_date`/`report_date`) — `WHERE ... = (SELECT max(...))`.
- В знаменателях — `nullIf(x, 0)`, иначе деление на ноль.
- Числа в ответе — с разделителями тысяч. Язык — русский, Markdown.
- Эмодзи только ⚠ для предупреждений.

## Формат ответа главному агенту

- Короткий вывод с ключевыми цифрами жирным.
- Путь к parquet: "Данные сохранены в `/parquet/<hash>.parquet` (N строк)."
- Если строил график: "График: `/plots/<filename>.png`."
- Если данные неполные / устаревший snapshot — ⚠ первой строкой.

## Разрешённые таблицы для этой задачи

{schema_section}

## Активные навыки для этой задачи

{skills_section}

Если для ответа нужны таблицы ИЛИ знания не из списка выше — прямо скажи
главному: "Для ответа нужно <X>, это не входит в мою задачу". Не пиши SQL
против непереданных таблиц.
"""


# ─── Skill loader ─────────────────────────────────────────────────────────

def _resolve_skill_paths(client_dir: Path, skill_slugs: list[str]) -> list[Path]:
    """
    Resolve a list of skill slugs to actual SKILL.md paths.

    Search order:
      1. client_dir/skills/<slug>/SKILL.md
      2. client_dir/shared_skills/<slug>/SKILL.md
    Missing skills are silently skipped (with a log).
    """
    paths: list[Path] = []
    for slug in skill_slugs:
        p1 = client_dir / "skills" / slug / "SKILL.md"
        p2 = client_dir / "shared_skills" / slug / "SKILL.md"
        if p1.exists():
            paths.append(p1)
        elif p2.exists():
            paths.append(p2)
        else:
            print(f"⚠ delegate_to_generalist: skill '{slug}' not found")
    return paths


def _load_skill_bodies(paths: list[Path]) -> str:
    """
    Render skill bodies sorted by file path for deterministic caching.
    Frontmatter is stripped.
    """
    if not paths:
        return "(нет активных навыков)"
    # deterministic order
    paths = sorted(paths, key=lambda p: str(p))
    parts: list[str] = []
    for p in paths:
        text = p.read_text(encoding="utf-8")
        # strip frontmatter if present
        if text.startswith("---"):
            try:
                _, _fm, body = text.split("---", 2)
                text = body.strip()
            except ValueError:
                pass
        parts.append(f"### {p.parent.name}\n\n{text.strip()}")
    return "\n\n---\n\n".join(parts)


# ─── Factory ──────────────────────────────────────────────────────────────

def make_delegate_to_generalist_tool(
    *,
    client_dir: Path,
    default_model,
    tools_fn=None,
    middleware: Optional[list] = None,
):
    """
    Build a `delegate_to_generalist` tool closed over client_dir and model.

    Args:
        client_dir: path to clients/<id>/
        default_model: LangChain chat model instance
        tools_fn: callable returning list of tools (default: [clickhouse_query, python_analysis, think_tool])
        middleware: list of AgentMiddleware (default: [CachingMiddleware()])

    Returns:
        LangChain BaseTool.
    """
    middleware = middleware or [CachingMiddleware()]
    tools_fn = tools_fn or (lambda: [clickhouse_query, python_analysis, think_tool])

    @tool
    def delegate_to_generalist(
        task: str,
        tables: list[str],
        skills: list[str],
    ) -> str:
        """
        Делегировать задачу универсальному помощнику.

        ТРЕБОВАНИЯ К ВЫЗОВУ:
        - task:   полный текст подзадачи (вопрос пользователя + контекст).
        - tables: имена ClickHouse-таблиц, которые нужны помощнику (он
                  подгрузит их схемы). Имена бери из `/data_map.md`.
                  Минимум 1 таблица.
        - skills: slugs доменных навыков из `/skills/` и `/shared_skills/`.
                  **МИНИМУМ 2 skills**. Один из них ВСЕГДА — `clickhouse-basics`
                  (общие правила SQL для ClickHouse). Второй и последующие —
                  доменные, матчи по description в твоём system prompt.

        ПОЧЕМУ 2+ скиллов: один clickhouse-basics даёт правила SQL (даты,
        nullIf, LIMIT), а доменный skill (attribution, cohort-analysis, и т.д.)
        даёт методологию и интерпретацию. Без доменного subagent напишет
        технически правильный SQL, но интерпретация будет слабая.

        Помощник работает изолированно — главный агент получает только
        финальный ответ, без промежуточных SQL. Подходит для задач не
        покрытых специализированными субагентами (direct-optimizer,
        scoring-intelligence).

        Примеры вызова:
          task="Покажи ROAS топ-10 кампаний за последний месяц"
          tables=["dm_direct_performance", "campaigns_settings"]
          skills=["clickhouse-basics", "campaign-analysis", "python-analysis"]

          task="Посчитай когорты клиентов по first_traffic_source, retention"
          tables=["dm_client_profile"]
          skills=["clickhouse-basics", "cohort-analysis"]

          task="Markov-атрибуция для клиентов с has_crm_paid=1"
          tables=["dm_conversion_paths"]
          skills=["clickhouse-basics", "attribution", "python-analysis"]

        Args:
            task:    Полный текст подзадачи.
            tables:  Список имён таблиц ClickHouse (≥1).
            skills:  Список slug'ов скиллов kebab-case (≥2, обязательно
                     включая `clickhouse-basics`).
        """
        # ── Validate inputs ─────────────────────────────────────────────
        if not tables:
            return json.dumps({
                "success": False,
                "error": "tables пустой — укажи хотя бы одну таблицу из /data_map.md",
            }, ensure_ascii=False)
        if len(skills) < 2:
            return json.dumps({
                "success": False,
                "error": (
                    f"Нужно минимум 2 skills, передано {len(skills)}. "
                    "Обязательно: 'clickhouse-basics' + доменный(е) skill. "
                    "Посмотри список в `/skills/` и `/shared_skills/` — добавь релевантный."
                ),
            }, ensure_ascii=False)
        if "clickhouse-basics" not in skills:
            return json.dumps({
                "success": False,
                "error": (
                    "В skills обязательно должен быть 'clickhouse-basics' (правила SQL). "
                    f"Сейчас передано: {skills}. Добавь его и повтори."
                ),
            }, ensure_ascii=False)

        # ── Render deterministic system prompt ──────────────────────────
        schema = get_schema_cache().render_schema_section(tables)
        skill_paths = _resolve_skill_paths(client_dir, skills)
        skills_body = _load_skill_bodies(skill_paths)

        system_prompt = _GENERALIST_BASE.format(
            schema_section=schema,
            skills_section=skills_body,
        )

        # ── Build and run a one-shot agent ──────────────────────────────
        try:
            agent = create_agent(
                model=default_model,
                tools=tools_fn(),
                system_prompt=system_prompt,
                middleware=middleware,
            )
        except TypeError:
            # Fallback: older langchain signatures without middleware arg
            agent = create_agent(
                model=default_model,
                tools=tools_fn(),
                system_prompt=system_prompt,
            )

        try:
            result = agent.invoke({"messages": [HumanMessage(content=task)]})
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

        # Extract final answer
        messages = result.get("messages") if isinstance(result, dict) else None
        messages = messages or []
        final_text = ""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                content = msg.content
                if isinstance(content, str) and content.strip():
                    final_text = content
                    break
                if isinstance(content, list):
                    parts = [
                        b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                    ]
                    if parts:
                        final_text = "\n".join(parts)
                        break

        # Collect parquet paths mentioned in ToolMessages (clickhouse_query)
        parquet_paths: list[str] = []
        for msg in messages:
            if isinstance(msg, ToolMessage) and (getattr(msg, "name", "") or "") == "clickhouse_query":
                try:
                    data = json.loads(msg.content)
                    if data.get("parquet_path"):
                        parquet_paths.append(data["parquet_path"])
                except Exception:
                    pass

        return json.dumps(
            {
                "success": True,
                "text_output": final_text or "(пустой ответ subagent)",
                "parquet_paths": parquet_paths,
                "tool_calls_count": sum(1 for m in messages if isinstance(m, ToolMessage)),
            },
            ensure_ascii=False,
        )

    return delegate_to_generalist
