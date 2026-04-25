# Карта данных ClickHouse (БД `magnetto`, 17 таблиц)

Краткое описание — что в какой таблице лежит и к какому субагенту относится. Полные схемы (столбцы+типы) не в этом файле — субагенты получают их автоматически при обращении.

**Колонка "Skills" ниже — какие SKILL.md релевантны для работы с этой таблицей.** (Generalist сам решает какие читать через свой `read_file`. Main скиллы не передаёт — `task(subagent_type="generalist", description="...")` без skills/tables аргументов.)

## Трафик и визиты (grain: traffic slice / event)

- **`dm_traffic_performance`** — 1 строка = (date × project × utm × device × city). Визиты, bounce, page_views, duration + **62 goal_* колонки** (счётчики целей). ⚠ **НЕТ cost/revenue** — ROAS/CPC из этой таблицы НЕЛЬЗЯ. Для расходов используй `dm_direct_performance`.
  Skills: `clickhouse-basics`, `campaign-analysis`, `anomaly-detection`, `goals-reference`
- **`dm_client_journey`** — 1 строка = 1 визит (event grain). visit_number, traffic_source, utm_*, device, city, duration, page_views + флаги `has_lead`/`has_call`/`has_form`/`has_crm_created`/`has_crm_paid` (visit-level: была ли цель В ЭТОМ визите). ⚠ **visit-level has_lead ≠ client-level has_lead**.
  Skills: `clickhouse-basics`, `attribution`, `goals-reference`, `cohort-analysis`
- **`visits_all_fields`** — raw-дамп Метрики, 51 колонка включая Array-поля: `goalsID`, `purchaseID`, `purchaseDateTime`, `purchaseRevenue`, `purchasedProductID`, `impressionsURL`, `DirectClickOrder`, `DirectBannerGroup` и др. ⚠ **camelCase-имена** (`clientID` а не `client_id`, `dateTime`, `startURL`). Используй только когда витрин недостаточно — покопаться в purchase-массивах, Direct-payload на визит, impressions.
  Skills: `clickhouse-basics`, `goals-reference`

## Клиенты и пути (grain: client)

- **`dm_client_profile`** — 1 строка = 1 client_id. first_visit_date, last_visit_date, days_active, total_visits, projects_visited (Array), `first_traffic_source`, `first_utm_*`, `last_utm_*`, `has_lead` (client-level: был ли КОГДА-НИБУДЬ лид), `first_lead_date`, `days_to_first_lead`, `has_crm_created`, `has_crm_paid`, `crm_paid_date`. 672K клиентов, 1647 с has_lead=1. ⚠ **нет `project_slug`**, есть `first_project` / `last_project` / `projects_visited`. ⚠ **client-level has_lead** (а не per-visit).
  Skills: `clickhouse-basics`, `cohort-analysis`, `attribution`, `segmentation`, `campaign-analysis`
- **`dm_conversion_paths`** — 1 строка = 1 client_id. `channels_path` (Array каналов по визитам), `channels_dedup_path`, `sources_path`, `campaigns_path` (Array), `days_from_first_path` (Array), `path_length`, `first_touch_date`, `conversion_date`, `conversion_window_days`. Для Markov/Shapley атрибуции. ⚠ **нет project_slug вообще**.
  Skills: `clickhouse-basics`, `attribution`, `cohort-analysis`

## Директ — результаты (grain: ad day)

