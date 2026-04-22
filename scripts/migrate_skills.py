"""
Миграция старых skill-файлов (skills/*.md) в формат deepagents v2:
clients/magnetto/skills/<slug>/SKILL.md с YAML-frontmatter.

Читает имя/description из клиентского _registry.py, а тело — из текущего .md.
Запускать из корня server_latest/.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_SKILLS = ROOT / "skills"
DST_SKILLS_CLIENT = ROOT / "clients" / "magnetto" / "skills"
DST_SHARED = ROOT / "clients" / "magnetto" / "shared_skills"

# Import SKILLS dict without loading its sibling imports
sys.path.insert(0, str(SRC_SKILLS))
# evaluate by reading _registry.py and extracting `SKILLS` manually
registry_text = (SRC_SKILLS / "_registry.py").read_text(encoding="utf-8")
# crude parse — we only need name, router_hint
skill_entries: dict[str, str] = {}
for match in re.finditer(
    r'"(?P<name>[a-z_]+)":\s*\{\s*"router_hint":\s*\((?P<hint>[\s\S]*?)\),',
    registry_text,
):
    name = match.group("name")
    hint_raw = match.group("hint").strip()
    # strip string concatenation "..." "..." → join
    hint = " ".join(
        s.strip() for s in re.findall(r'"([^"]+)"', hint_raw)
    )
    skill_entries[name] = hint

# Allocation — которые идут в shared_skills (общие для всех субагентов)
SHARED_SLUGS = {
    "clickhouse_querying": "clickhouse-basics",
    "python_analysis":     "python-analysis",
    "visualization":       "visualization",
}
# остальные → clients/magnetto/skills/<slug>/
# старое имя → новый kebab-case slug
SLUG_MAP = {
    "campaign_analysis":          "campaign-analysis",
    "cohort_analysis":            "cohort-analysis",
    "anomaly_detection":          "anomaly-detection",
    "weekly_report":              "weekly-report",
    "segmentation":               "segmentation",
    "goals_reference":            "goals-reference",
    "attribution":                "attribution",
    "direct_keywords_placements": "direct-keywords-placements",
    "direct_performance":         "direct-performance",
    "direct_queries":             "direct-queries",
    "scoring_clients":            "scoring-clients",
    "scoring_funnel_paths":       "scoring-funnel-paths",
    "scoring_step_impact":        "scoring-step-impact",
    "subagent_guide":             None,   # устаревший — старая инструкция по sub-agent-tools, в новой архитектуре не нужна
}


def migrate(name: str, hint: str) -> None:
    """Migrate a single skill file into kebab-case directory with frontmatter."""
    src = SRC_SKILLS / f"{name}.md"
    if not src.exists():
        print(f"  skip {name}: source missing")
        return

    if name in SHARED_SLUGS:
        new_slug = SHARED_SLUGS[name]
        dst_dir = DST_SHARED / new_slug
    elif name in SLUG_MAP:
        new_slug = SLUG_MAP[name]
        if new_slug is None:
            print(f"  deprecated {name}: not migrated")
            return
        dst_dir = DST_SKILLS_CLIENT / new_slug
    else:
        # default: kebab-case of underscore
        new_slug = name.replace("_", "-")
        dst_dir = DST_SKILLS_CLIENT / new_slug

    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_file = dst_dir / "SKILL.md"

    body = src.read_text(encoding="utf-8").strip()

    # frontmatter
    # description = первые 500 символов хинта + body summary если хинта мало
    description = hint.strip()
    if len(description) < 100:
        # fallback: читаем первую строку тела после заголовка
        body_lines = [l.strip() for l in body.split("\n") if l.strip()]
        first_para = next(
            (l for l in body_lines if not l.startswith("#")),
            "Специализированные инструкции по этому домену",
        )
        description = first_para[:500]

    # deepagents truncates description to 1024 chars
    description = description[:1000]

    frontmatter = "---\n"
    frontmatter += f"name: {new_slug}\n"
    # Multi-line description with block scalar for readability
    if "\n" in description or len(description) > 80:
        frontmatter += "description: |\n  " + description.replace("\n", "\n  ") + "\n"
    else:
        frontmatter += f"description: {description}\n"
    frontmatter += "---\n\n"

    dst_file.write_text(frontmatter + body + "\n", encoding="utf-8")
    print(f"  {name:32s} -> {dst_dir.relative_to(ROOT)}")


def main() -> int:
    DST_SKILLS_CLIENT.mkdir(parents=True, exist_ok=True)
    DST_SHARED.mkdir(parents=True, exist_ok=True)
    print(f"Migrating {len(skill_entries)} skills…")
    for name, hint in skill_entries.items():
        migrate(name, hint)
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
