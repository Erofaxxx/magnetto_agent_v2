# Инструкция по установке на Ubuntu Server

> **Целевой сервер:** `91.218.114.183` / `servermagnetto.asktab.ru`  
> **Репозиторий:** `https://github.com/Erofaxxx/magnetto_agent_v1.git`  
> **Рабочая директория агента:** `/root/clickhouse_analytics_agent`

---

## Требования

- Ubuntu 22.04 / 24.04 LTS
- Root-доступ по SSH
- DNS A-запись `servermagnetto.asktab.ru → 91.218.114.183` уже активна
- Порты 22, 80, 443 открыты в firewall
- Доступ к ClickHouse (self-hosted)
- API ключ OpenRouter (`sk-or-v1-...`)

---

## Шаг 0. Подключение к серверу

```bash
ssh root@91.218.114.183
# или
ssh root@servermagnetto.asktab.ru
```

Проверить, что домен резолвится на нужный IP:
```bash
host servermagnetto.asktab.ru
# Ожидаемый результат: servermagnetto.asktab.ru has address 91.218.114.183
```

---

## Шаг 1. Клонирование репозитория

```bash
# Клонируем репозиторий
git clone https://github.com/Erofaxxx/magnetto_agent_v1.git /root/magnetto_agent_v1

# Копируем папку агента в рабочую директорию
cp -r /root/magnetto_agent_v1/clickhouse_analytics_agent /root/clickhouse_analytics_agent

# Переходим в рабочую директорию
cd /root/clickhouse_analytics_agent
```

Проверка:
```bash
ls /root/clickhouse_analytics_agent
# Ожидаемый результат: agent.py  api_server.py  config.py  requirements.txt  nginx.conf  agent.service  .env.example  ...
```

---

## Шаг 2. Установка системных зависимостей

```bash
apt-get update
apt-get install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx curl wget git
```

Проверка:
```bash
nginx -v
# Ожидаемый результат: nginx version: nginx/1.x.x
python3 --version
# Ожидаемый результат: Python 3.10.x или выше
```

---

## Шаг 3. Python окружение и зависимости

```bash
cd /root/clickhouse_analytics_agent

# Создаём виртуальное окружение
python3 -m venv venv

# Активируем
source venv/bin/activate

# Обновляем pip
pip install --upgrade pip

# Устанавливаем зависимости
pip install -r requirements.txt
```

Проверка:
```bash
python -c "import langgraph, langchain_openai, clickhouse_connect, pandas, matplotlib, fastapi; print('All OK')"
# Ожидаемый результат: All OK
```

---

## Шаг 4. Настройка переменных окружения

```bash
cd /root/clickhouse_analytics_agent
cp .env.example .env
nano .env
```

Заполните файл `.env` следующими значениями:

```env
# ─── OpenRouter API Key ───────────────────────────────────────────────────────
# Получить ключ: https://openrouter.ai/keys
OPENROUTER_API_KEY=sk-or-v1-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

# ─── LLM Model ────────────────────────────────────────────────────────────────
MODEL=anthropic/claude-sonnet-4.6

# ─── ClickHouse Connection ────────────────────────────────────────────────────
# Только хост, без https:// и без слеша в конце
CLICKHOUSE_HOST=your-clickhouse-host.example.com
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=your_user
CLICKHOUSE_PASSWORD=your_password
CLICKHOUSE_DATABASE=your_database

# Self-hosted с Let's Encrypt — оставьте пустым (доверенный CA)
CLICKHOUSE_SSL_CERT_PATH=

# ─── Server ───────────────────────────────────────────────────────────────────
SERVER_URL=https://servermagnetto.asktab.ru
HOST=0.0.0.0
PORT=8000

# ─── Optional tuning ──────────────────────────────────────────────────────────
MAX_TOKENS=8192
MAX_AGENT_ITERATIONS=15
TEMP_FILE_TTL_SECONDS=3600
```

Сохраните файл: `Ctrl+O`, `Enter`, `Ctrl+X`.

Проверка (не должно быть пустых обязательных полей):
```bash
grep -E '^(OPENROUTER_API_KEY|CLICKHOUSE_HOST|CLICKHOUSE_USER|CLICKHOUSE_PASSWORD|CLICKHOUSE_DATABASE)=' /root/clickhouse_analytics_agent/.env
```

---

## Шаг 5. Проверка подключения к ClickHouse