- **`dm_direct_performance`** — 1 строка = (date × campaign_id × adgroup_id × **ad_id** × ad_network_type=SEARCH|AD_NETWORK). `cost`, `clicks`, `impressions`, `sessions`, `bounces`, `purchase_revenue`, `purchase_profit`, `leads_all` (314553735), `unique_calls` (201619840), `targeted_calls` (201619843), `order_created` (332069613), `order_paid` (332069614), `form_submissions` (322914144), `phone_clicks` (314248561), `quiz_completed` (321286959), `spam_traffic` (402733217), `order_create_started` (498366562, **новая цель**), `goal_507627231` (507627231 «Квал.площ+КЦ», **новая цель**), `cabinet_name`. ⚠ **`ad_id` теперь в grain-ключе** (раньше был агрегирован до adgroup). ⚠ **`spam_traffic` — только цель 402733217**, раньше было сумма трёх целей (402733217+405315077+407450615). ⚠ **НЕТ `project_slug`** — только `campaign_name` (парсить). ⚠ **`campaign_id: UInt64`** — в `campaigns_settings` тот же ключ `Int64` → при JOIN нужен CAST. ⚠ `bounces` здесь **счётчик** (UInt64), а в `dm_client_journey.bounce` — флаг 0/1. ⚠ **`purchase_revenue` пуст с 2025-11-17** (проблема в Direct API / ETL).
  **Обычно направляй в subagent `direct-optimizer` через `task(...)`, а не в generalist.**

## Директ — оптимизация (grain: ad unit + settings)

- **`bad_keywords`** — snapshot-рейтинг ключевых фраз. 1.4K строк, только 1 день (`report_date = 2026-04-15` на текущий момент). Поля: `Criterion` (ключ), `CampaignId`, `AdGroupId`, `clicks`, `cost`, `ctr`, `cpc`, `roas`, `goal_score`, `zone_status` (green/yellow/red), `bid_zone`, `cabinet_name`. ⚠ **PascalCase имена** `CampaignId`/`CampaignName` (не snake как в dm_direct_performance).
- **`bad_placements`** — snapshot-рейтинг площадок РСЯ. 67K строк, `Placement`, `CampaignId`, `clicks`, `cost`, `roas`, `bounce_rate`, `goal_score_rate`, `cpc_deviation`, `roas_deviation`, `zone_status`, `zone_reason`, `is_recent`.
- **`bad_queries`** — snapshot-рейтинг поисковых запросов. 2.8K строк, `Query`, `CriterionType` (keyword/autotargeting), `TargetingCategory`, `matched_keyword`, `is_chronic`, `is_recent`, `days_active`, `zone_status`, `zone_reason`.
- **`campaigns_settings`** — конфигурация кампаний. 79 строк. `campaign_id: Int64`, `campaign_name`, `campaign_type`, `status`, `state`, `start_date`, `end_date`, `daily_budget_amount: Decimal`, `strategy_search_type`, `strategy_network_type`, `attribution_model`, `priority_goal_ids: Array(Int64)`, `priority_goal_values: Array(Decimal)`, `negative_keywords: Array(String)`, `excluded_sites: Array(String)`, `time_targeting_schedule: Array(String)`, `cabinet_name`.
- **`adgroups_settings`** — настройки групп. 1.2K строк. `group_id`, `group_name`, `campaign_id: Int64`, `keywords: Array(String)`, `negative_keywords: Array(String)`, `region_ids: Array(Int64)`, `autotargeting_state/status/exact/alternative/competitor/broader/accessory/brand_*`.
- **`ads_settings`** — креативы и модерация. 19K строк. `ad_id`, `campaign_id: Int64`, `status`, `state`, `ad_type`, `title`, `title2`, `text`, `href`, `final_url`, `display_domain`, `image_ad_title/text/href`, `vcard_moderation`, `ad_image_moderation`, `sitelinks_moderation`.

## Командный центр (grain: daily snapshot)

Дневные snapshot-витрины «что в портфеле сегодня». Каждая строка — состояние сущности на `report_date = today()`. Используются UI-дашбордом командного центра и для быстрых ответов про текущее состояние портфеля. Витрины собираются ночным materialized view поверх `dm_direct_performance` + `*_settings` + `budget_reallocation`.

