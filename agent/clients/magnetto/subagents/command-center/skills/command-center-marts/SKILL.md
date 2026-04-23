---
name: command-center-marts
description: |
  Структура и семантика витрин командного центра: command_center_campaigns, command_center_adgroups, command_center_ads, budget_reallocation. Что где лежит, какие grain-ключи, почему sum(adgroups.cost) ≤ sum(campaigns.cost), как читать priority_goal_ids/values и history_* массивы. Загрузи перед любым SELECT в command_center_*.
---

# Командный центр — структура витрин

Три связанные дневные snapshot-витрины в БД `magnetto` + витрина бюджетных рекомендаций. Обновляются ночным MV: campaigns (07:30 UTC), adgroups (07:45), ads (07:50). У всех `report_date = today()` после рефреша.

## magnetto.command_center_campaigns

Портфель кампаний на сегодняшнюю дату. **1 строка = (report_date × campaign_id)**. Источник JOIN: `dm_direct_performance` (7d + prev 7d) + `campaigns_settings` + `budget_reallocation`.

**Ключевые поля**:
- Идентификация: `campaign_id`, `campaign_name`, `campaign_type` (TEXT_CAMPAIGN / SMART_CAMPAIGN / DYNAMIC_TEXT_CAMPAIGN и т.д.), `cabinet_name` (`audit-magnetto-tab1..4`).
- Состояние: `meta_state`, `status`, `state`, `search_strategy`, `network_strategy`, `attribution_model`.
- Бюджет: `weekly_budget` (рекомендованный от budget_reallocation).
- Таксономия: `traffic_mix` (search/network/mixed/none), `semantic_tags Array(String)` — brand/competitor/retarget/dynamic/media/smart. Используй `hasAny(semantic_tags, [...])`.
- Метрики `*_week` / `*_prev` (7d): `cost`, `revenue`, `impressions`, `clicks`, `leads`, `calls` (=unique_calls), `forms`, `orders`, `spam_traffic`, `targeted_calls`, `order_create_started`, `order_created`, `goal_507627231`, `unique_calls`, `quiz_completed`, `phone_clicks`.
- Производные: `roas_week`, `cpa_week`, `cpc_week`, `ctr_week`.
- Настройки: `priority_goal_ids Array(Int64)`, `priority_goal_values Array(Float64)` — параллельные массивы: `priority_goal_ids[i]` ↔ `priority_goal_values[i]` (id цели в Метрике → ценность в ₽).
- История: `history_weeks Array(Date)` + `history_cost / history_revenue / history_clicks / history_leads / history_calls / history_forms / history_orders: Array(...)` за 12 недель.
- Цветовая маркировка: `health` LowCardinality (green/yellow/red/pending), `health_reason String`.

## magnetto.command_center_adgroups

Группы внутри кампаний. **1 строка = (report_date × group_id)**.

Дополнительно к «кампаниям»: `group_name`, `serving_status` (ELIGIBLE/REJECTED/...), `group_type` (BASE/DYNAMIC/...), `keyword_count`, `autotargeting_state`, `autotargeting_risky` (0/1). История: только `history_cost/clicks/leads` (reduced).

⚠ **Фильтр на уровне источника**: только `status='ACCEPTED' AND serving_status='ELIGIBLE'`. Поэтому `sum(cost_week)` по adgroups кампании ≤ `cost_week` этой кампании из campaigns-mart. Разница = неактивные группы. Норма.

## magnetto.command_center_ads

Объявления внутри групп. **1 строка = (report_date × ad_id)**. Источник: все 4 кабинета tab1..tab4.

Поля креатива и модерации: `ad_type`, `ad_subtype`, `status`, `state`, `status_clarification`, `title`, `title2`, `text_body`, `final_url`, `has_image` (0/1), `vcard_moderation`, `ad_image_moderation`, `sitelinks_moderation`, `cabinet_name`.

Метрики `*_week` / `*_prev`: `cost`, `clicks`, `sessions`, `bounces`, `leads`, `spam_traffic`, `cpc`, `bounce_rate`. **Impressions и CTR намеренно убраны** — их на уровне объявления плохо агрегировать.

⚠ Фильтр `ad_id > 0` — smart/dynamic-кампании без привязки к конкретному объявлению исключены. `sum(ads.clicks) ≤ sum(campaigns.clicks)` — норма.

Health-эвристика для объявлений:
- `status='REJECTED'` → red
- `cost_week + cost_prev < 100₽` → pending (мало данных)
- `spam_traffic_week / clicks_week > 40%` → yellow
- остальное → green

## magnetto.budget_reallocation

Рекомендации по бюджету. **1 строка = (report_date × campaign_id × cabinet_name)**. Используется в `command_center_campaigns.weekly_budget` и в `/api/budget`.

Поля: `current_weekly_budget`, `recommended_weekly_budget`, `delta_rub`, `delta_pct`, `zone_status` (green/yellow/red), `rationale String` (объяснение), `expected_weekly_cost`, `expected_weekly_revenue`, `baseline_weekly_*`, `forecast_elasticity`, `forecast_conf_low`, `forecast_conf_high`, `delta_revenue_weekly`.

## Семантические нюансы — обязательно знать

- **`spam_traffic`** — это **только** цель 402733217 (мусорный трафик). Раньше была сумма трёх целей (402733217 + 405315077 + 407450615). Старые отчёты из чата могут ссылаться на «spam», имея в виду сумму — не путай с новым определением.
- **`ad_id`** в grain-ключе `dm_direct_performance` добавился с апрельских правок. В command_center_ads берётся именно оттуда.
- **`purchase_revenue`** пустое с 2025-11-17 (проблема в ETL Direct API). До этого было ~21 млн ₽ total. Поэтому `revenue_week = 0` в свежих данных — это **не баг**, это реальность. Не пытайся делить на него для ROAS.

## Базовые фильтры

```sql
-- Всегда: последний snapshot через CTE (обходит alias-конфликт CH по report_date)
WITH d AS (SELECT max(report_date) AS d FROM magnetto.command_center_campaigns)
SELECT ... FROM magnetto.command_center_campaigns WHERE report_date = (SELECT d FROM d)

-- Только активные кампании
AND status IN ('ACCEPTED', 'ACTIVE') AND state NOT IN ('SUSPENDED')

-- Только проблемные
AND health = 'red'

-- Конкретный кабинет (если юзер упомянул проект)
AND cabinet_name = 'audit-magnetto-tab2'

-- По семантическому тегу
AND hasAny(semantic_tags, ['brand'])
```

## Маппинг кабинетов на проекты

| cabinet_name          | Проект       |
|-----------------------|--------------|
| audit-magnetto-tab1   | Costura Town |
| audit-magnetto-tab2   | Нити         |
| audit-magnetto-tab3   | Риваят       |
| audit-magnetto-tab4   | Оригана      |
