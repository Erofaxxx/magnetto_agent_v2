---
name: scoring-funnel-paths
description: |
  Скорость воронки (dm_funnel_velocity, cohort_age_days >= 60 для зрелости) и паттерны путей (dm_path_templates, pattern Array). Медианные дни visit→lead→CRM→paid, какие цепочки каналов конвертят и за сколько, стоимость путей.
---

# Скорость воронки и паттерны каналов

## Таблица magnetto.dm_funnel_velocity

Скорость воронки по недельным когортам: первый визит → лид → CRM → оплата. Все источники трафика (не только платный).

**Поля**: cohort_week (понедельник), cohort_age_days, new_clients, clients_with_lead, lead_rate_pct, avg_days_to_lead, median_days_to_lead, clients_with_crm, crm_rate_from_lead_pct, avg_days_lead_to_crm, clients_paid, paid_rate_from_crm_pct, snapshot_date.

### Воронка
```
new_clients → (lead_rate_pct%) → clients_with_lead → (crm_rate_from_lead_pct%) → clients_with_crm → (paid_rate_from_crm_pct%) → clients_paid
```

Цели: Лид=314553735, CRM создан=332069613, Оплата=332069614.

### Важно: фильтрация по возрасту когорты
Молодые когорты (<30 дней) не успели пройти воронку. Для честного анализа: `cohort_age_days >= 60` (средний цикл ~70 дней).

## Таблица magnetto.dm_path_templates

Дедуплицированные цепочки каналов клиентов: какие последовательности приводят к конверсии. 52 паттерна, 25 с конверсиями.

**Поля**: pattern (Array(String)), dedup_steps, ad_touches, total_clients, converters, cr_pct, avg_visits, avg_window_days, median_window_days, estimated_path_cost, cost_per_conversion (Nullable), snapshot_date.

### Дедупликация
Сырой: ad→ad→organic→organic→ad→organic. Дедупл: ['ad', 'organic', 'ad', 'organic'].

### Стоимость пути
```
estimated_path_cost = ad_touches × avg_cpc (из всего бюджета Директа)
cost_per_conversion = estimated_path_cost × total_clients / converters
```
Грубая оценка, достаточная для сравнения паттернов.

### Ключевой вывод: organic критически важен

| Паттерн | Клиентов | CR% | Стоимость/конв |
|---------|----------|-----|----------------|
| ['ad'] | 580 117 | 0.014% | ~23 080 руб |
| ['organic'] | 21 129 | 1.614% | 0 |
| ['organic', 'ad', 'organic'] | 261 | 4.981% | 66 руб |
| ['ad', 'organic', 'ad', 'organic'] | - | 6.12% | 108 руб |

Реклама работает как подогрев в середине мульти-канального пути, а не как самостоятельный канал конверсии.

## SQL-шаблоны для воронки

### Общая скорость воронки
```sql
SELECT cohort_week, new_clients, lead_rate_pct,
       avg_days_to_lead, median_days_to_lead,
       crm_rate_from_lead_pct, avg_days_lead_to_crm
FROM magnetto.dm_funnel_velocity
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_funnel_velocity)
  AND cohort_age_days >= 60
ORDER BY cohort_week DESC
LIMIT 10
```

### Узкие места воронки
```sql
SELECT 'Визит → Лид' AS stage, round(avg(lead_rate_pct), 2) AS avg_rate_pct
FROM magnetto.dm_funnel_velocity
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_funnel_velocity) AND cohort_age_days >= 60
UNION ALL
SELECT 'Лид → CRM', round(avg(crm_rate_from_lead_pct), 2)
FROM magnetto.dm_funnel_velocity
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_funnel_velocity) AND cohort_age_days >= 60
UNION ALL
SELECT 'CRM → Оплата', round(avg(paid_rate_from_crm_pct), 2)
FROM magnetto.dm_funnel_velocity
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_funnel_velocity) AND cohort_age_days >= 60
```

### Ранние сигналы свежих когорт
```sql
SELECT cohort_week, cohort_age_days, new_clients, clients_with_lead, lead_rate_pct
FROM magnetto.dm_funnel_velocity
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_funnel_velocity)
  AND cohort_age_days BETWEEN 7 AND 30
ORDER BY cohort_week DESC
```

## SQL-шаблоны для паттернов каналов

### Какие пути конвертируют
```sql
SELECT pattern, total_clients, converters, cr_pct,
       round(median_window_days, 0) AS median_days
FROM magnetto.dm_path_templates
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_path_templates)
  AND converters > 0
ORDER BY cr_pct DESC
```

### Стоимость конверсии по путям
```sql
SELECT pattern, ad_touches, total_clients, converters, cr_pct,
       round(estimated_path_cost, 0) AS path_cost,
       round(cost_per_conversion, 0) AS cost_per_conv
FROM magnetto.dm_path_templates
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_path_templates)
  AND ad_touches > 0 AND converters > 0
ORDER BY cost_per_conversion ASC
```

### Роль рекламы — самостоятельная или поддерживающая
```sql
SELECT if(ad_touches = 0, 'без рекламы', 'с рекламой') AS ad_type,
       sum(total_clients) AS clients, sum(converters) AS converts,
       round(sum(converters) / sum(total_clients) * 100, 3) AS cr_pct
FROM magnetto.dm_path_templates
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_path_templates)
GROUP BY ad_type
```

### Убыточные паттерны (реклама без конверсий)
```sql
SELECT pattern, ad_touches, total_clients, converters,
       round(estimated_path_cost, 0) AS path_cost
FROM magnetto.dm_path_templates
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_path_templates)
  AND ad_touches > 0 AND converters = 0
ORDER BY total_clients DESC
```
