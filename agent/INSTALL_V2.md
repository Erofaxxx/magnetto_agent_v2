# Deployment: deepagents agent v2 (USE_DEEPAGENTS=1)

Рабочий процесс — стареют код и v1 venv остаются, v2 добавляется **рядом**.
Откат: `USE_DEEPAGENTS=0` в `.env` + `systemctl restart analytics-agent`.

## 1. Установка Python 3.12 (если ещё нет)

Ubuntu 22.04:
```bash
apt update
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.12 python3.12-venv python3.12-dev
python3.12 --version    # ≥ 3.12
```

## 2. Создание venv_v2 рядом со старым

```bash
cd /root/clickhouse_analytics_agent
python3.12 -m venv venv_v2
source venv_v2/bin/activate
pip install --upgrade pip
pip install -r requirements_v2.txt
deactivate
```

## 3. Инициализация структуры для Magnetto

Папка `clients/magnetto/` — новая часть кода в репозитории; скопируется вместе
с остальными файлами при обычном `rsync`. Проверить:

```bash
ls clients/magnetto/
# AGENTS.md  data_map.md  shared_skills  skills  subagents
```

## 4. Переменная окружения

В `/root/clickhouse_analytics_agent/.env` добавить:
```
USE_DEEPAGENTS=1
MAX_AGENT_ITERATIONS=30     # 30 итераций суммарно (main + subagents)
```
Остальные `OPENROUTER_API_KEY`, `CLICKHOUSE_*`, `MODEL` — как были.

## 5. Обновление systemd unit

Если старый unit использует `venv/bin/python` — нужно переключить на
`venv_v2/bin/python`. Проверить:

```bash
cat /etc/systemd/system/analytics-agent.service
# ExecStart=/root/clickhouse_analytics_agent/venv/bin/uvicorn ...
```

Отредактировать на `venv_v2/bin/uvicorn`:
```bash
sed -i 's|venv/bin/uvicorn|venv_v2/bin/uvicorn|g' /etc/systemd/system/analytics-agent.service
systemctl daemon-reload
systemctl restart analytics-agent
```

## 6. Проверка после рестарта

```bash
systemctl status analytics-agent --no-pager
journalctl -u analytics-agent -n 50 --no-pager | grep -E "deepagents|SchemaCache|subagent"
# Expected:
#   ✅ SchemaCache loaded: 17 tables
#   ✅ Loaded subagent: direct-optimizer (tables: 7, skills_paths: 2)
#   ✅ Loaded subagent: scoring-intelligence (tables: 5, skills_paths: 2)
#   ✅ deepagents main agent ready ...
#   ✅ deepagents v2 ready (USE_DEEPAGENTS=1)
```

Быстрый тест:
```bash
curl -X POST http://localhost:8000/api/analyze \
  -H "Content-Type: application/json" \
  -d '{"query": "Сколько клиентов с has_lead=1?"}'
# → {"job_id": "...", "session_id": "...", "status": "pending", ...}

# Poll:
curl http://localhost:8000/api/job/<job_id>
```

## 7. Откат

```bash
# .env:
USE_DEEPAGENTS=0
# или:
sed -i '/^USE_DEEPAGENTS=/d' /root/clickhouse_analytics_agent/.env

# systemd unit (откат на старый venv):
sed -i 's|venv_v2/bin/uvicorn|venv/bin/uvicorn|g' /etc/systemd/system/analytics-agent.service
systemctl daemon-reload
systemctl restart analytics-agent
```

## 8. Чистка сессионных файлов

Новый агент хранит парquet/plots в `temp_data/sessions/<session_id>/`.
Старый cleanup-loop в api_server.py очищает только `TEMP_DIR/*.parquet` по TTL.
Для новых папок потребуется либо расширить cleanup, либо вручную:

```bash
# удалить сессии старше 7 дней
find temp_data/sessions -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
```

## 9. Сессионная память между turn'ами

- Parquet, графики, заметки живут в `temp_data/sessions/<session_id>/` и
  **не теряются** между вопросами в одном чате.
- Между разными чатами (разные `session_id`) — полная изоляция.
- Скачать файл фронтенду: `GET /api/session/<session_id>/file?path=/plots/xxx.png`
- Список файлов: `GET /api/session/<session_id>/files`

## 10. Структура новой части

```
clients/magnetto/
├── AGENTS.md                     # identity главного агента (всегда в system prompt)
├── data_map.md                   # карта 17 таблиц с маркерами ⚠ (всегда в prompt)
├── shared_skills/                # общие навыки (доступны всем субагентам)
│   ├── clickhouse-basics/SKILL.md
│   ├── python-analysis/SKILL.md
│   └── visualization/SKILL.md
├── skills/                       # доменные навыки главного (progressive disclosure)
│   ├── attribution/SKILL.md
│   ├── campaign-analysis/SKILL.md
│   ├── cohort-analysis/SKILL.md
│   ├── goals-reference/SKILL.md
│   └── ...
└── subagents/
    ├── direct-optimizer/
    │   ├── SUBAGENT.md
    │   └── skills/{direct-*}/SKILL.md
    └── scoring-intelligence/
        ├── SUBAGENT.md
        └── skills/{scoring-*}/SKILL.md

core_v2/
├── agent_factory.py              # build_agent(session_id, client_id)
├── api_adapter.py                # drop-in для api_server.py
├── schema_cache.py               # singleton per-process
├── session_context.py            # ContextVar для session_id
├── session_backend.py            # per-session CompositeBackend
├── tools.py                      # clickhouse_query, python_analysis, think_tool, list_tables
├── caching_middleware.py         # cache_control на system/last-tool/last-human
├── budget_middleware.py          # 30 iterations total
├── subagent_loader.py            # parse SUBAGENT.md + tables.md
└── delegate_to_generalist.py     # main-tool: делегирование универсальному
```

## 11. Адаптивность: как добавить новое

**Новый skill:** `clients/magnetto/skills/<slug>/SKILL.md` с frontmatter
(name, description). Перезагружать агент не обязательно — `FilesystemBackend`
читает файл при matching description.

**Новый специализированный subagent:** создать `clients/magnetto/subagents/<name>/`
со `SUBAGENT.md` (frontmatter: name, description, schema_tables, model).
Перезапустить сервис (`systemctl restart analytics-agent`).

**Новая таблица:** добавить строку в `data_map.md` + (опционально)
`schema_tables` в существующем `SUBAGENT.md`. Перезапустить сервис.

**Новый клиент:** скопировать `clients/magnetto/` → `clients/<new>/`,
отредактировать AGENTS.md + data_map.md + subagents. В `api_server.py`
можно прокидывать `client_id` из query / JWT / header.
