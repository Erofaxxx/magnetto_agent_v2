"""
Named ClickHouse queries for GET /api/tables/{query_name}.

Структура каждого запроса:
  description             — короткое описание для фронта
  sql                     — SELECT без ORDER BY и LIMIT (добавляются динамически).
                            `{db}.<table>` подставляется в момент импорта из
                            config.CLICKHOUSE_DATABASE — мультитенантный
                            деплой переключается через env, без правки кода.
  sortable_columns        — белый список колонок, по которым разрешена сортировка
  filterable_zone_status  — флаг: поддерживается ли фильтрация по zone_status
  filterable_cabinet      — флаг: поддерживается ли фильтрация по cabinet_name
                             (список доступных кабинетов бэкенд подтягивает
                              из ClickHouse на лету, см. api_server._get_available_cabinets)

Добавляй новые запросы сюда — endpoint подхватит их автоматически.
"""

from config import CLICKHOUSE_DATABASE as _CH_DB


_QUERIES_TEMPLATES: dict[str, dict] = {
    "bad_placements": {
        "description": "Плохие площадки",
        "sql": """
            SELECT
                `Placement`,
                `CampaignName`,
                cost,
                clicks,
                cpc,
                purchase_revenue,
                roas,
                goal_score_rate,
                bounce_rate,
                avg_cpc_campaign,
                bench_roas_campaign,
                bench_goal_score_rate,
                zone_status,
                zone_reason,
                cabinet_name
            FROM {db}.bad_placements
            WHERE (zone_status != 'pending' OR zone_status IS NULL)
        """,
        "sortable_columns": ["Placement", "CampaignName", "cpc", "cost", "clicks", "purchase_revenue", "roas", "goal_score_rate", "tier12_conversions", "med_cpc_campaign", "med_gsr_campaign", "med_roas_campaign", "zone_status", "cabinet_name"],
        "filterable_zone_status": True,
        "filterable_cabinet": True,
    },
    "bad_keywords": {
        "description": "Плохие ключевые запросы",
        "sql": """
            SELECT
                `Criterion`,
                `CampaignName`,
                ad_network_type,
                `AdGroupName`,
                cpc,
                goal_score_rate,
                avg_bid,
                cpc_to_bid_ratio,
                purchase_revenue,
                roas,
                med_roas,
                tier12_conversions,
                med_goal_score_rate,
                zone_status,
                cabinet_name
            FROM {db}.bad_keywords
            WHERE (zone_status != 'pending')
        """,
        "sortable_columns": ["Criterion", "CampaignName", "AdGroupName", "cpc", "goal_score_rate", "avg_bid", "cpc_to_bid_ratio", "purchase_revenue", "roas", "med_roas", "tier12_conversions", "med_goal_score_rate", "zone_status", "cabinet_name"],
        "filterable_zone_status": True,
        "filterable_cabinet": True,
    },
    "bad_queries": {
        "description": "Плохие поисковые запросы",
        "sql": """
            SELECT
                `Query`,
                `CriterionType`,
                `CampaignName`,
                `TargetingCategory`,
                `roas`,
                `goal_score_rate`,
                `cost`,
                `clicks`,
                `cpc`,
                `bounce_rate`,
                `zone_status`,
                `zone_reason`,
                cabinet_name
            FROM {db}.bad_queries
            WHERE (zone_status != 'pending')
        """,
        "sortable_columns": ["Query", "CriterionType", "CampaignName", "TargetingCategory", "roas", "goal_score_rate", "cost", "clicks", "cpc", "bounce_rate", "zone_status", "zone_reason", "cabinet_name"],
        "filterable_zone_status": True,
        "filterable_cabinet": True,
    },
    "daily_briefing": {
        "description": "Ежедневный брифинг по клиентам",
        "sql": """
            SELECT
                client_id,
                priority,
                total_visits,
                days_since_last_visit,
                first_traffic_source,
                lift_score,
                next_target_action,
                retarget_in_days,
                action_conversion_lift,
                analyst_comment,
                report_date
            FROM {db}.report_daily_briefing
        """,
        "sortable_columns": ["client_id", "priority", "total_visits", "days_since_last_visit", "lift_score", "retarget_in_days", "action_conversion_lift", "report_date"],
        "filterable_zone_status": False,
    },
}


def _resolve(template: dict) -> dict:
    """Substitute the {db} placeholder in `sql` with the current CH database."""
    out = dict(template)
    out["sql"] = out["sql"].format(db=_CH_DB)
    return out


QUERIES: dict[str, dict] = {name: _resolve(spec) for name, spec in _QUERIES_TEMPLATES.items()}
