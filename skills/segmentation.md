# Skill: Использование именованных сегментов (только чтение)

## Когда активируется

Запросы содержат: "сегмент", "аудитория", "ретаргет", "для сегмента", "покажи сегмент",
"лояльные покупатели", "тёплые лиды", "аудитория из сегмента", "использовать сегмент",
"segment", "audience"

## Что ты можешь делать

ТОЛЬКО использовать уже созданные сегменты для аналитики — читать их SQL-определение
и применять как CTE в запросах.

**Создание новых сегментов** — только в отдельном чате сегментации (`/api/segment/chat`).
Если пользователь просит создать сегмент — объясни, что это делается в отдельном чате.

## Как использовать сегмент в запросе

Когда пользователь ссылается на именованный сегмент (например "Тёплые лиды Direct"):

1. Уточни у пользователя SQL из поля `sql_query` нужного сегмента
   (пользователь может скопировать его из `/api/segments`)
2. Используй SQL сегмента как CTE:

```sql
-- Шаблон: сегмент как CTE в аналитическом запросе
WITH segment AS (
    -- вставь sql_query сегмента сюда
    SELECT DISTINCT client_id FROM dm_client_profile
    WHERE first_utm_source = 'ya-direct'
      AND total_visits >= 2
      AND has_purchased = 0
      AND days_since_last_visit <= 30
)
SELECT
    utm_source,
    count(DISTINCT cp.client_id)        AS clients,
    sum(cp.revenue)                     AS revenue,
    round(sum(cp.revenue) / count(*), 0) AS avg_revenue
FROM dm_conversion_paths cp
WHERE cp.client_id IN (SELECT client_id FROM segment)
GROUP BY utm_source
ORDER BY revenue DESC
```

## Ограничения

- НЕ создавай и не редактируй сегменты — только используй существующие
- Если сегмент не найден — скажи пользователю создать его в режиме сегментации
- Всегда применяй LIMIT к финальному запросу
