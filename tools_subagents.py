"""
Sub-agent tool wrappers for the main AnalyticsAgent.

Two tools that delegate specialised queries to sub-agents:
  - ask_direct_optimizer  → DirectOptimizerAgent
  - ask_scoring_agent     → ScoringIntelligenceAgent

Each tool returns (content, artifacts) via response_format="content_and_artifact"
so plots created by sub-agents are delivered to the user.
"""

import json

from langchain_core.tools import tool


@tool(response_format="content_and_artifact")
def ask_direct_optimizer(query: str) -> tuple[str, list[str]]:
    """
    Делегировать вопрос подагенту оптимизации Яндекс Директа.

    Подагент работает с таблицами:
    • bad_keywords — ежедневный рейтинг ключевых фраз (zone_status, bid_zone, goal_score)
    • bad_placements — рейтинг площадок РСЯ (zone_status, bounce_rate, CPC-отклонения)
    • bad_queries — рейтинг поисковых запросов (zone_status, is_chronic, автотаргетинг)
    • dm_direct_performance — статистика Директа по кампаниям (расходы, клики, лиды, CRM, ROAS)

    Используй когда вопрос касается:
    - Какие ключевые слова неэффективны / тратят бюджет / zone_status red
    - Какие площадки РСЯ исключить / лучшие площадки
    - Плохие поисковые запросы / минус-слова / is_chronic
    - Отчёт по Директу: расходы, CPL, CPA по кампаниям
    - Сравнение SEARCH vs РСЯ
    - Воронка конверсий Директа (impressions→clicks→sessions→leads→CRM)

    НЕ используй для:
    - Данных Метрики (визиты, цели, трафик по каналам) — используй clickhouse_query напрямую
    - Атрибуции каналов (Markov, Shapley) — это существующий скилл attribution
    - Скоринга клиентов / ретаргетинга — это ask_scoring_agent

    Args:
        query: Полный вопрос пользователя. Передай как есть, без изменений.
    """
    from subagents.direct_optimizer import get_direct_optimizer

    agent = get_direct_optimizer()
    result = agent.run(query)

    if not result.get("success"):
        error_msg = result.get("error", "Unknown error in DirectOptimizerAgent")
        return json.dumps({"success": False, "error": error_msg}), []

    text = result.get("text_output", "")
    plots = result.get("plots", [])

    content = json.dumps({
        "success": True,
        "text_output": text,
        "tool_calls_count": len(result.get("tool_calls", [])),
    }, ensure_ascii=False)

    return content, plots


@tool(response_format="content_and_artifact")
def ask_scoring_agent(query: str) -> tuple[str, list[str]]:
    """
    Делегировать вопрос подагенту скоринга клиентов и аналитики путей.

    Подагент работает с таблицами:
    • dm_active_clients_scoring — скоринг активных клиентов (priority hot/warm/cold, lift_score, рекомендации)
    • dm_step_goal_impact — lift-анализ целей по шагам визитов (какие цели увеличивают конверсию)
    • dm_funnel_velocity — скорость воронки по когортам (visit→lead→CRM→paid, median_days)
    • dm_path_templates — паттерны каналов (какие цепочки каналов конвертят и за сколько)

    Используй когда вопрос касается:
    - Кого ретаргетить сегодня / горячие клиенты / priority hot/warm
    - Какие цели важнее на первом/втором визите / lift целей
    - Скорость воронки: сколько дней до лида/CRM по когортам
    - Какие пути каналов конвертят лучше всего / роль рекламы в пути
    - Работает ли квиз / чат / конкретный инструмент (через lift-анализ)
    - Список client_id для ретаргетинга по проекту
    - Динамика hot/warm/cold клиентов

    НЕ используй для:
    - Данных Яндекс Директа (кампании, ключи, расходы) — это ask_direct_optimizer
    - Общего анализа трафика (dm_traffic_performance) — используй clickhouse_query напрямую
    - Создания сегментов аудитории — это отдельный агент сегментации

    Args:
        query: Полный вопрос пользователя. Передай как есть, без изменений.
    """
    from subagents.scoring_intelligence import get_scoring_agent

    agent = get_scoring_agent()
    result = agent.run(query)

    if not result.get("success"):
        error_msg = result.get("error", "Unknown error in ScoringIntelligenceAgent")
        return json.dumps({"success": False, "error": error_msg}), []

    text = result.get("text_output", "")
    plots = result.get("plots", [])

    content = json.dumps({
        "success": True,
        "text_output": text,
        "tool_calls_count": len(result.get("tool_calls", [])),
    }, ensure_ascii=False)

    return content, plots
