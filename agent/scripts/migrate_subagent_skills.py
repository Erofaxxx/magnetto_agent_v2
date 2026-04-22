"""
Миграция доменных skill-файлов внутрь папок специализированных субагентов.
direct_* → subagents/direct-optimizer/skills/
scoring_* → subagents/scoring-intelligence/skills/
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "skills"
SUBAGENT_SKILLS_DIR = ROOT / "clients" / "magnetto" / "subagents"

# slug → (subagent_name, description)
MAP: dict[str, tuple[str, str]] = {
    "direct_keywords_placements": (
        "direct-optimizer",
        "Анализ bad_keywords и bad_placements: интерпретация zone_status (red/yellow/green), "
        "goal_score, roas_deviation, bid_zone. Когда удалять ключ / исключать площадку РСЯ, "
        "как учитывать brand / seasonality / cpc_deviation / bounce_rate. SQL-шаблоны для "
        "выборки кандидатов на отключение.",
    ),
    "direct_queries": (
        "direct-optimizer",
        "Работа с bad_queries — поисковые запросы автотаргетинга. Трактовка is_chronic / "
        "is_recent, TargetingCategory, подбор минус-слов, матчинг matched_keyword ↔ keyword.",
    ),
    "direct_performance": (
        "direct-optimizer",
        "Отчёты по dm_direct_performance: расходы, клики, CPC, CPA, CPL, ROAS, leads_all, "
        "order_created, order_paid, SEARCH vs AD_NETWORK, воронка impressions→clicks→"
        "sessions→leads→CRM. Стандартный шаблон еженедельного отчёта по Директу.",
    ),
    "scoring_clients": (
        "scoring-intelligence",
        "Работа с dm_active_clients_scoring: priority hot/warm/cold, lift_score, next_step, "
        "recommended_goal_id, optimal_retarget_days. Кого ретаргетить, как интерпретировать "
        "score, исключение мусорных / тавтологических целей из рекомендаций.",
    ),
    "scoring_step_impact": (
        "scoring-intelligence",
        "Работа с dm_step_goal_impact: lift-анализ целей по шагам визитов. rate_with_goal "
        "vs rate_without_goal, какие действия на каком шаге увеличивают конверсию. "
        "Исключения: CRM-тавтологии (332069613/332069614), мусорные цели "
        "(402733217, 405315077/078, 407450615, 541504123).",
    ),
    "scoring_funnel_paths": (
        "scoring-intelligence",
        "Скорость воронки (dm_funnel_velocity, cohort_age_days >= 60 для зрелости) и "
        "паттерны путей (dm_path_templates, pattern Array). Медианные дни visit→lead→CRM→"
        "paid, какие цепочки каналов конвертят и за сколько, стоимость путей.",
    ),
}


def _slugify(name: str) -> str:
    return name.replace("_", "-")


def main() -> int:
    for src_name, (subagent, description) in MAP.items():
        src = SRC / f"{src_name}.md"
        if not src.exists():
            print(f"  skip {src_name}: missing")
            continue

        slug = _slugify(src_name)
        dst_dir = SUBAGENT_SKILLS_DIR / subagent / "skills" / slug
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst_file = dst_dir / "SKILL.md"

        body = src.read_text(encoding="utf-8").strip()

        frontmatter = "---\n"
        frontmatter += f"name: {slug}\n"
        frontmatter += "description: |\n  " + description.replace("\n", "\n  ") + "\n"
        frontmatter += "---\n\n"

        dst_file.write_text(frontmatter + body + "\n", encoding="utf-8")
        print(f"  {src_name:32s} -> {dst_dir.relative_to(ROOT)}")

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
