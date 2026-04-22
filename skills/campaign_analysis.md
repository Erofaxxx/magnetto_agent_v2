# Скилл: Аналитика каналов и кампаний

Активируется при вопросах про: источники трафика, каналы, кампании, UTM, конверсию,
откуда приходят лиды, first touch, last touch, путь клиента до лида.

---

## Доступные данные

Расходы и клики Директа есть в `dm_direct_performance` (см. отдельный skill). Для вопросов "какой канал привёл клиента", "откуда пришли", "first/last touch" — используй визитные витрины ниже.

Таблиц `dm_orders`, `dm_purchases`, `dm_campaign_funnel` нет — это не ecommerce.
Конверсия = **лид** (`has_lead = 1`), глубокая конверсия = **CRM оплата** (`has_crm_paid = 1`).

Доступны: трафик, конверсия в лиды/CRM, пути клиентов.

## Кабинеты и visit-based витрины

У Magnetto 4 рекламных кабинета Яндекс Директа (`audit-magnetto-tab1..tab4`, по одному на проект: costura-town, niti, rivayat, origana), но **один общий счётчик Яндекс Метрики на все 4 проекта**. Поэтому:

- Direct-based витрины (`dm_direct_performance`, `bad_keywords`, `bad_placements`, `bad_queries`, `campaigns_settings`, `adgroups_settings`, `ads_settings`) **разделены по кабинетам** через колонку `cabinet_name`.
- Visit-based витрины (`dm_traffic_performance`, `dm_client_profile`, `dm_client_journey`, `dm_conversion_paths`, `dm_funnel_velocity`, `dm_step_goal_impact`, `dm_active_clients_scoring`, `dm_path_templates`) **не содержат cabinet_name** — на уровне визита нельзя сказать, с какого кабинета пришёл пользователь.

### Мост visit ↔ cabinet через project_slug

В `dm_client_profile.last_project` и `dm_client_journey.project_slug` лежит slug проекта (извлечён из URL `/our-projects/[slug]`). Маппинг slug → кабинет — статический 1:1, зашит в SQL через `transform()`:

```sql
-- Клиентская воронка с привязкой к кабинету Директа
SELECT
    transform(last_project,
        ['costura-town', 'niti', 'rivayat', 'origana'],
        ['audit-magnetto-tab1', 'audit-magnetto-tab2', 'audit-magnetto-tab3', 'audit-magnetto-tab4'],
        'unmapped')                AS cabinet_name,
    count()                        AS clients,
    countIf(has_lead = 1)          AS leads,
    countIf(has_crm_paid = 1)      AS paid
FROM dm_client_profile
WHERE first_visit_date >= today() - 90
GROUP BY cabinet_name
ORDER BY leads DESC
```

**Оговорки:**
- Маппинг достоверен для `last_project IN ('costura-town','niti','rivayat','origana')`.
- Для прочих slug-ов (`zk-1712`, `grinvich`, числовые `29/30/31`, второстепенные проекты) попадают в `unmapped`. Всегда упоминай долю `unmapped` в ответе.
- Визиты без URL `/our-projects/[slug]` не имеют проекта вообще — `last_project = ''`, тоже `unmapped`.

---

## Какую витрину использовать

| Задача | Витрина |
|--------|---------|
| Трафик по каналу: визиты, отказы, глубина, динамика по дням | `dm_traffic_performance` |
| Конверсия канала в лиды (по клиентам) | `dm_client_profile` (first_traffic_source / first_utm_*) |
| Конверсия канала по last touch | `dm_client_journey` (последний визит до лида) |
| Полный путь клиента до лида, мультитач | `dm_conversion_paths` |
| Расход / лиды / CRM по кабинетам Директа | `dm_direct_performance` (`cabinet_name`) |
| Сведение visits × cabinet | через `last_project` → `project_cabinet_map` |

---

## dm_traffic_performance — трафик и динамика

### Поля
| Поле | Описание |
|------|----------|
| `date` | Дата |
| `project_slug` | Проект (ЖК) |
| `utm_source` | Источник трафика |
| `utm_medium` | Тип трафика (cpc, organic, email...) |
| `utm_campaign` | Кампания |
| `traffic_source` | Тип источника (ad, organic, direct, referral, ...) |
| `search_engine` | Поисковая система |
| `device_category` | Устройство (desktop / mobile / tablet) |
| `region_city` | Город |
| `visits` | Визиты |
| `new_users` | Новые пользователи |
| `bounces` | Отказы |
| `total_duration_sec` | Суммарное время на сайте (секунды) |
| `total_page_views` | Суммарные просмотры страниц |
| `goal_*` | Счётчики целей (каждая цель — отдельная колонка) |

### Ключевые цели (goal-колонки)
| Группа | Колонки |
|--------|---------|
| Основная форма заявки | `goal_314553735` |
| Звонки (колтрекинг) | `goal_314248561`, `goal_201619840`, `goal_201619843`, `goal_201619846` |
| Формы / лиды | `goal_313904512`, `goal_314247265`, `goal_314247991`, `goal_338849075` |
| CRM (создан/оплачен) | `goal_332069613`, `goal_332069614`, `goal_405315077`, `goal_405315078` |
| Чат | `goal_349618756`, `goal_349618757`, `goal_349772279` |

