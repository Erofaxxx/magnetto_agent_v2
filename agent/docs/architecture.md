# Архитектура агента Magnetto

Документ описывает, как устроен `clickhouse_analytics_agent` (deepagents + LangGraph + FastAPI), что попадает в контекст на каждом шаге, что кэшируется и где редактировать промпты/навыки/подагентов.

На проде работает режим `USE_DEEPAGENTS=1` — всё ниже описывает этот путь. Legacy-классы в `agent/subagents/*.py` и `agent/tools_subagents.py` в этом режиме не задействованы.

---

## 1. Компоненты на одной картинке

```
┌───────────────────────────────────────────────────────────────────────────┐
│ Frontend (chat.asktab.ru)                                                 │
│   ├─ /api/ai/analyze.php, /api/ai/job.php — прокси к агенту               │
│   └─ /api/command_center/{campaigns,adgroups,ads}.php — прокси (быстрые)  │
└───────────────┬───────────────────────────────────────────────────────────┘
                │ agent_id=3 → https://magnetto.asktab.ru
                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ FastAPI (agent/api_server.py, порт 8000, systemd analytics-agent)         │
│   ├─ POST /api/analyze          → _new_job + asyncio.create_task          │
│   ├─ GET  /api/job/{id}         → polling                                 │
│   ├─ GET  /api/command_center/* → прямой SELECT через _reports_query_dicts│
│   └─ GET  /api/tables, /api/budget — прямой SELECT                        │
└───────────────┬───────────────────────────────────────────────────────────┘
                │ agent.invoke({"messages": [...]}, config={"configurable": {"thread_id": session_id}})
                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ build_agent() (agent/core/agent_factory.py) — singleton на (client, model)│
│   deepagents.create_deep_agent(model, tools, memory, skills, subagents,   │
│                                middleware, backend, checkpointer)         │
│                                                                           │
│   Main agent (Claude Sonnet 4.6)                                          │
│    ├─ tools: think_tool, clickhouse_query, python_analysis, list_tables,  │
│    │         delegate_to_generalist, task, write_todos,                   │
│    │         ls/read_file/write_file/glob/grep (виртуальная ФС)           │
│    ├─ memory: AGENTS.md + data_map.md (в system prompt)                   │
│    ├─ skills index: clients/magnetto/skills/ + shared_skills/ (по требов.)│
│    ├─ subagents: command-center, direct-optimizer, scoring-intelligence   │
│    └─ middleware: Caching, Budget, RoutingEnforcer, HardcodeDetector,     │
│                   DynamicContext                                          │
│                                                                           │
│    ┌──────── task(subagent_type=...) ─────────┐                           │
│    ▼                                          ▼                           │
│   Subagent "command-center"   Subagent "direct-optimizer" …               │
│    ├─ tools: clickhouse_query, python_analysis, think_tool                │
│    ├─ system prompt: SUBAGENT.md body + schema_section                    │
│    ├─ skills: subagents/<name>/skills/ + shared_skills/                   │
│    └─ middleware: Caching, DynamicContext                                 │
└───────────────┬───────────────────────────────────────────────────────────┘
                │ clickhouse_query → _get_ch_client() (user: User_magnetto)
                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ ClickHouse (clickhouse.asktab.ru:8443, БД magnetto)                       │
│   Два юзера:                                                              │
│    ├─ User_magnetto        — tools.clickhouse_query (ответы на вопросы)   │
│    └─ reports_magnetto     — _reports_query_dicts (REST endpoints)        │
└───────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Жизненный цикл запроса

### 2.1 Приём и постановка job

1. Фронт: `POST /api/ai/analyze.php` → PHP-прокси → `POST https://magnetto.asktab.ru/api/analyze` с телом `{query, session_id?, model?}`.
2. `api_server.analyze()` создаёт `job_id`, кладёт в in-memory словарь `_jobs`, запускает `asyncio.create_task(_run_agent_job(job_id))`, сразу возвращает `{job_id, session_id, status: "pending"}`.
3. Фронт раз в ~3 сек опрашивает `GET /api/job/{job_id}`. Когда `status="done"`, читает `text_output`, `plots`, `tool_calls`.

### 2.2 Что исполняет `_run_agent_job`

