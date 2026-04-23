---
name: direct-keywords-placements
description: |
  Анализ bad_keywords и bad_placements: интерпретация zone_status (red/yellow/green), goal_score, roas_deviation, bid_zone. Когда удалять ключ / исключать площадку РСЯ, как учитывать brand / seasonality / cpc_deviation / bounce_rate. SQL-шаблоны для выборки кандидатов на отключение.
---

# Анализ ключевых слов и площадок Директа

## Кабинеты Яндекс Директа

У клиента 4 кабинета. Обе витрины (`bad_keywords`, `bad_placements`) собраны `UNION ALL` из 4 клонов и содержат колонку **`cabinet_name` LowCardinality(String)**: `audit-magnetto-tab1..tab4` (см. `direct_performance` skill для маппинга на проекты). Все внутренние бенчмарки и медианы (`med_goal_score_rate`, `med_roas`, `bench_roas_campaign`, `bench_goal_score_rate`, `avg_cpc_campaign`) считаются **per-cabinet × CampaignId** — зоны кампаний из разных кабинетов не смешиваются.

Фильтруй по `cabinet_name` при вопросах о конкретном проекте; группируй по нему при сравнении кабинетов.

## Таблицы

### magnetto.bad_keywords — рейтинг ключевых фраз
Окно 60 дней, обновление ежедневно. Только `CriterionType = 'KEYWORD'`.

**Ключевые поля**: **cabinet_name**, Criterion, MatchType, ad_network_type (SEARCH/AD_NETWORK), CampaignId, CampaignName, AdGroupId, AdGroupName, clicks, impressions, cost, ctr, cpc, avg_bid, cpc_to_bid_ratio, purchase_revenue, roas, goal_score, goal_score_rate, tier12_conversions, goal_rate_deviation, roas_deviation, med_goal_score_rate, med_roas, bid_zone, zone_status.

### magnetto.bad_placements — рейтинг площадок РСЯ
Окно 60 дней. Только `AdNetworkType = 'AD_NETWORK'`. Окно `max(Date) - 60` считается **per-cabinet** — у каждого кабинета своя последняя дата.

**Ключевые поля**: **cabinet_name**, Placement, CampaignId, CampaignName, cost, clicks, impressions, cpc, purchase_revenue, roas, goal_score, goal_score_rate, bounces, bounce_rate, is_recent, cpc_deviation, goal_rate_deviation, roas_deviation, avg_cpc_campaign, bench_roas_campaign, bench_goal_score_rate, zone_status, zone_reason.

## goal_score — взвешенный балл конверсий

| Уровень | Вес | Цели |
|---------|-----|------|
| Макро (tier 1) | ×10 | Все лиды, уникальный/целевой звонок, CRM создан/оплачен |
| Микро (tier 2) | ×3 | Отправка формы, клик по телефону |
| Слабые (tier 3) | ×1 | Скачать презентацию |

`goal_score_rate = (goal_score / clicks) × 100` — эффективность на клик.
`tier12_conversions` — строгий счётчик макро-конверсий без весов.

## bid_zone (только bad_keywords)

| Зона | cpc_to_bid_ratio | Смысл |
|------|-------------------|-------|
| A | < 0.4 | Дешёвые клики |
| B | 0.4–0.7 | Норма |
| C | 0.7–0.9 | Высокая конкуренция |
| D | > 0.9 | Перегретый аукцион |

## Отклонения от медианы кампании

- `goal_rate_deviation` — 0 = на уровне медианы, -0.5 = вдвое хуже, +0.3 = лучше на 30%
- `roas_deviation` — аналогично по ROAS
- Нет конверсий/выручки → принудительно -1.0

## zone_status (bad_keywords)

**pending**: cost < 300 И clicks < 20.

| bid_zone | green | yellow | red |
|----------|-------|--------|-----|
| D (>0.9) | goal_dev ≥ -0.2 И roas_dev ≥ -0.2 | одно в норме | оба хуже |
| C (0.7–0.9) | goal_dev ≥ -0.2 И roas_dev ≥ -0.3 | частично | оба плохие |
| B (норма) | goal_dev ≥ -0.2 | goal_dev ≥ -0.5 ИЛИ roas_dev ≥ -0.2 | оба хуже |
| A (<0.4) | goal_dev ≥ -0.3 | goal_dev ≥ -0.6 | tier12=0 И cost>500 |

