---
name: anomaly-detection
description: |
  аномалии, аномальные значения, резкое изменение, выбросы, почему упало, почему выросло, неожиданный скачок, странные данные, необычное поведение, резкий рост, резкое падение, исследуй причину
---

## Skill: Обнаружение и расследование аномалий

### Алгоритм расследования

1. **Выгрузи исторические данные** — минимум 30–90 дней для baseline
2. **Рассчитай baseline** — mean + std за период до аномалии
3. **Флаги аномалий** — |z-score| > 2 или отклонение > 30% от среднего
4. **Сегментируй** — найди, в каком сегменте (канал, кампания, устройство, проект) концентрируется аномалия
5. **Сформулируй гипотезу** — аномалия в данных или в бизнесе?

### Ключевые метрики для мониторинга

| Метрика | Источник | Как считать |
|---------|----------|-------------|
| Визиты | `dm_traffic_performance` | `SUM(visits)` |
| Новые пользователи | `dm_traffic_performance` | `SUM(new_users)` |
| Отказы | `dm_traffic_performance` | `SUM(bounces) / SUM(visits)` |
| Лиды (any goal) | `dm_traffic_performance` | сумма goal-колонок лидов |
| Новые лиды (клиенты) | `dm_client_profile` | `countIf(has_lead = 1)` по `first_lead_date` |
| CRM-созданные | `dm_client_profile` | `countIf(has_crm_created = 1)` по `crm_created_date` |
| CRM-оплаченные | `dm_client_profile` | `countIf(has_crm_paid = 1)` по `crm_paid_date` |

Данных по рекламным расходам нет — CPC, CPM, ROAS рассчитать невозможно.

### Z-score в Python

```python
import numpy as np

# Рассчитай статистику baseline (исключи аномальный период):
baseline = df[df['date'] < anomaly_start]
mean_val = baseline['metric'].mean()
std_val = baseline['metric'].std()

# Флаги:
df['z_score'] = (df['metric'] - mean_val) / std_val
df['is_anomaly'] = df['z_score'].abs() > 2

result = df[df['is_anomaly']].to_markdown(index=False)
```

### Сравнение с аналогичным периодом

```sql
-- Текущая неделя vs та же неделя прошлого года:
SELECT
    toStartOfWeek(date) AS week,
    SUM(visits) AS visits_current,
    lagInFrame(SUM(visits), 52) OVER (ORDER BY toStartOfWeek(date)) AS visits_last_year,
    (SUM(visits) - lagInFrame(SUM(visits), 52) OVER (ORDER BY toStartOfWeek(date)))
    / lagInFrame(SUM(visits), 52) OVER (ORDER BY toStartOfWeek(date)) * 100 AS yoy_pct
FROM dm_traffic_performance
WHERE date >= today() - INTERVAL 1 YEAR
GROUP BY week
ORDER BY week
```

### Сегментация для локализации аномалии

```sql
-- Разбивка по каналу в аномальный день (трафик + конверсии в лиды):
SELECT
    traffic_source,
    utm_medium,
    SUM(visits)                                                 AS visits,
    SUM(goal_314553735)                                         AS leads_main_form,
    SUM(goal_314248561) + SUM(goal_201619840)
        + SUM(goal_201619843) + SUM(goal_201619846)             AS calls
FROM dm_traffic_performance
WHERE date = '2024-03-15'  -- аномальная дата
GROUP BY traffic_source, utm_medium
ORDER BY visits DESC
LIMIT 50

-- Разбивка по проекту (project_slug):
SELECT
    project_slug,
    SUM(visits)  AS visits,
    SUM(new_users) AS new_users
FROM dm_traffic_performance
WHERE date = '2024-03-15'
GROUP BY project_slug
ORDER BY visits DESC
```

### Аномалии в воронке лидов (dm_client_profile)

```sql
-- Динамика новых лидов по дням:
SELECT
    first_lead_date AS date,
    count()         AS new_leads
FROM dm_client_profile
WHERE has_lead = 1
  AND first_lead_date >= today() - INTERVAL 60 DAY
GROUP BY date
ORDER BY date
```

### Типичные причины аномалий

| Паттерн | Вероятная причина |
|---|---|
| Резкий рост трафика в один день | Акция, новостной инфоповод, вирусный контент |
| Резкое падение трафика | Технический сбой, блокировка, изменение UTM-разметки |
| Рост трафика без роста лидов | Нецелевой трафик, проблемы с формами на сайте |
| Падение лидов при стабильном трафике | Изменение форм, технический сбой лид-форм |
| Аномалия в одном канале | Изменение ставок/бюджетов, отключение кампании |
| Аномалия в одном проекте | Обновление страницы/лендинга, технический сбой |
| Рост CRM-оплат | Сезонность, успешная акция, накопленный pipeline |
| Постепенный тренд | Изменение алгоритма Яндекса, сезонность, конкуренция |

### Вывод аномалий

```python
# Таблица с флагами:
anomalies = df[df['is_anomaly']].copy()
anomalies['отклонение'] = anomalies['z_score'].apply(
    lambda z: f"⚠️ +{z:.1f}σ" if z > 0 else f"⚠️ {z:.1f}σ"
)
result = anomalies[['date', 'metric', 'отклонение']].to_markdown(index=False)
```

Аномалия — исследуй, не игнорируй. Резкий рост трафика без роста лидов — это сигнал, не успех.