```
agent = build_agent(client_id="magnetto", model=...)         # закэширован
agent.invoke(
    {"messages": [HumanMessage(query)]},
    config={"configurable": {"thread_id": session_id},
            "recursion_limit": ~60},
)
```

`build_agent()` (см. `core/agent_factory.py`) собирает единый deepagents-граф один раз на `(client_id, model)` и хранит его в `_AGENT_CACHE`. Checkpointer (SqliteSaver → `chat_history.db`) сохраняет историю диалога между turn-ами по `thread_id = session_id`.

### 2.3 Шаги внутри deepagents-графа

На каждый turn main agent:
1. Получает в context **все** сообщения сессии (checkpointer их подгружает по `thread_id`).
2. Вызывает LLM → LLM возвращает либо финальный text, либо tool_call.
3. Если tool_call — исполняет tool (clickhouse_query / task / write_todos / ...), результат идёт обратно в messages.
4. Цикл до финального ответа или до `BudgetMiddleware` лимита.

При `task(subagent_type=..., description=...)` deepagents запускает **вложенный граф** — это отдельный агент со своим system-prompt, tools и middleware (см. §4). Вложенный граф работает изолированно, вернёт финальный текст — main agent получает его как `ToolMessage`.

---

## 3. Main agent

### 3.1 Где системный промпт

System prompt main-agent собирается deepagents-ом из нескольких источников, подаваемых в `create_deep_agent(...)`:

| Блок | Источник | Меняется |
|---|---|---|
| Базовый deepagents-wrapper | в коде deepagents (не наш) | с версией библиотеки |
| Описание tools | объявления `@tool` в `agent/core/tools.py` + `subagent_loader.py` + `delegate_to_generalist.py` | при правке сигнатур tools |
| `memory` | `clients/magnetto/AGENTS.md` + `clients/magnetto/data_map.md` (читаются целиком) | свободно, см. §3.2 |
| `skills` (индекс) | заголовки всех `SKILL.md` в `clients/magnetto/skills/` + `clients/magnetto/shared_skills/` | при добавлении/удалении папок (progressive disclosure) |
| Subagent descriptions | frontmatter.description из `clients/magnetto/subagents/*/SUBAGENT.md` | при правке SUBAGENT.md |
| **Блок «Сегодня + НДС»** | `DynamicContextMiddleware._dynamic_block()` | **раз в сутки** (дата меняется в полночь МСК) |

Блок «Сегодня + НДС» формируется в рантайме при каждом model-call, но `DynamicContextMiddleware` стоит **первым** в списке (outermost) — поэтому `CachingMiddleware` (второй) видит уже дополненный system prompt и ставит `cache_control` **ПОСЛЕ** блока. Блок оказывается ВНУТРИ кэша → это жёсткая часть системной инструкции. Cache miss происходит ровно один раз в сутки в полночь МСК, когда строка «Сегодня: X» меняется (байт-стрим system становится другим).

### 3.2 Редактирование системной части main-агента

| Что править | Файл |
|---|---|
| Роль / правила делегирования / стиль ответа | `agent/clients/magnetto/AGENTS.md` |
| Карта таблиц и ⚠-маркеров | `agent/clients/magnetto/data_map.md` |
| Описание нового скилла (подтянется в индекс) | новая папка `agent/clients/magnetto/skills/<slug>/SKILL.md` с frontmatter `name` + `description` |
| Shared-скилл для всех subagents тоже | `agent/clients/magnetto/shared_skills/<slug>/SKILL.md` |
| Сегодня/НДС блок | `agent/core/dynamic_context_middleware.py` — функция `_dynamic_block()` |

После правки — **не требуется** рестарт, **если**:
- поправили только body SKILL.md (progressive disclosure читает файлы по требованию)

Требуется `systemctl restart analytics-agent`, если:
- поправили AGENTS.md / data_map.md / SUBAGENT.md frontmatter / добавили-удалили папку в skills/ (индекс строится один раз при `build_agent()`)

### 3.3 Инструменты main-агента (приоритет делегирования)

Объявления — `agent/core/tools.py` + `core/delegate_to_generalist.py` + встроенный `task` от deepagents.