## zone_status (bad_placements)

**pending**: is_recent=0 ИЛИ clicks<10 ИЛИ cost<200.
**red**: нет целей + cost>400 / CPC 3× выше среднего + нет целей / нет выручки + low GSR + cost>250.
**green**: ROAS 2-50 + CPC ok / ROAS>50 / GSR 3-5× выше бенчмарка + CPC ok / GSR>5× бенчмарка.
**yellow**: всё остальное.

`zone_reason` — машиночитаемый код: `r:no_goals+cost>400`, `g:roas_2-50+cpc_ok` и т.д.

## Когда zone_status пересмотреть

- Брендовые фразы в red → стратегически важны, не отключать
- Нишевые площадки недвижимости (cian.ru, avito.ru) → высокий CPC, но качественный трафик
- Площадки в yellow с bounce_rate > 70% → агент может понизить до red
- Сезонность → окно 60 дней может не захватить пик

## SQL-шаблоны

### Красные ключи с расходом
```sql
SELECT cabinet_name, Criterion, MatchType, ad_network_type, CampaignName, AdGroupName,
       clicks, cost, tier12_conversions, goal_score_rate, med_goal_score_rate,
       bid_zone, zone_status
FROM magnetto.bad_keywords
WHERE zone_status = 'red'
  -- AND cabinet_name = 'audit-magnetto-tab1'   -- раскомментируй для конкретного проекта
ORDER BY cost DESC
LIMIT 30
```

### Срез красных ключей по кабинетам (где самая большая утечка)
```sql
SELECT cabinet_name,
       count()                  AS red_keywords,
       round(sum(cost))         AS wasted_cost,
       sum(clicks)              AS wasted_clicks
FROM magnetto.bad_keywords
WHERE zone_status = 'red'
  AND report_date = (SELECT max(report_date) FROM magnetto.bad_keywords)
GROUP BY cabinet_name
ORDER BY wasted_cost DESC
```

### Зелёные ключи — повысить ставки
```sql
SELECT Criterion, ad_network_type, CampaignName, clicks, cost, tier12_conversions,
       goal_score_rate, round(goal_rate_deviation * 100, 0) AS deviation_pct, bid_zone
FROM magnetto.bad_keywords
WHERE zone_status = 'green' AND goal_rate_deviation > 0.3
ORDER BY tier12_conversions DESC, goal_score_rate DESC
```

### Красные площадки — исключить
```sql
SELECT Placement, CampaignName, cost, clicks, cpc, avg_cpc_campaign,
       goal_score, bounce_rate, zone_reason
FROM magnetto.bad_placements
WHERE zone_status = 'red'
ORDER BY cost DESC
LIMIT 30
```

### Зелёные площадки — масштабировать
```sql
SELECT Placement, CampaignName, cost, clicks, roas, goal_score_rate,
       bench_goal_score_rate, zone_reason
FROM magnetto.bad_placements
WHERE zone_status = 'green'
ORDER BY goal_score_rate DESC
```

### Площадки с высоким bounce_rate
```sql
SELECT Placement, CampaignName, clicks, bounce_rate, cost, goal_score, zone_status
FROM magnetto.bad_placements
WHERE bounce_rate > 70 AND is_recent = 1 AND clicks >= 10
ORDER BY bounce_rate DESC
```

### Ключи/площадки по кампании
```sql
-- Ключи (добавь cabinet_name, если кампании с тем же именем есть в нескольких кабинетах)
SELECT cabinet_name, Criterion, MatchType, ad_network_type, clicks, cost, tier12_conversions,
       goal_score_rate, bid_zone, zone_status
FROM magnetto.bad_keywords
WHERE CampaignName ILIKE '%<название>%'
ORDER BY cost DESC

-- Площадки
SELECT cabinet_name, Placement, cost, clicks, roas, goal_score_rate, bounce_rate, zone_status, zone_reason
FROM magnetto.bad_placements
WHERE CampaignName ILIKE '%<название>%'
ORDER BY cost DESC
```
