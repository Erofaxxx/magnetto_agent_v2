---
name: cohort-analysis
description: |
  когорты, когортный анализ, удержание клиентов, retention, LTV, пожизненная ценность, dm_client_journey, dm_client_profile, возврат клиентов, клиенты по периодам, первый лид, цикл сделки, конверсия воронки по когортам
---

## Skill: Когортный анализ

### Ключевые таблицы

- **dm_client_profile** — профиль клиента: первый визит, первый лид, CRM-статус (только clientID > 0)
- **dm_client_journey** — все визиты клиента с флагами конверсий (только clientID > 0)

Важно: `dm_traffic_performance` считает ВСЕ визиты включая анонимные (clientID = 0).
Разница с клиентскими таблицами = анонимные сессии. Это норма, не ошибка.

Нет таблиц dm_orders, dm_purchases, dm_products — это не ecommerce, а B2C недвижимость.
Вместо покупок — события воронки: **лид** (`has_lead`) → **CRM создан** (`has_crm_created`) → **CRM оплачен** (`has_crm_paid`).

---

### Когортирование по первому визиту

```sql
-- Когорты по месяцу первого визита + конверсия в лид/оплату:
SELECT
    toStartOfMonth(first_visit_date)  AS cohort_month,
    count()                           AS cohort_size,
    countIf(has_lead = 1)             AS leads,
    countIf(has_crm_created = 1)      AS crm_created,
    countIf(has_crm_paid = 1)         AS crm_paid,
    round(countIf(has_lead = 1) / count() * 100, 2)        AS lead_cr_pct,
    round(countIf(has_crm_paid = 1) / count() * 100, 2)    AS paid_cr_pct
FROM dm_client_profile
WHERE first_visit_date >= toStartOfMonth(today() - INTERVAL 12 MONTH)
GROUP BY cohort_month
ORDER BY cohort_month
```

---

### Когортирование по первому лиду

```sql
-- Когорты по месяцу первого лида:
SELECT
    toStartOfMonth(first_lead_date)   AS cohort_month,
    count()                           AS cohort_size,
    countIf(has_crm_created = 1)      AS crm_created,
    countIf(has_crm_paid = 1)         AS crm_paid,
    round(countIf(has_crm_paid = 1) / count() * 100, 2)    AS paid_cr_pct,
    round(avg(days_to_first_lead), 1) AS avg_days_to_lead
FROM dm_client_profile
WHERE has_lead = 1
  AND first_lead_date >= toStartOfMonth(today() - INTERVAL 12 MONTH)
GROUP BY cohort_month
ORDER BY cohort_month
```

---

### Retention — возврат клиентов по месяцам

```sql
-- Клиенты по когорте первого визита + активность в последующих месяцах:
WITH cohorts AS (
    SELECT
        client_id,
        toStartOfMonth(first_visit_date) AS cohort_month
    FROM dm_client_profile
    WHERE first_visit_date >= toStartOfMonth(today() - INTERVAL 12 MONTH)
),
activity AS (
    SELECT
        j.client_id,
        c.cohort_month,
        toStartOfMonth(j.date) AS activity_month,
        dateDiff('month', c.cohort_month, toStartOfMonth(j.date)) AS month_offset
    FROM dm_client_journey j
    JOIN cohorts c USING (client_id)
    WHERE j.date >= toStartOfMonth(today() - INTERVAL 12 MONTH)
)
SELECT
    cohort_month,
    month_offset,
    count(DISTINCT client_id) AS active_clients
FROM activity
GROUP BY cohort_month, month_offset
ORDER BY cohort_month, month_offset
LIMIT 5000
```

### Retention rate в Python

```python
# Retention = клиенты вернувшиеся в месяц T+N / размер когорты
pivot = df.pivot_table(
    index='cohort_month',
    columns='month_offset',
    values='active_clients',
    aggfunc='sum'
)
# Первый столбец (offset=0) = размер когорты
cohort_sizes = pivot[0]
retention = pivot.divide(cohort_sizes, axis=0) * 100

import pandas as pd
result = "## Retention по когортам (% от размера)\n\n"
result += retention.round(1).to_markdown()
```

---

### Цикл сделки — от первого визита до лида

```sql
-- Распределение дней от первого визита до первого лида:
SELECT
    days_to_first_lead,
    count() AS clients
FROM dm_client_profile
WHERE has_lead = 1
  AND days_to_first_lead >= 0
GROUP BY days_to_first_lead
ORDER BY days_to_first_lead
LIMIT 200
```

```python
import pandas as pd

df_filtered = df[df['days_to_first_lead'] >= 0]
result = f"""## Цикл сделки (первый визит → первый лид)

- Медиана: {df_filtered['days_to_first_lead'].median():.0f} дней
- Среднее: {df_filtered['days_to_first_lead'].mean():.1f} дней
- До 1 дня: {(df_filtered['days_to_first_lead'] <= 1).mean():.1%} клиентов
- До 7 дней: {(df_filtered['days_to_first_lead'] <= 7).mean():.1%} клиентов
- До 30 дней: {(df_filtered['days_to_first_lead'] <= 30).mean():.1%} клиентов
"""
```

---

### Воронка по когортам (лид → CRM создан → CRM оплачен)

```sql
SELECT
    toStartOfMonth(first_visit_date) AS cohort_month,
    count()                          AS total_clients,
    countIf(has_lead = 1)            AS leads,
    countIf(has_crm_created = 1)     AS crm_created,
    countIf(has_crm_paid = 1)        AS crm_paid
FROM dm_client_profile
WHERE first_visit_date >= toStartOfMonth(today() - INTERVAL 12 MONTH)
GROUP BY cohort_month
ORDER BY cohort_month
```

```python
df['lead_cr']    = df['leads'] / df['total_clients'] * 100
df['crm_cr']     = df['crm_created'] / df['leads'].replace(0, float('nan')) * 100
df['paid_cr']    = df['crm_paid'] / df['crm_created'].replace(0, float('nan')) * 100

result = "## Воронка по когортам\n\n"
result += df[['cohort_month','total_clients','leads','crm_created','crm_paid',
              'lead_cr','crm_cr','paid_cr']].round(1).to_markdown(index=False)
```

---

### Интерпретация

- Сравнивай когорты одинаковой **зрелости** (одинаковое число месяцев наблюдения) — молодые когорты неполные
- Низкий lead_cr при высоком трафике → проблема с качеством трафика или формами
- Высокий lead_cr, низкий crm_cr → проблема на этапе обработки лидов (отдел продаж)
- Долгий цикл сделки (days_to_first_lead > 30) — норма для недвижимости
- Retention M1 > 20% для недвижимости — высокий показатель (клиенты возвращаются изучать)