| Tool | Назначение | Ограничение |
|---|---|---|
| `write_todos(...)` | план на 2+ шага **до** первого SQL/делегации | обязателен для multi-step |
| `think_tool(thought)` | фиксация гипотезы/плана (аудитируется RoutingEnforcer) | — |
| `task(subagent_type, description)` | делегировать специализированному subagent | см. §4 |
| `delegate_to_generalist(task, tables, skills)` | одноразовый generalist, main вручную указывает tables и skills | для нестандартных вопросов |
| `clickhouse_query(sql)` | **только** COUNT / MAX(date) / DISTINCT / post-processing | `RoutingEnforcer` блокирует сложные SQL, если не было делегации в turn |
| `python_analysis(code, parquet_path)` | post-process parquet от subagent (графики, merge) | `HardcodeDetector` блокирует `pd.DataFrame({...})` с литералами |
| `list_tables()` | резерв, если нужна таблица не из data_map.md | — |
| виртуальная ФС (`ls`, `read_file`, `write_file`, `glob`, `grep`) | работа с `/plots/`, `/memories/`, подгрузка `/skills/<x>/SKILL.md` | — |

### 3.4 Middleware main-агента (порядок важен)

В `create_deep_agent(middleware=[...])` — порядок в списке. Первый элемент — **outermost**, его `wrap_*` вызывается первым и может модифицировать `request` до того, как следующие middleware и сам model-call его увидят.

| № | Middleware | Когда срабатывает | Что делает |
|---|---|---|---|
| 1 | `DynamicContextMiddleware` | `wrap_model_call` (outermost) | добавляет блок «Сегодня: YYYY-MM-DD / НДС в РФ: 22%» в конец `system_message.content` **ДО** Caching. Делает это на каждый call, но значение меняется раз в сутки → в пределах дня байт-стрим стабилен |
| 2 | `CachingMiddleware` | `wrap_model_call` | ставит `cache_control: ephemeral` на последний блок system (включая only что добавленный today+VAT) + на последний ToolMessage (граница истории) + на последний HumanMessage (свежий ввод). Всё ДО этих точек уходит в Anthropic ephemeral-кэш |
| 3 | `BudgetMiddleware` | `wrap_model_call` | считает tool-calls в turn; по превышении лимита подмешивает «budget notice» в system |
| 4 | `RoutingEnforcer` | `wrap_tool_call` | перехватывает сложные `clickhouse_query` без предшествующей делегации — возвращает блокирующий `ToolMessage` |
| 5 | `HardcodeDetector` | `wrap_tool_call` | ловит `pd.DataFrame({...: [литералы]})` в `python_analysis` |

Каждый middleware — `agent/core/<name>_middleware.py`.

---

## 4. Подагенты (subagents)

Декларативная регистрация: каждая папка `agent/clients/magnetto/subagents/<name>/` с файлом `SUBAGENT.md`. Loader — `agent/core/subagent_loader.py`, подхватывает их при `build_agent()`.

### 4.1 Формат SUBAGENT.md

```yaml
---
name: command-center                  # имя subagent'а, оно же аргумент task(subagent_type=...)
description: |
  Когда делегировать сюда (читается main-агентом).
  Когда НЕ делегировать (исключения).
model: anthropic/claude-sonnet-4.6
schema_tables:                        # имена таблиц → будут подставлены в {schema_section}
  - command_center_campaigns
  - command_center_adgroups
  - command_center_ads
  - budget_reallocation
---

Ты — аналитик ...                     # body — это system prompt subagent'а
{schema_section}                      # placeholder, рендерится из SchemaCache
...
```

Skills subagent'а:
- `agent/clients/magnetto/subagents/<name>/skills/<slug>/SKILL.md` — специфичные
- `agent/clients/magnetto/shared_skills/<slug>/SKILL.md` — общие (те же что видит main, но subagent подгружает сам)

### 4.2 Текущие subagents

| Subagent | model | Таблицы (schema_tables) | Skills свои | shared_skills общие |
|---|---|---|---|---|
| **command-center** | claude-sonnet-4.6 | `command_center_campaigns`, `command_center_adgroups`, `command_center_ads`, `budget_reallocation` | `command-center-marts`, `command-center-drill`, `command-center-selection` | `clickhouse-basics`, `python-analysis`, `visualization` |
| **direct-optimizer** | claude-sonnet-4.6 | `bad_keywords`, `bad_placements`, `bad_queries`, `campaigns_settings`, `adgroups_settings`, `ads_settings`, `dm_direct_performance` | `direct-keywords-placements`, `direct-queries`, `direct-performance` | (те же shared) |
| **scoring-intelligence** | claude-sonnet-4.6 | `dm_active_clients_scoring`, `dm_step_goal_impact`, `dm_funnel_velocity`, `dm_path_templates`, `report_daily_briefing` | `scoring-clients`, `scoring-step-impact`, `scoring-funnel-paths` | (те же shared) |

