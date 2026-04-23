---
name: command-center-drill
description: |
  Готовые SQL-паттерны для drill-down по портфелю: состояние «прямо сейчас», красные кампании, «почему у кампании X spam», кто ворует бюджет, расход vs weekly_budget, отклонённые объявления, развернуть priority_goal_ids/values через arrayJoin. Загрузи когда пользователь просит конкретный анализ — бери паттерн отсюда, не собирай с нуля.
---

# Drill-down паттерны в командном центре

Все анализы строятся сверху вниз: **portfolio summary → кампания → группа → объявление**. Если пользователь спрашивает «что случилось», твоя задача — пройти воронку и остановиться там, где сигнал чёткий.

## Паттерн 1. Что в портфеле прямо сейчас?

Один SELECT даёт полный снимок. Используй когда вопрос общего характера («как дела», «что плохо», «какие кампании в красной зоне»).

```sql
WITH d AS (SELECT max(report_date) AS d FROM magnetto.command_center_campaigns)
SELECT
    countIf(health = 'green')   AS green,
    countIf(health = 'yellow')  AS yellow,
    countIf(health = 'red')     AS red,
    countIf(health = 'pending') AS pending,
    sum(cost_week)    AS cost_w,
    sum(cost_prev)    AS cost_p,
    sum(clicks_week)  AS clicks_w,
    sum(leads_week)   AS leads_w,
    sum(spam_traffic_week) AS spam_w,
    sum(order_created_week) AS orders_w
FROM magnetto.command_center_campaigns
WHERE report_date = (SELECT d FROM d);
```

## Паттерн 2. Red-кампании с причинами

```sql
WITH d AS (SELECT max(report_date) AS d FROM magnetto.command_center_campaigns)
SELECT
    campaign_id, campaign_name, cabinet_name,
    cost_week, cost_prev,
    round((cost_week - cost_prev) / nullIf(cost_prev, 0) * 100, 1) AS cost_delta_pct,
    clicks_week, leads_week, spam_traffic_week,
    health_reason
FROM magnetto.command_center_campaigns
WHERE report_date = (SELECT d FROM d)
  AND health = 'red'
ORDER BY cost_week DESC;
```

## Паттерн 3. «Почему у кампании X spam 40%» — drill в группы+объявления

```sql
-- Шаг 1: группы кампании
WITH d AS (SELECT max(report_date) AS d FROM magnetto.command_center_adgroups)
SELECT
    group_id, group_name, serving_status, status,
    clicks_week, spam_traffic_week,
    round(spam_traffic_week / nullIf(clicks_week, 0) * 100, 1) AS spam_pct,
    health, health_reason
FROM magnetto.command_center_adgroups
WHERE report_date = (SELECT d FROM d)
  AND campaign_id = <CID>
ORDER BY spam_traffic_week DESC;

-- Шаг 2: объявления в подозрительной группе
WITH d AS (SELECT max(report_date) AS d FROM magnetto.command_center_ads)
SELECT
    ad_id, ad_type, status, title, text_body,
    clicks_week, spam_traffic_week,
    round(spam_traffic_week / nullIf(clicks_week, 0) * 100, 1) AS spam_pct,
    final_url, health_reason
FROM magnetto.command_center_ads
WHERE report_date = (SELECT d FROM d)
  AND adgroup_id = <GID>
ORDER BY spam_traffic_week DESC;
```

## Паттерн 4. Кто украл бюджет за неделю

```sql
WITH d AS (SELECT max(report_date) AS d FROM magnetto.command_center_campaigns)
SELECT
    campaign_name, cabinet_name,
    round(cost_week)      AS cost_w,
    round(cost_prev)      AS cost_p,
    round(cost_week - cost_prev)  AS delta_rub,
    round((cost_week - cost_prev) / nullIf(cost_prev, 0) * 100, 1) AS delta_pct,
    leads_week, leads_prev,
    health, health_reason
FROM magnetto.command_center_campaigns
WHERE report_date = (SELECT d FROM d)
  AND cost_week > cost_prev
ORDER BY (cost_week - cost_prev) DESC
LIMIT 10;
```

## Паттерн 5. Рекомендованный бюджет vs факт

```sql
WITH d AS (SELECT max(report_date) AS d FROM magnetto.command_center_campaigns)
SELECT
    campaign_name, cabinet_name,
    round(cost_week) AS actual_cost,
    round(weekly_budget) AS recommended_budget,
    round(cost_week - weekly_budget) AS over_budget,
    health, health_reason
FROM magnetto.command_center_campaigns
WHERE report_date = (SELECT d FROM d)
  AND weekly_budget > 0
  AND cost_week > weekly_budget * 1.15  -- перерасход >15%
ORDER BY over_budget DESC;
```

## Паттерн 6. Динамика по health-зонам

```sql
-- Что изменилось за неделю в целом по портфелю
WITH d AS (SELECT max(report_date) AS d FROM magnetto.command_center_campaigns)
SELECT
    health,
    count() AS campaigns_n,
    round(sum(cost_week))  AS cost_w,
    round(sum(cost_prev))  AS cost_p,
    sum(clicks_week)  AS clicks_w,
    sum(leads_week)   AS leads_w,
    sum(spam_traffic_week) AS spam_w
FROM magnetto.command_center_campaigns
WHERE report_date = (SELECT d FROM d)
GROUP BY health
ORDER BY cost_w DESC;
```

## Паттерн 7. Приоритетные цели кампании — факт vs настройка

Параллельные массивы `priority_goal_ids` и `priority_goal_values`. Разворачивай через `arrayZip`:

```sql
WITH d AS (SELECT max(report_date) AS d FROM magnetto.command_center_campaigns)
SELECT
    campaign_name,
    arrayJoin(arrayZip(priority_goal_ids, priority_goal_values)) AS goal,
    goal.1 AS goal_id,
    goal.2 AS goal_value_rub
FROM magnetto.command_center_campaigns
WHERE report_date = (SELECT d FROM d)
  AND length(priority_goal_ids) > 0
  AND campaign_id = <CID>;
```

## Паттерн 8. Отклонённые объявления (модерация)

```sql
WITH d AS (SELECT max(report_date) AS d FROM magnetto.command_center_ads)
SELECT
    campaign_id, adgroup_id, ad_id,
    ad_type, status, status_clarification,
    title, text_body,
    vcard_moderation, ad_image_moderation, sitelinks_moderation,
    cost_prev
FROM magnetto.command_center_ads
WHERE report_date = (SELECT d FROM d)
  AND status = 'REJECTED'
ORDER BY cost_prev DESC;
```

## Когда НЕ останавливаться в command_center и делегировать

- Вопрос про **конкретные ключевые слова**, минус-слова, bid_zone → скажи «нужен direct-optimizer» (это подагент на bad_keywords/bad_placements/bad_queries + campaigns_settings).
- Вопрос про **chronic queries**, автотаргетинг конкретных фраз → direct-optimizer.
- Вопрос про **роль кампании в клиентском пути** → scoring-intelligence.

## Когда останавливаться и дать короткий ответ

- Вопрос «сколько у меня сейчас в красной зоне» — один SELECT, ответ в 3 строки.
- Вопрос «как изменился ROAS портфеля» — summary-SELECT, ответ таблицей 2×2.
- «Какие кампании перерасходуют бюджет» — один SELECT по cost_week > weekly_budget.

Не раскручивай drill, если юзер не просил причин — просто дай числа.
