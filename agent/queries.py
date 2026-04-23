"""
Named ClickHouse queries for GET /api/tables/{query_name}.

Структура каждого запроса:
  description             — короткое описание для фронта
  sql                     — SELECT без ORDER BY и LIMIT (добавляются динамически)
  sortable_columns        — белый список колонок, по которым разрешена сортировка
  filterable_zone_status  — флаг: поддерживается ли фильтрация по zone_status
  filterable_cabinet      — флаг: поддерживается ли фильтрация по cabinet_name
                             (список доступных кабинетов бэкенд подтягивает
                              из ClickHouse на лету, см. api_server._get_available_cabinets)

Добавляй новые запросы сюда — endpoint подхватит их автоматически.
"""

QUERIES: dict[str, dict] = {
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
            FROM magnetto.bad_placements
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
            FROM magnetto.bad_keywords
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
            FROM magnetto.bad_queries
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
            FROM magnetto.report_daily_briefing
        """,
        "sortable_columns": ["client_id", "priority", "total_visits", "days_since_last_visit", "lift_score", "retarget_in_days", "action_conversion_lift", "report_date"],
        "filterable_zone_status": False,
    },
}