### 4.3 Tools subagents

У всех subagents одинаковый набор (`tool_list_subagent` в `agent_factory.py`):

```python
tool_list_subagent = [clickhouse_query, python_analysis, think_tool]
```

**НЕ имеют** (в отличие от main):
- `list_tables`, `delegate_to_generalist`, `task`, `write_todos`, виртуальная ФС (`ls/read_file/...`).

Это значит: subagent не может вложенно делегировать дальше, не строит todos, не читает `/memories/`. Его работа — один изолированный вопрос + 1-3 SQL + финальный текст.

### 4.4 Middleware subagents

После `load_subagents()` в `agent_factory.py` каждому spec'у задаётся **тот же порядок**, что у main:

1. `DynamicContextMiddleware` (outermost) — today+НДС добавляется в system ДО cache_control
2. `CachingMiddleware` — ставит cache_control на стабильный system (включая today) + на последний HumanMessage (это входящий `description` от main-агента)

**BudgetMiddleware / RoutingEnforcer / HardcodeDetector** к subagents **не применяются** — у них свой itertation limit (`max_iterations=10` на subagent против 30 у main).

### 4.5 Что кладётся в контекст subagent'а при запуске

Каждый вызов `task(subagent_type="X", description="...")`:

```
system:                                                  ← всё это в кэше
  <body SUBAGENT.md с подставленным {schema_section}>     (включая схему таблиц
  <Индекс доступных skills: имена + descriptions>         из SchemaCache и ваш
  Сегодня: YYYY-MM-DD. Любой период без года — YYYY.      собственный SUBAGENT.md)
  НДС в РФ: 22% ...

  <<< cache_control: ephemeral на последнем блоке >>>

messages:
  HumanMessage(description)                              ← main передаёт сюда всё, что
    + cache_control: ephemeral                             знает по задаче (см. AGENTS.md
                                                           «Протокол task(description=...)»)
  ... (далее цикл tool_call → tool_result → ai → ...)
```

Исторические сообщения основной сессии subagent **не видит** — он всегда стартует с чистой истории на свой вопрос. Поэтому main должен класть в `description` всё нужное (фильтры, период, формат ответа, уже выгруженные parquet) — иначе subagent будет задавать SQL для того, что main уже знает.

Полная task от main'а → HumanMessage → получает `cache_control` от CachingMiddleware. Если main за сессию обращается к этому же subagent'у с тем же description (например, уточняющий вопрос поверх той же задачи) — cache hit.

### 4.6 Как добавить нового subagent'а

1. `mkdir agent/clients/magnetto/subagents/<new-name>/`
2. Создать `SUBAGENT.md` с frontmatter (см. §4.1). Важно:
   - `name` совпадает с именем папки
   - `description` — **роутер читает именно его**, чтобы решить когда делегировать в этот subagent
   - `schema_tables` — только таблицы, реально нужные; это рендерится в полноценную схему с типами
3. (Опц.) `mkdir agent/clients/magnetto/subagents/<new-name>/skills/` + 2-3 `SKILL.md` с progressive-disclosure frontmatter
4. (Опц.) Обновить `description` у соседних SUBAGENT.md — добавить «НЕ используй для <твоей зоны> (это `<new-name>`)»
5. `systemctl restart analytics-agent`

Никакой Python-код править не нужно — `subagent_loader.load_subagents()` подхватит автоматически.

---

## 5. delegate_to_generalist — третий путь

Файл: `agent/core/delegate_to_generalist.py`.

Когда main-агент сталкивается с вопросом, который не матчит ни один SUBAGENT (трафик, UTM, профили клиентов, атрибуция, когорты, сегменты), он зовёт:

```
delegate_to_generalist(
    task="что нужно сделать",
    tables=["dm_traffic_performance", "dm_client_journey"],
    skills=["attribution", "cohort-analysis"],
)
```

