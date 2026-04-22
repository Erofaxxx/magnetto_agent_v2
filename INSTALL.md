# ClickHouse Analytics Agent — установка и запуск

Агент на `deepagents` + FastAPI. Пишет SQL к ClickHouse, запускает Python-песочницу,
делегирует задачи специализированным субагентам (direct-optimizer, scoring-intelligence).
Работает как HTTP API (`uvicorn api_server:app`, порт 8000).

## 1. Требования

- Ubuntu 22.04+
- Python 3.12
- ClickHouse (внешний, доступный по сети)
- OpenRouter API key

```bash
apt update
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.12 python3.12-venv python3.12-dev
python3.12 --version
```

## 2. Клонирование и установка зависимостей

```bash
git clone https://github.com/Erofaxxx/magnetto_agent_v2.git /root/clickhouse_analytics_agent
cd /root/clickhouse_analytics_agent
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
```

## 3. Конфигурация `.env`

Скопируйте шаблон и заполните:
```bash
cp .env.example .env
```

Минимальный набор переменных:
```
OPENROUTER_API_KEY=sk-or-v1-...
CLICKHOUSE_HOST=...
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=...
CLICKHOUSE_PASSWORD=...
CLICKHOUSE_DATABASE=...
MODEL=anthropic/claude-sonnet-4.5
USE_DEEPAGENTS=1
MAX_AGENT_ITERATIONS=30
```

`USE_DEEPAGENTS=1` включает текущее ядро (`core/`). `USE_DEEPAGENTS=0` — откат
на legacy-маршрутизатор (`agent.py`/`router.py`).

## 4. Структура клиента

Всё, что относится к конкретному клиенту (Magnetto), лежит в `clients/magnetto/`:

```
clients/magnetto/
├── AGENTS.md                     # identity главного агента (в system prompt)
├── data_map.md                   # карта таблиц ClickHouse (в system prompt)
├── shared_skills/                # навыки, доступные всем субагентам
│   ├── clickhouse-basics/SKILL.md
│   ├── python-analysis/SKILL.md
│   └── visualization/SKILL.md
├── skills/                       # навыки главного (progressive disclosure)
│   └── <slug>/SKILL.md
└── subagents/
    ├── direct-optimizer/{SUBAGENT.md, skills/...}
    └── scoring-intelligence/{SUBAGENT.md, skills/...}
```

## 5. Запуск как systemd-сервис

```bash
cp agent.service /etc/systemd/system/analytics-agent.service
systemctl daemon-reload
systemctl enable --now analytics-agent
systemctl status analytics-agent --no-pager
journalctl -u analytics-agent -f
```

Ожидаемые строки при старте:
```
✅ SchemaCache loaded: N tables
✅ Loaded subagent: direct-optimizer ...
✅ Loaded subagent: scoring-intelligence ...
✅ deepagents main agent ready ...
✅ ClickHouse Analytics Agent API started | http://0.0.0.0:8000
```

## 6. Проверка

```bash
curl -X POST http://localhost:8000/api/analyze \
  -H "Content-Type: application/json" \
  -d '{"query": "Сколько клиентов с has_lead=1?"}'
# → {"job_id": "...", "session_id": "...", "status": "pending", ...}

curl http://localhost:8000/api/job/<job_id>
```

## 7. Обновление на сервере

```bash
cd /root/clickhouse_analytics_agent
git pull origin main
source venv/bin/activate
pip install -r requirements.txt   # если менялись зависимости
deactivate
systemctl restart analytics-agent
```

## 8. Сессионные файлы

Агент пишет parquet/plots в `temp_data/sessions/<session_id>/` — сохраняется
между turn'ами одного чата, изолировано между разными `session_id`.

Скачать файл: `GET /api/session/<session_id>/file?path=/plots/xxx.png`
Список файлов: `GET /api/session/<session_id>/files`

Чистка старых сессий:
```bash
find temp_data/sessions -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
```

## 9. Добавление нового функционала

**Новый skill:** создать `clients/magnetto/skills/<slug>/SKILL.md` с
frontmatter (name, description). Перезагрузка сервиса не обязательна —
`FilesystemBackend` читает файл при матчинге description.

**Новый субагент:** `clients/magnetto/subagents/<name>/SUBAGENT.md`
(frontmatter: name, description, schema_tables, model). `systemctl restart analytics-agent`.

**Новая таблица:** строка в `data_map.md` + (опционально) `schema_tables`
в соответствующем `SUBAGENT.md`. Рестарт сервиса.

**Новый клиент:** скопировать `clients/magnetto/` → `clients/<new>/`,
отредактировать AGENTS.md, data_map.md, субагентов. В `api_server.py` можно
прокидывать `client_id` из query/JWT/header.