### Метрики трафика
```sql
-- Трафик по каналу с качеством
SELECT
    traffic_source,
    utm_source,
    sum(visits)                                                  AS visits,
    sum(new_users)                                               AS new_users,
    round(sum(bounces) / sum(visits) * 100, 1)                  AS bounce_rate_pct,
    round(sum(total_duration_sec) / sum(visits) / 60, 1)        AS avg_duration_min,
    round(sum(total_page_views) / sum(visits), 1)               AS avg_pageviews
FROM dm_traffic_performance
WHERE date >= today() - INTERVAL 30 DAY
GROUP BY traffic_source, utm_source
ORDER BY visits DESC
LIMIT 50

-- Динамика визитов по дням
SELECT
    date,
    traffic_source,
    sum(visits) AS visits
FROM dm_traffic_performance
WHERE date >= today() - INTERVAL 30 DAY
GROUP BY date, traffic_source
ORDER BY date, visits DESC
```

### Конверсия в лиды из dm_traffic_performance (сессионная, ориентировочная)
```sql
-- Лиды по каналу (session-based через goal-колонки):
SELECT
    traffic_source,
    utm_source,
    sum(visits)                                                                AS visits,
    sum(goal_314553735 + goal_313904512 + goal_338849075)                      AS form_leads,
    sum(goal_314248561 + goal_201619840 + goal_201619843 + goal_201619846)     AS calls,
    round(sum(goal_314553735 + goal_313904512 + goal_338849075)
          / nullIf(sum(visits), 0) * 100, 2)                                   AS form_cr_pct
FROM dm_traffic_performance
WHERE date >= today() - INTERVAL 30 DAY
GROUP BY traffic_source, utm_source
ORDER BY visits DESC
LIMIT 50
```

---

## dm_client_profile — конверсия по каналу привлечения (first touch)

Для вопроса "откуда пришли клиенты, ставшие лидами" — использовать `dm_client_profile`,
а не `dm_traffic_performance` (там сессионная атрибуция).

```sql
-- Конверсия в лид по первому источнику (first touch):
SELECT
    first_traffic_source,
    first_utm_source,
    count()                                             AS total_clients,
    countIf(has_lead = 1)                               AS leads,
    countIf(has_crm_created = 1)                        AS crm_created,
    countIf(has_crm_paid = 1)                           AS crm_paid,
    round(countIf(has_lead = 1) / count() * 100, 2)    AS lead_cr_pct
FROM dm_client_profile
WHERE first_visit_date >= today() - INTERVAL 90 DAY
GROUP BY first_traffic_source, first_utm_source
ORDER BY total_clients DESC
LIMIT 50

-- Цикл сделки по каналу (дней от первого визита до лида):
SELECT
    first_traffic_source,
    first_utm_source,
    count()                                  AS leads,
    round(avg(days_to_first_lead), 1)        AS avg_days_to_lead,
    round(avg(total_visits), 1)              AS avg_visits_before_lead
FROM dm_client_profile
WHERE has_lead = 1
  AND days_to_first_lead >= 0
GROUP BY first_traffic_source, first_utm_source
HAVING leads >= 5
ORDER BY leads DESC
```

---

## dm_conversion_paths — полный путь клиента

### Поля
| Поле | Тип | Описание |
|------|-----|----------|
| `client_id` | UInt64 | ID клиента |
| `has_lead` | UInt8 | 1 = стал лидом |
| `has_crm_created` | UInt8 | 1 = создан в CRM |
| `has_crm_paid` | UInt8 | 1 = оплата в CRM |
| `path_length` | UInt16 | Количество касаний (визитов) в пути |
| `first_touch_date` | Date | Дата первого касания |
| `conversion_date` | Date | Дата конверсии (лида) |
| `conversion_window_days` | UInt16 | Дней от первого касания до лида |
| `channels_path` | Array(String) | Полный путь по каналам |
| `channels_dedup_path` | Array(String) | Путь без повторов подряд |
| `sources_path` | Array(String) | Путь по utm_source |
| `campaigns_path` | Array(String) | Путь по utm_campaign |
| `days_from_first_path` | Array(UInt16) | Дней от первого касания на каждом шаге |

### Типичные запросы

```sql
-- Среднее количество касаний до лида
SELECT
    round(avg(path_length))              AS avg_path_length,
    median(path_length)                  AS median_path_length,
    round(avg(conversion_window_days))   AS avg_days_to_lead
FROM dm_conversion_paths
WHERE has_lead = 1

-- Топ путей (дедублированных) по частоте среди конвертировавших в лид
SELECT
    channels_dedup_path                  AS path,
    count()                              AS clients
FROM dm_conversion_paths
WHERE has_lead = 1
GROUP BY path
ORDER BY clients DESC
LIMIT 20

-- Топ путей клиентов с CRM-оплатой
SELECT
    channels_dedup_path                  AS path,
    count()                              AS clients
FROM dm_conversion_paths
WHERE has_crm_paid = 1
GROUP BY path
ORDER BY clients DESC
LIMIT 20

-- Распределение по длине пути
SELECT
    path_length,
    count()     AS clients
FROM dm_conversion_paths
WHERE has_lead = 1
GROUP BY path_length
ORDER BY path_length
```

---

## Правила интерпретации

- **Нет расходов** → не считать CPC, CPM, CPA, ROAS. Если пользователь просит ROAS — объяснить, что данных по расходам нет.
- **Малые выборки** → при n < 5 ставить ⚠️ и предупреждать о ненадёжности.
- **Период** → всегда указывать сравниваемые периоды явно.
- **dm_traffic_performance vs dm_client_profile** → первая даёт сессионную конверсию (приблизительно), вторая — точную клиентскую конверсию по first touch.
- **has_lead vs goal_*** → `has_lead` в dm_client_profile/dm_client_journey — надёжный флаг лида на уровне клиента. Goal-колонки в dm_traffic_performance — сессионные счётчики (могут считаться несколько раз с одного клиента).
- **Сравнение first touch / last touch** → dm_client_profile даёт first_touch. Для last_touch использовать dm_client_journey: последний визит с `is_converting_visit = 1` или последний визит перед `first_lead_date`.