В отличие от SUBAGENT:
- Generalist не декларируется SUBAGENT.md — он строится динамически каждый вызов
- Main сам указывает таблицы и скиллы (subagent сам их не выбирает)
- System prompt generalist = **stable-base** (`_GENERALIST_BASE` в файле) + schema_section по переданным tables (сортируется) + body переданных skills (сортируются)
- Идентичные `(tables, skills)` → идентичный byte-stream prompt → cache hit со 2-го вызова

Middleware: `CachingMiddleware` + `DynamicContextMiddleware` (см. `agent_factory.py` строка про `delegate_tool`).

---

## 6. Что кэшируется (Anthropic prompt caching)

Anthropic требует: `cache_control: ephemeral` на блоке → всё **ДО и включая этот блок** кэшируется (TTL 5 мин), всё ПОСЛЕ — нет.

`CachingMiddleware` (`agent/core/caching_middleware.py`) ставит cache_control в трёх точках:

1. **System prompt** — на последний блок (AGENTS.md + data_map + skills index + SUBAGENT.md body + **блок «Сегодня + НДС»** от DynamicContextMiddleware — всё кэшируется как единый стабильный префикс).
2. **Последний ToolMessage** в messages — граница «вся история tool_call/tool_result до этой точки». Внутри одного turn это то, что накопилось в ходе итераций: tool_calls с их результатами. CachingMiddleware ставит cache_control только на последний — но за счёт ephemeral TTL + идентичного префикса Anthropic кэширует всё что выше.
3. **Последний HumanMessage** — граница «свежий пользовательский ввод» (или `description` от main → subagent). Включён в кэш, чтобы при следующем model-call в этом же turn вопрос не перекладывался заново.

### 6.1 Что в кэше на каждом model-call