```bash
cd /root/clickhouse_analytics_agent
source venv/bin/activate

python3 -c "
from config import *
import clickhouse_connect

client = clickhouse_connect.get_client(
    host=CLICKHOUSE_HOST,
    port=CLICKHOUSE_PORT,
    username=CLICKHOUSE_USER,
    password=CLICKHOUSE_PASSWORD,
    database=CLICKHOUSE_DATABASE,
    secure=True,
)
print('Connected! Tables:', client.query('SHOW TABLES').result_rows[:5])
"
```

Ожидаемый результат:
```
Connected! Tables: [('table1',), ('table2',), ...]
```

Если ошибка `Connection refused` — проверьте хост и порт:  
```bash
nc -zv your-clickhouse-host.example.com 8443
```

---

## Шаг 6. Настройка Firewall (UFW)

```bash
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP (редирект на HTTPS)
ufw allow 443/tcp   # HTTPS
ufw --force enable
ufw status
```

Ожидаемый результат:
```
Status: active
22/tcp   ALLOW
80/tcp   ALLOW
443/tcp  ALLOW
```

> Порт `8000` **не открывать** публично — uvicorn слушает только через Nginx.

---

## Шаг 7. Настройка systemd (автозапуск сервиса)

```bash
# Копируем unit-файл
cp /root/clickhouse_analytics_agent/agent.service /etc/systemd/system/analytics-agent.service

# Убеждаемся, что пути в unit-файле правильные
grep 'WorkingDirectory\|ExecStart\|EnvironmentFile' /etc/systemd/system/analytics-agent.service
# Ожидаемый результат:
# WorkingDirectory=/root/clickhouse_analytics_agent
# EnvironmentFile=/root/clickhouse_analytics_agent/.env
# ExecStart=/root/clickhouse_analytics_agent/venv/bin/uvicorn ...

# Перезагружаем конфигурацию systemd
systemctl daemon-reload

# Включаем автозапуск при загрузке сервера
systemctl enable analytics-agent

# Запускаем сервис
systemctl start analytics-agent

# Проверяем статус (должен быть active (running))
systemctl status analytics-agent
```

Ожидаемый результат `systemctl status`:
```
● analytics-agent.service - ClickHouse Analytics Agent (LangGraph + FastAPI)
   Loaded: loaded (/etc/systemd/system/analytics-agent.service; enabled)
   Active: active (running) since ...
```

Проверка, что uvicorn слушает порт 8000:
```bash
ss -tlnp | grep 8000
# Ожидаемый результат: LISTEN  0  ...  0.0.0.0:8000
```

Логи в реальном времени:
```bash
journalctl -u analytics-agent -f
```

---

## Шаг 8. Настройка Nginx

```bash
# Копируем конфиг
cp /root/clickhouse_analytics_agent/nginx.conf /etc/nginx/sites-available/analytics-agent

# Проверяем, что домен в конфиге правильный
grep 'server_name' /etc/nginx/sites-available/analytics-agent
# Ожидаемый результат: server_name servermagnetto.asktab.ru;

# Включаем сайт
ln -sf /etc/nginx/sites-available/analytics-agent /etc/nginx/sites-enabled/analytics-agent

# Удаляем дефолтный сайт (если мешает)
rm -f /etc/nginx/sites-enabled/default

# Проверяем конфиг
nginx -t
# Ожидаемый результат: nginx: configuration file /etc/nginx/nginx.conf test is successful

# Перезагружаем Nginx
systemctl reload nginx
```

---

## Шаг 9. Получение HTTPS сертификата (Let's Encrypt)

> ⚠️ DNS-запись `servermagnetto.asktab.ru → 91.218.114.183` должна уже работать и Nginx должен быть запущен!

```bash
# Проверяем доступность домена по HTTP перед запросом сертификата
curl -I http://servermagnetto.asktab.ru
# Ожидаемый результат: HTTP/1.1 301 Moved Permanently

# Получаем сертификат (certbot сам обновит nginx.conf)
certbot --nginx -d servermagnetto.asktab.ru
```

Certbot попросит email (для уведомлений об истечении) и согласие с условиями.

Проверка автообновления:
```bash
certbot renew --dry-run
# Ожидаемый результат: Congratulations, all simulated renewals succeeded
```

После получения сертификата перезапустите nginx:
```bash
systemctl reload nginx
```

---

## Шаг 10. Проверка работы API