- **`command_center_campaigns`** — 1 строка = (report_date × campaign_id). JOIN актуального snapshot'а `dm_direct_performance` (7d window) + `campaigns_settings` + `budget_reallocation`. Поля: `campaign_name`, `campaign_type`, `meta_state`, `status`, `state`, `search_strategy`, `network_strategy`, `attribution_model`, `weekly_budget` (рекомендация от ИИ-агента), `traffic_mix` (search/network/mixed), `semantic_tags: Array(String)`, `cabinet_name`. Метрики за 7 дней (_week) и предыдущие 7 дней (_prev): `cost`, `revenue`, `impressions`, `clicks`, `leads`, `calls` (=unique_calls), `forms`, `orders`, `spam_traffic`, `targeted_calls`, `order_create_started`, `order_created`, `goal_507627231`, `unique_calls`, `quiz_completed`, `phone_clicks`. Производные: `roas_week`, `cpa_week`, `cpc_week`, `ctr_week`. История за 12 недель: `history_weeks: Array(Date)`, `history_cost/revenue/clicks/leads/calls/forms/orders: Array(...)`. Настройки из `campaigns_settings`: `priority_goal_ids: Array(Int64)`, `priority_goal_values: Array(Float64)` — параллельные массивы (id цели в Метрике → её ценность в ₽). Цветовая маркировка: `health LowCardinality(String)` (green/yellow/red/pending), `health_reason String`. ⚠ **один report_date = today()** — для исторических срезов бери history_*, а не старые report_date. ⚠ **summary на портфеле** — это `sum()` по всем строкам последнего report_date, avg_cpc считается как `total_cost / total_clicks`, НЕ среднее от cpc_week.
- **`command_center_adgroups`** — 1 строка = (report_date × group_id). Набор метрик аналогичен campaigns (но агрегировано по группе). Дополнительные поля: `group_name`, `serving_status`, `group_type`, `keyword_count`, `autotargeting_state`, `autotargeting_risky`. История: `history_weeks/cost/clicks/leads`. ⚠ **фильтрует только серверно-активные группы** (`status='ACCEPTED' AND serving_status='ELIGIBLE'`) — поэтому сумма по adgroups для одной кампании МОЖЕТ быть МЕНЬШЕ `campaigns_mart.cost_week` на величину кликов/расходов в неактивных группах. Это норма.
- **`command_center_ads`** — 1 строка = (report_date × ad_id). Источник — все 4 кабинета `tab1..tab4`. Поля: `ad_type`, `ad_subtype`, `status`, `state`, `status_clarification`, `title`, `title2`, `text_body`, `final_url`, `has_image`, `vcard_moderation`, `ad_image_moderation`, `sitelinks_moderation`, `cabinet_name`. Метрики _week/_prev: `cost`, `clicks`, `sessions`, `bounces`, `leads`, `spam_traffic`, `cpc`, `bounce_rate`. Health: REJECTED → red; `cost+cost_prev < 100` → pending; `spam_traffic/clicks > 40%` → yellow; иначе green. ⚠ **`ad_id = 0` исключены** (фильтр `ad_id > 0`) — smart/dynamic-кампании без привязки к конкретному объявлению. Поэтому `SUM(clicks_week)` по ads-mart < campaigns-mart на величину ad_id=0 трафика. Нормально.
- **`budget_reallocation`** — рекомендации агента по weekly-бюджету и цветовой зоне (health). Источник для `weekly_budget` в command_center_campaigns и для `/api/budget` endpoint'а. Поля: `campaign_id`, `cabinet_name`, `current_weekly_budget`, `recommended_weekly_budget`, `delta_rub`, `delta_pct`, `zone_status` (green/yellow/red), `rationale`, `expected_weekly_cost`, `expected_weekly_revenue`, `baseline_weekly_*`, `forecast_elasticity`, `forecast_conf_low/high`, `delta_revenue_weekly`. Обновляется ночным MV, затем потребляется командным центром.

Skills для command_center-витрин: `clickhouse-basics`, `campaign-analysis`, `goals-reference`.

**Типичные паттерны запросов к command_center_*:**
- «Какие кампании в красной зоне?» → `SELECT campaign_name, health_reason FROM command_center_campaigns WHERE health='red' AND report_date=(SELECT max(report_date) FROM ...)`
- «Почему у кампании X мусорный трафик 40%?» → drill в `command_center_adgroups WHERE campaign_id=X` + `command_center_ads WHERE adgroup_id IN (...)`
- «Какой ROI по портфелю за неделю?» → `SELECT sum(revenue_week)/nullIf(sum(cost_week),0) FROM command_center_campaigns WHERE report_date=(max)`.

