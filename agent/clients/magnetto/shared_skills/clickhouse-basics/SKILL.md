---
name: clickhouse-basics
description: |
  SQL запрос к базе данных, выгрузить данные, написать SELECT, получить данные из ClickHouse, запрос к таблице, показать данные, сколько, топ, список, найди в базе
---

## Skill: Выгрузка данных из ClickHouse

### Шаблоны фильтрации по датам — использовать ТОЛЬКО этот синтаксис:

```sql
-- Прошлый месяц:
WHERE date >= toStartOfMonth(today() - INTERVAL 1 MONTH) AND date < toStartOfMonth(today())
-- Последние 30 дней:
WHERE date >= today() - INTERVAL 30 DAY
-- Текущий год:
WHERE toYear(date) = toYear(today())
-- Конкретный период:
WHERE date BETWEEN '2024-01-01' AND '2024-01-31'
```

Не используй CTE только для фильтрации по дате — это всегда решается в WHERE напрямую.

### Правила LIMIT

- Обычный запрос: LIMIT 1 000–10 000
- Большая выборка (временные ряды, full scan): до LIMIT 500 000
- Таблицы могут содержать 800 000+ строк — оценивай объём до запроса

### Агрегация

Агрегируй в SQL (SUM, COUNT, AVG, GROUP BY) — ClickHouse очень быстр на агрегациях.
Фильтруй в WHERE — не выгружай лишнего для Python.
Доступные функции: toStartOfMonth(), toYear(), toDayOfWeek(), arrayJoin() и др.

### CTE для нескольких таблиц — объединяй в ОДНОМ запросе:

```sql
WITH кампании AS (
    SELECT campaign_id, SUM(spend) AS spend
    FROM dm_campaigns
    WHERE date >= '2024-01-01'
    GROUP BY campaign_id
),
сессии AS (
    SELECT campaign_id, COUNT() AS visits, SUM(revenue) AS revenue
    FROM dm_traffic_performance
    GROUP BY campaign_id
)
SELECT к.campaign_id, к.spend, с.visits, с.revenue,
       с.revenue / к.spend AS roas
FROM кампании к LEFT JOIN сессии с USING (campaign_id)
```

Начинай с одной витрины. JOIN — только если без него принципиально не решить.
При JOIN двух витрин — одной строкой укажи по какому ключу соединяешь.

### Кэш и parquet_path

- Если ответ содержит `"cached": true` — данные из кэша, итерация не потрачена
- Сохраняй `parquet_path` из ответа clickhouse_query для передачи в python_analysis
- Если parquet_path уже есть из предыдущего запроса — передай напрямую, не повторяй тот же SQL

### list_tables

Схема таблиц уже в системном промпте — НЕ вызывай list_tables.
Используй list_tables только если схема кажется неполной или таблица не найдена.

### Выбор таблицы

- dm_traffic_performance — для недельной/дневной динамики визитов
- dm_campaign_funnel — НЕ содержит недельной динамики; использовать для воронки конверсии
- Если используешь другую таблицу вместо названной пользователем — первой строкой ответа объясни почему

### Комментарии к запросу

- При временной фильтрации — указывай в ответе по какому полю фильтруешь
- При JOIN — указывай ключ соединения и что он означает для интерпретации