```bash
# Health check
curl https://servermagnetto.asktab.ru/health
# Ожидаемый результат: {"status":"ok", ...}

# Информация об API
curl https://servermagnetto.asktab.ru/api/info

# Тестовый запрос к агенту
curl -X POST https://servermagnetto.asktab.ru/api/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Привет! Какие таблицы есть в базе данных?",
    "session_id": "test-session-001"
  }'
```

Документация API (Swagger UI):
```
https://servermagnetto.asktab.ru/docs
```

---

## Автоматическая установка (альтернатива шагам 2–7)

Вместо ручного выполнения шагов 2–7 можно запустить скрипт `setup.sh`:

```bash
# После клонирования репозитория (шаг 1) и перед настройкой .env (шаг 4)
cd /root/clickhouse_analytics_agent
chmod +x setup.sh
bash setup.sh
```

Скрипт автоматически:
- Установит системные пакеты
- Создаст Python venv и установит зависимости
- Создаст `.env` из `.env.example`
- Установит и включит systemd-сервис
- Настроит Nginx

После `setup.sh` вернитесь к **Шагу 4** для заполнения `.env`, затем выполните шаги 9–10.

---

## Управление сервисом

| Команда | Описание |
|---------|----------|
| `systemctl start analytics-agent` | Запустить |
| `systemctl stop analytics-agent` | Остановить |
| `systemctl restart analytics-agent` | Перезапустить |
| `systemctl status analytics-agent` | Статус |
| `journalctl -u analytics-agent -f` | Логи в реальном времени |
| `journalctl -u analytics-agent --since "1 hour ago"` | Логи за последний час |

---

## Обновление агента

```bash
cd /root/magnetto_agent_v1
git pull origin main

# Обновляем файлы агента
cp -r clickhouse_analytics_agent/* /root/clickhouse_analytics_agent/

# Обновляем зависимости если нужно
source /root/clickhouse_analytics_agent/venv/bin/activate
pip install -r /root/clickhouse_analytics_agent/requirements.txt

# Перезапускаем сервис
systemctl restart analytics-agent
systemctl status analytics-agent
```

---

## Структура файлов

```
/root/clickhouse_analytics_agent/
├── .env                     ← ваши секреты (не в git!)
├── .env.example             ← шаблон
├── config.py                ← загрузка конфигурации
├── clickhouse_client.py     ← подключение к ClickHouse + выгрузка Parquet
├── python_sandbox.py        ← выполнение Python кода, захват графиков
├── tools.py                 ← LangGraph инструменты
├── agent.py                 ← LangGraph агент + SqliteSaver
├── router.py                ← роутер запросов
├── segment_agent.py         ← агент сегментации
├── segment_store.py         ← хранилище сегментов
├── api_server.py            ← FastAPI сервер
├── chat_logger.py           ← логирование чатов
├── requirements.txt         ← зависимости Python
├── agent.service            ← systemd unit
├── nginx.conf               ← конфиг Nginx
├── setup.sh                 ← скрипт автоустановки
├── chat_history.db          ← SQLite (создаётся автоматически)
└── temp_data/               ← временные parquet файлы (автоочистка)
```

---

## Диагностика проблем

### Сервис не запускается
```bash
journalctl -u analytics-agent -n 50 --no-pager
```
Обычные причины: неверный `.env`, ошибка импорта Python, порт 8000 занят другим процессом.

Проверить порт:
```bash
ss -tlnp | grep 8000
# Если порт занят — найти процесс: fuser 8000/tcp
```

### Ошибка подключения к ClickHouse
```bash
# Проверьте доступность хоста и порта
nc -zv your-clickhouse-host.example.com 8443
```

### 502 Bad Gateway в Nginx
```bash
# Проверьте, запущен ли uvicorn
systemctl status analytics-agent

# Проверьте, слушает ли порт 8000
ss -tlnp | grep 8000

# Смотрите ошибки nginx
tail -n 50 /var/log/nginx/analytics_agent_error.log
```

### Ошибка SSL сертификата Let's Encrypt при certbot
```bash
# Убедитесь, что nginx запущен и порт 80 открыт
systemctl status nginx
curl -I http://servermagnetto.asktab.ru

# Проверьте DNS propagation
nslookup servermagnetto.asktab.ru
# Должно вернуть 91.218.114.183
```

### Медленные ответы (504 Gateway Timeout)
- Нормальное время ответа агента: 15–60 секунд
- `proxy_read_timeout` в `nginx.conf` уже установлен на 600 секунд
- Если проблема остаётся — проверьте `journalctl -u analytics-agent -f` на ошибки