## Скоринг и аналитика решений (grain: snapshot)

- **`dm_active_clients_scoring`** — snapshot-скоринг клиентов. 367K строк (subset активных). `client_id`, `total_visits`, `last_visit_date`, `days_since_last`, `first_traffic_source`, `last_traffic_source`, `last_project`, `has_lead`, `lift_score: Float32` (**0..19020**), `matched_goals`, `priority: String` (hot/warm/cold), `next_step: UInt8` (номер следующего шага), `recommended_goal_id`, `recommended_goal_name`, `recommended_lift`, `optimal_retarget_days`, `snapshot_date`. ⚠ `lift_score: Float32` (здесь) ≠ `lift_score: UInt32` в report_daily_briefing (там уже нормализован).
- **`dm_step_goal_impact`** — snapshot lift-анализа целей по шагам визитов. 238 строк. `visit_number`, `goal_id`, `goal_name`, `clients_at_step`, `clients_with_goal`, `clients_without_goal`, `converters_with_goal`, `converters_without_goal`, `rate_with_goal`, `rate_without_goal`, `lift`. Используется для рекомендаций "какую цель продвигать на шаге N".
- **`dm_funnel_velocity`** — snapshot скорости воронки по когортам недель. 25 строк. `cohort_week`, `cohort_age_days`, `new_clients`, `clients_with_lead`, `lead_rate_pct`, `avg_days_to_lead`, `median_days_to_lead`, `clients_with_crm`, `crm_rate_from_lead_pct`, `avg_days_lead_to_crm`, `clients_paid`, `paid_rate_from_crm_pct`. ⚠ **для зрелых метрик фильтруй `cohort_age_days >= 60`** — молодые когорты не дозрели.
- **`dm_path_templates`** — snapshot паттернов каналов. 54 строки. `pattern: Array(String)` (последовательность каналов), `dedup_steps`, `ad_touches`, `total_clients`, `converters`, `cr_pct`, `avg_visits`, `avg_window_days`, `median_window_days`, `estimated_path_cost`, `cost_per_conversion`.

## Отчёты

- **`report_daily_briefing`** — ежедневный брифинг: 50 горячих клиентов на день + `analyst_comment`. `client_id`, `priority`, `total_visits`, `days_since_last_visit`, `first_traffic_source`, `lift_score: UInt32`, `next_target_action: String`, `retarget_in_days`, `action_conversion_lift: UInt32`, `analyst_comment`, `report_date`. Natural entry-point для утреннего диалога с маркетологом.

---

## Маркеры путаницы (перекрытие имён и семантики)

### 1. `has_lead` — 3 разные семантики

| Где | Смысл |
|---|---|
| `dm_client_profile.has_lead` | **client-level**: этот клиент КОГДА-ЛИБО оставил лид (1647 уникальных клиентов) |
| `dm_client_journey.has_lead` | **visit-level**: в ЭТОМ визите была цель лида (1737 строк-визитов с флагом) |
| `dm_conversion_paths.has_lead` | **client-level**: клиент дошёл до лида через рассматриваемый путь |
| `dm_active_clients_scoring.has_lead` | **client-level**, но ТОЛЬКО по активным клиентам (1044 vs 1647 в profile) |

На вопрос "сколько лидов" — profile даст клиентов, journey даст визиты, разница ~5-10%. Уточняй у пользователя если неоднозначно.

### 2. Naming case inconsistency

| snake_case | PascalCase/camelCase | Таблицы |
|---|---|---|
| `campaign_id: UInt64` | `CampaignId: UInt64` | dm_direct_performance vs bad_keywords / bad_placements / bad_queries |
| `campaign_name` | `CampaignName` | same |
| `client_id: UInt64` | `clientID: UInt64` | dm_* vs visits_all_fields |
| `goal_id: UInt32` | `goalsID: Array(UInt32)` | dm_step_goal_impact vs visits_all_fields |
| `date` | `dateTime`, `startURL`, etc. | dm_* vs visits_all_fields |

При JOIN с visits_all_fields или bad_* помни про case. Для `campaigns_settings.campaign_id: Int64` vs `dm_direct_performance.campaign_id: UInt64` — нужен `CAST`.

