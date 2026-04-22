---
name: direct-performance
description: |
  Отчёты по dm_direct_performance: расходы, клики, CPC, CPA, CPL, ROAS, leads_all, order_created, order_paid, SEARCH vs AD_NETWORK, воронка impressions→clicks→sessions→leads→CRM. Стандартный шаблон еженедельного отчёта по Директу.
---

# Статистика Яндекс Директа

## Таблица magnetto.dm_direct_performance

Основная витрина статистики Директа. Гранулярность: 1 строка = день × **кабинет** × кампания × группа × тип сети. Данные с 01.11.2025, обновление ежедневно в 03:00.

**Поля**: date, **cabinet_name** (LowCardinality(String)), campaign_id, campaign_name, adgroup_id, adgroup_name, ad_network_type (SEARCH/AD_NETWORK), impressions, clicks, cost (руб, с НДС), sessions, bounces, purchase_revenue, purchase_profit, leads_all, unique_calls, targeted_calls, order_created, order_paid, form_submissions, phone_clicks, quiz_completed, spam_traffic.

### Кабинеты Яндекс Директа (с апреля 2026)

У клиента Magnetto **4 рекламных кабинета** — витрина агрегирует их через `UNION ALL` 4 клонов (`direct_custom_report_cab1..4`). Фильтруй/группируй по `cabinet_name`, если вопрос касается конкретного проекта.

| cabinet_name          | ClientLogin в Директе             | Основной project_slug |
|-----------------------|-----------------------------------|-----------------------|
| `audit-magnetto-tab1` | `ksi-costura-urban-magnetto`      | `costura-town`        |
| `audit-magnetto-tab2` | `ksi-niti-magnetto`               | `niti`                |
| `audit-magnetto-tab3` | `ksi-rivayat-kongrada-magnetto`   | `rivayat`             |
| `audit-magnetto-tab4` | `ksi-origana-grinvich-magnetto`   | `origana`             |

Правило: если вопрос про "расход", "клики", "кампании" без указания кабинета — агрегируй по всем; если упомянут проект / `costura` / `niti` / `rivayat` / `origana` — фильтруй `WHERE cabinet_name = '...'`.

Маппинг 1:1, поэтому для visit-based витрин (`dm_client_profile.last_project`, `dm_active_clients_scoring.last_project` и т.п.) кабинет получается inline без JOIN-а:

```sql
transform(last_project,
    ['costura-town', 'niti', 'rivayat', 'origana'],
    ['audit-magnetto-tab1', 'audit-magnetto-tab2', 'audit-magnetto-tab3', 'audit-magnetto-tab4'],
    'unmapped') AS cabinet_name
```

## Воронка конверсий

```
impressions → clicks → sessions → leads_all → order_created → order_paid
```

Цели:
- leads_all — цель 314553735 (Все лиды magnetto)
- unique_calls — цель 201619840
- targeted_calls — цель 201619843
- order_created — цель 332069613 (CRM: Заказ создан)
- order_paid — цель 332069614 (CRM: Заказ оплачен)
- form_submissions — цель 322914144
- phone_clicks — цель 314248561
- quiz_completed — цель 321286959

Сумма unique_calls + form_submissions ≠ leads_all — разная методология атрибуции.

## Метрики (вычисляются в запросе)

| Метрика | Формула |
|---------|---------|
| CTR | `clicks / nullIf(impressions, 0) * 100` |
| CPC | `cost / nullIf(clicks, 0)` |
| CPL | `cost / nullIf(leads_all, 0)` |
| CPA (CRM) | `cost / nullIf(order_created, 0)` |
| CPO (оплата) | `cost / nullIf(order_paid, 0)` |
| Bounce rate | `bounces / nullIf(sessions, 0) * 100` |
| CR лид | `leads_all / nullIf(sessions, 0) * 100` |
| Доля спама | `spam_traffic / nullIf(clicks, 0) * 100` |

Всегда `nullIf(..., 0)` в знаменателе!

## Поиск vs РСЯ

| | SEARCH | AD_NETWORK |
|--|--------|------------|
| CPC | ~166 руб | ~2.5 руб |
| CPL | ~19 305 руб | ~7 564 руб |
| Спам | 0.6% | 17.4% |

Поиск — дорогой, качественный, мало спама. РСЯ — дешёвый клик, ~17% спама.

## Нюансы

- `adgroup_name = '0'`, `adgroup_id = 0` — записи без разбивки по группам (нормально)
- `spam_traffic` — особенно актуально для РСЯ. Коррекция: `clicks - spam_traffic` = чистые клики
- `purchase_revenue/profit` — атрибуция Директа, не CRM. Для реальной выручки → order_created/paid
- Данные за сегодня неполные → фильтр `WHERE date < today()`
- `cabinet_name` — вторая ключевая ось после `date`; всегда выводи её в `GROUP BY`, когда сравниваешь кампании между кабинетами (имена кампаний в разных кабинетах могут пересекаться)

## SQL-шаблоны