- System prompt целиком: AGENTS.md + data_map + skills index + subagents descriptions + блок «Сегодня + НДС»
- Для subagent: его SUBAGENT.md body + schema_section + индекс его skills + тот же блок «Сегодня + НДС»
- Вся история сообщений (HumanMessage, AIMessage, ToolMessage) до последнего HumanMessage включительно
- Последний `description` от main → subagent (это HumanMessage с позиции subagent'а)

### 6.2 Что НЕ в кэше

- Новый tool_call, который модель собирается сделать на этом model-call (его ещё нет в messages)
- Новый tool_result, который придёт после выполнения tool — он попадёт в кэш только на СЛЕДУЮЩЕМ model-call (когда станет «последним ToolMessage»)

Таким образом вся стабильная часть растущей истории цепочки tool-calls накапливается в кэше, и модель не переплачивает за повторное чтение уже увиденного контекста.

### 6.2 Эффект на стоимость

Claude Sonnet 4.6 через OpenRouter:
- Input без кэша: $3.00 / 1M
- Cache write (первый запрос сессии): $3.75 / 1M (+25% к обычному)
- Cache read (повторы в пределах 5 мин): $0.30 / 1M (**×10 дешевле**)

Типичный запрос к subagent: ~8–15K input tokens. Без кэша: $0.03–0.05 за запрос. С кэшем со 2-го запроса: $0.003–0.005.

### 6.3 Как проверить что cache хитает

В OpenRouter Activity dashboard для каждого запроса видно `cached_tokens`. Если > 0 — cache hit. В коде можно посмотреть через `response.usage_metadata` / `response_metadata["token_usage"]["prompt_tokens_details"]["cached_tokens"]`.

Проверка через curl пример — см. `tmp/cache_probe.py` в истории коммитов (уже удалён, но паттерн стандартный).

---

## 7. Права ClickHouse

Два юзера, потому что два разных кодопути:

| Юзер | Где используется | Какие таблицы нужны |
|---|---|---|
| `User_magnetto` | `tools.clickhouse_query` (main + все subagents через deepagents) | все таблицы magnetto, которые упомянуты в SUBAGENT.md → `schema_tables`, в `data_map.md`, или переданы в `delegate_to_generalist(tables=...)` |
| `reports_magnetto` | `api_server._reports_query_dicts` (endpoints `/api/budget`, `/api/command_center/*`, `/api/tables`) | только те, из которых читают endpoints — сейчас `dm_direct_performance`, `budget_reallocation`, `direct_custom_report_*`, `command_center_campaigns/_adgroups/_ads` |

### 7.1 Текущие GRANT (после апрельских правок)

```sql
-- Базовые
GRANT SELECT ON magnetto.dm_direct_performance       TO User_magnetto, reports_magnetto;
GRANT SELECT ON magnetto.dm_traffic_performance      TO User_magnetto;
GRANT SELECT ON magnetto.dm_client_journey           TO User_magnetto;
GRANT SELECT ON magnetto.dm_client_profile           TO User_magnetto;
GRANT SELECT ON magnetto.dm_conversion_paths         TO User_magnetto;
GRANT SELECT ON magnetto.visits_all_fields           TO User_magnetto;

-- Директ-оптимизация
GRANT SELECT ON magnetto.bad_keywords                TO User_magnetto;
GRANT SELECT ON magnetto.bad_placements              TO User_magnetto;
GRANT SELECT ON magnetto.bad_queries                 TO User_magnetto;
GRANT SELECT ON magnetto.campaigns_settings          TO User_magnetto;
GRANT SELECT ON magnetto.adgroups_settings           TO User_magnetto;
GRANT SELECT ON magnetto.ads_settings                TO User_magnetto;

-- Скоринг
GRANT SELECT ON magnetto.dm_active_clients_scoring   TO User_magnetto;
GRANT SELECT ON magnetto.dm_step_goal_impact         TO User_magnetto;
GRANT SELECT ON magnetto.dm_funnel_velocity          TO User_magnetto;
GRANT SELECT ON magnetto.dm_path_templates           TO User_magnetto;
GRANT SELECT ON magnetto.report_daily_briefing       TO User_magnetto;

-- Командный центр (выданы недавно)
GRANT SELECT ON magnetto.command_center_campaigns    TO User_magnetto, reports_magnetto;
GRANT SELECT ON magnetto.command_center_adgroups     TO User_magnetto, reports_magnetto;
GRANT SELECT ON magnetto.command_center_ads          TO User_magnetto, reports_magnetto;
GRANT SELECT ON magnetto.budget_reallocation         TO User_magnetto, reports_magnetto;
```

### 7.2 При добавлении новой таблицы в агент

1. Дописать запись в `agent/clients/magnetto/data_map.md`.
2. Если таблица идёт только через subagent — добавить её в `schema_tables:` в SUBAGENT.md соответствующего subagent.
3. Выдать `GRANT SELECT ON magnetto.<new_table> TO User_magnetto;` (а если из endpoint'а — ещё и `reports_magnetto`).
4. Рестарт агента, чтобы SchemaCache пересчитал схему.

---

## 8. Скиллы — как устроены и когда читаются

Каталоги:
- `agent/clients/magnetto/skills/` — скиллы для main-агента (он их видит только в индексе, читает body по требованию через `read_file("/skills/<slug>/SKILL.md")`)
- `agent/clients/magnetto/shared_skills/` — те же principles, но доступны ещё и subagents
- `agent/clients/magnetto/subagents/<sub>/skills/` — скиллы специфичные для subagent'а (видны только ему)

### 8.1 Формат SKILL.md

```yaml
---
name: attribution
description: |
  Когда использовать этот скилл (читается LLM для прогрессивного раскрытия).
---

# Заголовок скилла
... основная часть body ...
```

Frontmatter `description` идёт в индекс skills (видит main сразу). Body загружается только когда LLM явно прочитает файл через `read_file` или когда `delegate_to_generalist(skills=["attribution", ...])` их туда вложит.

### 8.2 Текущие main-скиллы (`clients/magnetto/skills/`)

| Slug | Про что |
|---|---|
| `anomaly-detection` | аномалии в traffic/direct |
| `attribution` | Markov / Shapley / last-click модели |
| `campaign-analysis` | paid-кампании высокого уровня |
| `cohort-analysis` | когорты клиентов, lead/crm rate |
| `goals-reference` | справочник goal_id ↔ goal_name Метрики |
| `segmentation` | сегменты клиентов для ретаргета |
| `weekly-report` | формат еженедельного отчёта |

### 8.3 Текущие shared-скиллы (видят все)

| Slug | Про что |
|---|---|
| `clickhouse-basics` | диалект CH, nullIf, LowCardinality, Array-функции |
| `python-analysis` | paradigm для python_analysis: parquet → DataFrame → plot |
| `visualization` | matplotlib/seaborn шаблоны, font-DejaVu, русские подписи |

### 8.4 Скиллы subagents

| Subagent | Skills |
|---|---|
| command-center | `command-center-marts`, `command-center-drill`, `command-center-selection` |
| direct-optimizer | `direct-keywords-placements`, `direct-queries`, `direct-performance` |
| scoring-intelligence | `scoring-clients`, `scoring-step-impact`, `scoring-funnel-paths` |

### 8.5 Как добавить новый скилл

Для main-агента:
```
agent/clients/magnetto/skills/<slug>/SKILL.md
```

Для всех (main + subagents):
```
agent/clients/magnetto/shared_skills/<slug>/SKILL.md
```

Только для конкретного subagent:
```
agent/clients/magnetto/subagents/<sub>/skills/<slug>/SKILL.md
```

Body пишется как обычный Markdown с примерами SQL/Python. После создания — `systemctl restart analytics-agent` (индекс строится один раз при старте).

---

## 9. Полный чек-лист «что где лежит»

| Задача | Файл |
|---|---|
| Редактировать роль / правила главного агента | `agent/clients/magnetto/AGENTS.md` |
| Редактировать карту таблиц | `agent/clients/magnetto/data_map.md` |
| Редактировать сегодня / НДС (форматировать) | `agent/core/dynamic_context_middleware.py` |
| Добавить / изменить main-скилл | `agent/clients/magnetto/skills/<slug>/SKILL.md` |
| Добавить / изменить shared-скилл | `agent/clients/magnetto/shared_skills/<slug>/SKILL.md` |
| Добавить / изменить subagent | `agent/clients/magnetto/subagents/<name>/SUBAGENT.md` + его skills/ |
| Поведение кэша | `agent/core/caching_middleware.py` |
| Лимит итераций | `agent/core/budget_middleware.py` (main) + `max_iterations=10` в `BaseSubAgent.__init__` для legacy |
| Enforcement (блок clickhouse без делегации) | `agent/core/enforcement_middleware.py` |
| Ловля захардкоженных DataFrame | `agent/core/enforcement_middleware.py` (HardcodeDetector) |
| Сборка главного графа deepagents | `agent/core/agent_factory.py` |
| Загрузчик SUBAGENT.md | `agent/core/subagent_loader.py` |
| Schema cache (типы колонок из CH) | `agent/core/schema_cache.py` |
| CH-клиент tools (User_magnetto) | `agent/core/tools.py` → `_get_ch_client()` |
| CH-клиент REST endpoints (reports_magnetto) | `agent/api_server.py` → `_get_reports_client()` / `_reports_query_dicts()` |
| generalist-подагент | `agent/core/delegate_to_generalist.py` |
| REST endpoints UI (/api/command_center/*, /api/budget, /api/tables) | `agent/api_server.py` |

---

## 10. Короткая шпаргалка «после правки — что делать»

| Правка | Рестарт сервиса | GRANT | Инвалидация кэша Anthropic |
|---|---|---|---|
| Body существующего SKILL.md | нет | нет | нет (body не в системной части main) |
| AGENTS.md / data_map.md | да | нет | да (system изменился) |
| frontmatter SKILL.md (description) | да | нет | да |
| SUBAGENT.md (body) | да | нет | да (для этого subagent) |
| SUBAGENT.md frontmatter (schema_tables) | да | если таблица новая | да |
| Новая таблица в CH | да | да | да |
| `_dynamic_block()` | да | нет | нет (блок и так вне кэша) |
| Код middleware | да | нет | да (поведение системы изменилось) |

---

## 11. Диагностика

- `journalctl -u analytics-agent -f` — живые логи.
- `GET /api/chat-stats` — сколько job в памяти, сколько ch_queries.
- `GET /debug/sessions` — список последних сессий.
- `GET /debug/session/<session_id>` — полная трассировка turn'ов с event_type/tool_name/content.
- `GET /debug/session/<session_id>/turn/<idx>` — один конкретный turn.
- `GET /debug/stats` — агрегированные счётчики токенов (`total_tokens_est` — это только content в трассе, НЕ фактические LLM-токены).

Реальные LLM-токены (включая cache_write / cache_read) видны только в OpenRouter Activity dashboard.