### 3. bounce vs bounces — флаг или счётчик

| Колонка | Таблицы | Смысл |
|---|---|---|
| `bounce: UInt8` | `dm_client_journey`, `visits_all_fields` | флаг 0/1 на визит |
| `bounces: UInt32+` | `dm_traffic_performance`, `dm_direct_performance`, `bad_placements` | счётчик отказов в агрегате |

`SUM(bounce)` на journey = число отказных визитов. `SUM(bounces)` на traffic — уже агрегат, нельзя делить на `COUNT()`. Правильно: `bounces / visits` в рамках одной строки.

### 4. `lift_score` — несовместимые типы

| Таблица | Тип | Диапазон |
|---|---|---|
| `dm_active_clients_scoring.lift_score` | Float32 | 0.0 .. 19020.78 |
| `report_daily_briefing.lift_score` | UInt32 | 262 .. 4135 |

Briefing — округлённая нормализованная версия. Нельзя напрямую сравнивать с scoring.

### 5. ROAS/cost — только в Директ-витринах, не в Метрика-витринах

| Нужно | Где `cost` | Ограничения |
|---|---|---|
| ROAS по кампаниям | dm_direct_performance | нет project_slug (только campaign_name, парсить), нет UTM |
| ROAS по ключам | bad_keywords | snapshot 1 дня |
| ROAS по площадкам | bad_placements | snapshot 1 дня |
| **ROAS по UTM / городу / девайсу** | **❌ не существует** | dm_traffic_performance не имеет cost; UTM-level ROAS недоступен |

Если вопрос про "ROAS по UTM-кампаниям" или "CPC по городам" — это **невозможно**, нужно объяснить ограничение.

### 6. `project_slug` — не везде

| Есть project_slug | Нет project_slug | Альтернатива |
|---|---|---|
| `dm_traffic_performance` ✅ | `dm_client_profile` | `first_project`, `last_project`, `projects_visited: Array` |
| `dm_client_journey` ✅ | `dm_active_clients_scoring` | `last_project` |
| | `dm_conversion_paths` | **нет никакой разбивки** |
| | `dm_direct_performance` | только `campaign_name`, парсить строку |
| | `visits_all_fields` | только `startURL`, парсить URL |
| | `bad_*`, `*_settings` | только `CampaignName`, парсить |

На вопрос "по проекту X" — сначала проверь где фильтровать. Если в Директ-витрине — парсинг `campaign_name` через `ILIKE`/`positionCaseInsensitive`.

### 7. Три разных date-поля

| Поле | Таблицы | Что значит |
|---|---|---|
| `date: Date` | dm_traffic_performance, dm_client_journey, dm_direct_performance, visits_all_fields | транзакционная дата |
| `dateTime: DateTime` | visits_all_fields | timestamp визита |
| `report_date: Date` | bad_keywords, bad_placements, bad_queries, report_daily_briefing | snapshot-дата (один день!) |
| `snapshot_date: Date` | dm_active_clients_scoring, dm_step_goal_impact, dm_funnel_velocity, dm_path_templates | snapshot-дата (один день!) |

**Критично:** на snapshot-таблицах фильтр `WHERE snapshot_date >= today() - 7` даст либо всё, либо ничего (история не сохраняется). Используй `WHERE snapshot_date = (SELECT max(snapshot_date) FROM X)`.

### 8. Фильтрация "только вчера и раньше"

Для всех транзакционных (date-based) таблиц: **`WHERE date < today()`** — данные за сегодня неполные.

### 9. Проекты (ЖК) — какие есть

Топ-5 по трафику: `costura-town`, `origana`, `niti`, `zk-1712`, `rivayat` (≈88% трафика). Есть ещё ~40 проектов с низким трафиком и кодовыми именами (цифры).

### 10. Цель 314553735 "Все лиды magnetto"

Основная лид-цель Метрики. При расчёте лидов — либо флаг `has_lead` (агрегат), либо `goal_314553735 > 0` в `dm_traffic_performance`. Детальный справочник целей — в `/skills/goals-reference/SKILL.md`.