### Итоги за период
```sql
SELECT sum(impressions) AS impressions, sum(clicks) AS clicks,
       round(sum(cost), 0) AS cost, sum(leads_all) AS leads,
       sum(order_created) AS crm_created, sum(order_paid) AS crm_paid,
       round(sum(cost) / nullIf(sum(clicks), 0), 2) AS cpc,
       round(sum(cost) / nullIf(sum(leads_all), 0), 0) AS cpl,
       round(sum(cost) / nullIf(sum(order_created), 0), 0) AS cpa_crm
FROM magnetto.dm_direct_performance
WHERE date >= today() - 30 AND date < today()
```

### Сравнение кампаний
```sql
SELECT campaign_name, ad_network_type,
       round(sum(cost), 0) AS cost, sum(leads_all) AS leads,
       sum(order_created) AS crm_created,
       round(sum(cost) / nullIf(sum(leads_all), 0), 0) AS cpl,
       round(sum(spam_traffic) / nullIf(sum(clicks), 0) * 100, 1) AS spam_pct
FROM magnetto.dm_direct_performance
WHERE date >= today() - 30 AND date < today()
GROUP BY campaign_id, campaign_name, ad_network_type
ORDER BY cost DESC
```

### Поиск vs РСЯ
```sql
SELECT ad_network_type,
       round(sum(cost), 0) AS cost, sum(clicks) AS clicks,
       sum(leads_all) AS leads, sum(order_created) AS crm_created,
       round(sum(cost) / nullIf(sum(clicks), 0), 2) AS cpc,
       round(sum(cost) / nullIf(sum(leads_all), 0), 0) AS cpl,
       sum(spam_traffic) AS spam,
       round(sum(spam_traffic) / nullIf(sum(clicks), 0) * 100, 1) AS spam_pct
FROM magnetto.dm_direct_performance
WHERE date >= today() - 30 AND date < today()
GROUP BY ad_network_type
```

### Помесячная динамика
```sql
SELECT toStartOfMonth(date) AS month,
       round(sum(cost), 0) AS cost, sum(leads_all) AS leads,
       sum(order_created) AS crm_created,
       round(sum(cost) / nullIf(sum(leads_all), 0), 0) AS cpl
FROM magnetto.dm_direct_performance
WHERE date < today()
GROUP BY month ORDER BY month
```

### Воронка по этапам
```sql
SELECT sum(impressions) AS impressions, sum(clicks) AS clicks,
       sum(sessions) AS sessions, sum(leads_all) AS leads,
       sum(order_created) AS crm_created, sum(order_paid) AS crm_paid,
       round(sum(clicks) / nullIf(sum(impressions), 0) * 100, 2) AS ctr_pct,
       round(sum(leads_all) / nullIf(sum(sessions), 0) * 100, 2) AS cr_lead_pct,
       round(sum(order_created) / nullIf(sum(leads_all), 0) * 100, 1) AS cr_crm_pct
FROM magnetto.dm_direct_performance
WHERE date >= today() - 30 AND date < today()
```

### Группы объявлений внутри кампании
```sql
SELECT adgroup_name,
       round(sum(cost), 0) AS cost, sum(clicks) AS clicks,
       sum(leads_all) AS leads,
       round(sum(cost) / nullIf(sum(leads_all), 0), 0) AS cpl
FROM magnetto.dm_direct_performance
WHERE date >= today() - 30 AND date < today()
  AND campaign_name ILIKE '%<название>%' AND adgroup_id != 0
GROUP BY adgroup_id, adgroup_name
ORDER BY cost DESC
```

### Сравнение кабинетов за период
```sql
SELECT cabinet_name,
       sum(clicks)                                       AS clicks,
       round(sum(cost))                                  AS cost,
       sum(leads_all)                                    AS leads,
       sum(order_created)                                AS crm_created,
       sum(order_paid)                                   AS crm_paid,
       round(sum(cost) / nullIf(sum(leads_all), 0))      AS cpl,
       round(sum(cost) / nullIf(sum(order_paid), 0))     AS cac_paid
FROM magnetto.dm_direct_performance
WHERE date BETWEEN today() - 30 AND today() - 1
GROUP BY cabinet_name
ORDER BY cost DESC
```

### Срез по конкретному кабинету
```sql
SELECT campaign_name, ad_network_type,
       round(sum(cost)) AS cost, sum(clicks) AS clicks, sum(leads_all) AS leads,
       round(sum(cost) / nullIf(sum(leads_all), 0)) AS cpl
FROM magnetto.dm_direct_performance
WHERE cabinet_name = 'audit-magnetto-tab3'    -- rivayat
  AND date >= today() - 30 AND date < today()
GROUP BY campaign_id, campaign_name, ad_network_type
ORDER BY cost DESC
```

### Сравнение двух периодов
```sql
SELECT if(date >= today() - 30, 'текущий', 'предыдущий') AS period,
       round(sum(cost), 0) AS cost, sum(leads_all) AS leads,
       round(sum(cost) / nullIf(sum(leads_all), 0), 0) AS cpl,
       sum(order_created) AS crm_created
FROM magnetto.dm_direct_performance
WHERE date >= today() - 60 AND date < today()
GROUP BY period ORDER BY period DESC
```
