"""
Subagent YAML/Markdown loader — парсит `SUBAGENT.md` с frontmatter и
рендерит финальный deepagents SubAgent dict.

Формат SUBAGENT.md:
    ---
    name: direct-optimizer
    description: |
      Когда использовать...
    model: anthropic/claude-sonnet-4.6
    schema_tables:           # либо явный список, либо ["*"] — все таблицы
      - bad_keywords
      - bad_placements
    response_format: response_models.SubagentResult   # опционально (Python path)
    extra_skills_paths:                                # опционально
      - clients/magnetto/skills                        # дополнительные skill-директории
    ---

    Ты — аналитик ...

    ## Твои таблицы
    {schema_section}         ← placeholder, подставляется из SchemaCache

    ## Каталог всех таблиц
    {data_map_compact}       ← опционально: компактный каталог из data_map.md

Что делает loader:
  1. Читает все SUBAGENT.md в clients/<id>/subagents/*/
  2. Парсит YAML-frontmatter.
  3. Рендерит {schema_section} через SchemaCache.render_schema_section()
     (если schema_tables == ["*"] — даёт все таблицы).
  4. Рендерит {data_map_compact} из data_map.md (для generalist'а).
  5. Резолвит response_format в Python-класс.
  6. Собирает skills: subagents/<name>/skills/ + shared_skills/ +
     extra_skills_paths из frontmatter.
  7. Возвращает list[SubAgent] для create_deep_agent.

Cache stability: рендеринг детерминирован — таблицы сортируются по имени,
data_map_compact парсится в стабильном порядке. Никаких timestamps/random.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Callable, Union

import yaml

from .schema_cache import get_schema_cache, render_data_map_compact


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


def _resolve_response_format(spec_path: str):
    """
    Резолвит строку вида `response_models.SubagentResult` в Python-класс.

    Стратегия импорта (try в порядке):
      1. Относительный из текущего пакета (`__package__` этого файла; в
         runtime сервиса это будет 'core' — модули грузятся как core.x
         потому что cwd = agent/).
      2. Относительный из 'agent.core' (если кто-то запускает с верхним
         cwd).
      3. Bare top-level import.

    Если резолв падает — возвращаем None и предупреждаем (subagent
    поднимется без structured output).
    """
    try:
        if "." not in spec_path:
            print(f"⚠ subagent_loader: response_format '{spec_path}' must be 'module.Class'")
            return None
        mod_path, cls_name = spec_path.rsplit(".", 1)
        last_exc: Exception | None = None
        for pkg in (__package__, "agent.core", "core"):
            if not pkg:
                continue
            try:
                mod = importlib.import_module(f".{mod_path}", package=pkg)
                return getattr(mod, cls_name)
            except (ImportError, ValueError, ModuleNotFoundError, AttributeError) as exc:
                last_exc = exc
                continue
        # Last resort: bare module
        try:
            mod = importlib.import_module(mod_path)
            return getattr(mod, cls_name)
        except Exception as exc:
            last_exc = exc
        print(f"⚠ subagent_loader: failed to resolve response_format '{spec_path}': {last_exc}")
        return None
    except Exception as exc:
        print(f"⚠ subagent_loader: failed to resolve response_format '{spec_path}': {exc}")
        return None


def _expand_wildcard_tables(schema_tables: list[str], schema_cache) -> list[str]:
    """`["*"]` → все таблицы из SchemaCache. Иначе вернуть как есть."""
    if schema_tables == ["*"]:
        return schema_cache.all_table_names()
    return schema_tables


def load_subagents(
    client_dir: Path,
    *,
    default_model,
    tools: Union[list, Callable[[list[str]], list]],
) -> list[dict]:
    """
    Build a list of SubAgent dicts for create_deep_agent.

    Args:
        client_dir: e.g. /path/to/clients/magnetto
        default_model: LangChain model instance (used if SUBAGENT.md has no model)
        tools: либо общий список tools (legacy), либо фабрика
               `tools(schema_tables: list[str]) -> list[tool]`, которая строит
               персональный набор tools для каждого subagent'а (нужно для
               per-subagent scope, например sample_table со своими allowed_tables).

    Returns:
        List of dicts matching deepagents.SubAgent TypedDict.
    """
    subagents_dir = client_dir / "subagents"
    shared_skills_dir = client_dir / "shared_skills"
    data_map_path = client_dir / "data_map.md"

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
        schema_tables_raw = meta.get("schema_tables", []) or []

        # Wildcard expansion: ["*"] → all tables (для generalist'а).
        # После expand'а используем как обычный список — sample_table /
        # describe_table получают полный allowed_tables, а
        # render_schema_section знает что делать со списком.
        schema_tables = _expand_wildcard_tables(schema_tables_raw, schema_cache)

        # Sanity check: предупредить если какая-то таблица из schema_tables
        # отсутствует в SchemaCache (опечатка в SUBAGENT.md, таблицу удалили
        # из CH, или у User_magnetto нет GRANT SELECT). Это не ошибка — subagent
        # поднимется без этой таблицы в схеме, но SQL против неё упадёт.
        if schema_tables and schema_tables_raw != ["*"]:
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

        # Render compact data_map (для generalist'а — заменяет полный data_map)
        if "{data_map_compact}" in body:
            data_map_compact = render_data_map_compact(data_map_path)
        else:
            data_map_compact = ""

        # Substitute placeholders in body
        rendered_prompt = (
            body
            .replace("{schema_section}", schema_section)
            .replace("{data_map_compact}", data_map_compact)
        )

        # Pre-bake skill body into system_prompt if frontmatter declares
        # `inline_skill: <skill_dir_name>`. Use case: subagents whose behavior
        # is fully driven by ONE mandatory skill (e.g. placements-auditor →
        # placements_daily). Pre-baking puts the skill body into the cached
        # system prefix from turn 0 instead of the model `read_file`-ing it on
        # turn ~3 (which adds a ~22K token spike to the cache mid-flight).
        # Other subagents with multiple optional skills just don't set this.
        inline_name = meta.get("inline_skill")
        if inline_name:
            inline_path = sub_dir / "skills" / str(inline_name) / "SKILL.md"
            if inline_path.exists():
                _, inline_body = _parse_frontmatter(inline_path.read_text(encoding="utf-8"))
                if inline_body:
                    rendered_prompt = (
                        rendered_prompt
                        + f"\n\n## Inlined skill: {inline_name}\n\n"
                        + inline_body
                    )
            else:
                print(f"⚠ subagent_loader[{name}]: inline_skill '{inline_name}' "
                      f"не найден по пути {inline_path}")

        # Collect skills:
        # 1. subagents/<name>/skills/      — личные скиллы подагента
        # 2. shared_skills/                 — общие правила (clickhouse-basics и т.д.)
        # 3. extra_skills_paths из frontmatter — дополнительные пути
        #    (например generalist может включать main'овские analytical skills)
        skills_paths: list[str] = []
        local_skills = sub_dir / "skills"
        if local_skills.exists():
            skills_paths.append(str(local_skills))
        if shared_skills_dir.exists():
            skills_paths.append(str(shared_skills_dir))
        for extra_rel in (meta.get("extra_skills_paths") or []):
            extra_abs = (client_dir.parent / extra_rel).resolve() \
                if not Path(extra_rel).is_absolute() else Path(extra_rel)
            if extra_abs.exists():
                skills_paths.append(str(extra_abs))
            else:
                print(f"⚠ subagent_loader[{name}]: extra_skills_paths '{extra_rel}' "
                      f"не найден (resolved: {extra_abs})")

        # Персонализируем tool-список если передана фабрика
        sub_tools = tools(schema_tables) if callable(tools) else tools

        entry: dict[str, Any] = {
            "name": name,
            "description": description,
            "system_prompt": rendered_prompt,
            "tools": sub_tools,
        }
        if meta.get("model"):
            # Caller decides whether to instantiate model or pass as string — we pass through
            entry["model"] = meta["model"]
        else:
            entry["model"] = default_model
        if skills_paths:
            entry["skills"] = skills_paths

        # Optional structured output via Pydantic
        rf_spec = meta.get("response_format")
        if rf_spec:
            rf_cls = _resolve_response_format(rf_spec)
            if rf_cls is not None:
                entry["response_format"] = rf_cls

        result.append(entry)
        print(
            f"✅ Loaded subagent: {name} "
            f"(tables: {len(schema_tables)}, skills_paths: {len(skills_paths)}, "
            f"response_format: {bool(entry.get('response_format'))})"
        )

    return result
