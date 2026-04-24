"""
Subagent YAML/Markdown loader — парсит `SUBAGENT.md` с frontmatter и
рендерит финальный deepagents SubAgent dict со встроенной схемой таблиц.

Формат SUBAGENT.md:
    ---
    name: direct-optimizer
    description: |
      Когда использовать...
    model: anthropic/claude-sonnet-4.6
    schema_tables:
      - bad_keywords
      - bad_placements
    ---

    Ты — аналитик ...

    ## Твои таблицы
    {schema_section}         ← placeholder, подставляется из SchemaCache

    ...

Что делает loader:
  1. Читает все SUBAGENT.md в clients/<id>/subagents/*/
  2. Парсит YAML-frontmatter.
  3. Рендерит {schema_section} через SchemaCache.render_schema_section().
  4. Собирает skills этого subagent'а (subagents/<name>/skills/) +
     общие из shared_skills/ — передаёт в SubAgent dict.
  5. Возвращает list[SubAgent] для create_deep_agent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schema_cache import get_schema_cache


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML frontmatter and return (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    try:
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm) or {}
        return (meta if isinstance(meta, dict) else {}), body.strip()
    except ValueError:
        return {}, text


def load_subagents(
    client_dir: Path,
    *,
    default_model,
    tools: list,
) -> list[dict]:
    """
    Build a list of SubAgent dicts for create_deep_agent.

    Args:
        client_dir: e.g. /path/to/clients/magnetto
        default_model: LangChain model instance (used if SUBAGENT.md has no model)
        tools: default tool list for all subagents

    Returns:
        List of dicts matching deepagents.SubAgent TypedDict.
    """
    subagents_dir = client_dir / "subagents"
    shared_skills_dir = client_dir / "shared_skills"

    if not subagents_dir.exists():
        return []

    schema_cache = get_schema_cache()
    result: list[dict] = []

    for sub_dir in sorted(subagents_dir.iterdir()):
        if not sub_dir.is_dir():
            continue
        sub_md = sub_dir / "SUBAGENT.md"
        if not sub_md.exists():
            print(f"⚠ subagent_loader: no SUBAGENT.md in {sub_dir}")
            continue

        meta, body = _parse_frontmatter(sub_md.read_text(encoding="utf-8"))
        name = meta.get("name") or sub_dir.name
        description = meta.get("description", "") or ""
        schema_tables = meta.get("schema_tables", []) or []

        # Sanity check: предупредить если какая-то таблица из schema_tables
        # отсутствует в SchemaCache (опечатка в SUBAGENT.md, таблицу удалили
        # из CH, или у User_magnetto нет GRANT SELECT). Это не ошибка — subagent
        # поднимется без этой таблицы в схеме, но SQL против неё упадёт.
        if schema_tables:
            known = set(schema_cache.all_table_names())
            missing = [t for t in schema_tables if t not in known]
            if missing:
                print(
                    f"⚠ subagent_loader[{name}]: schema_tables отсутствуют в SchemaCache: "
                    f"{missing}. Проверь опечатки в SUBAGENT.md, удалена ли таблица "
                    f"в CH, и что у CLICKHOUSE_USER есть GRANT SELECT."
                )

        # Render schema section
        if schema_tables:
            schema_section = schema_cache.render_schema_section(schema_tables)
        else:
            schema_section = "(нет таблиц для этого субагента)"

        # Substitute placeholder in body
        rendered_prompt = body.replace("{schema_section}", schema_section)

        # Collect skills — skills folder inside subagent + shared skills
        skills_paths: list[str] = []
        local_skills = sub_dir / "skills"
        if local_skills.exists():
            skills_paths.append(str(local_skills))
        if shared_skills_dir.exists():
            skills_paths.append(str(shared_skills_dir))

        entry: dict[str, Any] = {
            "name": name,
            "description": description,
            "system_prompt": rendered_prompt,
            "tools": tools,
        }
        if meta.get("model"):
            # Caller decides whether to instantiate model or pass as string — we pass through
            entry["model"] = meta["model"]
        else:
            entry["model"] = default_model
        if skills_paths:
            entry["skills"] = skills_paths

        result.append(entry)
        print(f"✅ Loaded subagent: {name} (tables: {len(schema_tables)}, skills_paths: {len(skills_paths)})")

    return result
