---
name: attribution
description: |
  атрибуция, data-driven атрибуция, вклад канала, Markov, Shapley, linear attribution, u-shaped, time decay, позиционная атрибуция, какой канал важнее, куда вкладывать бюджет, мультиканальная атрибуция, removal effect, attribution credit, customer journey attribution, какие каналы закрывают сделку, какие каналы открывают, attribution share
---

# Скилл: Data-Driven Атрибуция

Активируется при запросах про: **атрибуция**, вклад канала, data-driven атрибуция, Markov, Shapley,
linear attribution, u-shaped, time decay, позиционная атрибуция, какой канал важнее, куда вкладывать
бюджет, customer journey attribution, какие каналы закрывают сделку, какие каналы открывают,
removal effect, attribution credit, мультиканальная атрибуция.

---

## Доступные модели

| Модель | Витрина | Когда использовать |
|--------|---------|-------------------|
| First Touch / Last Touch | `dm_client_profile` | Быстрое сравнение по клиентам; описано в campaign_analysis |
| Linear | `dm_conversion_paths` | Равное распределение — базовый бенчмарк |
| U-Shaped (Position-Based) | `dm_conversion_paths` | Когда важен и вход, и закрытие |
| Time Decay | `dm_conversion_paths` | Акцент на ближних к лиду касаниях |
| **Markov Chain** | `dm_conversion_paths` | **Основная data-driven модель** — честный вклад каждого канала |

**Spend-данных нет** → CPA и ROAS недоступны.
Нет таблицы `dm_orders` — атрибуция ведётся **по лидам** (`has_lead = 1`), а не по заказам.
Дополнительно можно анализировать клиентов с `has_crm_paid = 1` как "глубокую конверсию".

---

## Данные dm_conversion_paths

- **Конвертировавшие** = `has_lead = 1` (лид, цель 314553735)
- **Неконвертировавшие** = `has_lead = 0` — нужны для Markov (null-пути)
- **Глубокая конверсия** = `has_crm_paid = 1` (CRM оплачен, цель 332069614)
- `channels_path`: значения — `organic`, `ad`, `direct`, `internal`, `referral`, `messenger`, `social`
- `sources_path`: utm_source значения
- Пустую строку `""` в sources_path считать каналом `organic/direct`, не удалять

**Правило выбора колонки:**
- Стратегический вопрос ("какие каналы важнее") → `channels_path`
- Тактический вопрос ("какой источник/кампания") → `sources_path` / `campaigns_path`

---

## Атрибуция через цели CRM (goal-based)

Если нужно понять **какой канал приводит клиентов, дошедших до сделки** — использовать CRM-цели.

### CRM-цели для атрибуции

| Цель | ID | Поле в витринах | Смысл |
|------|----|-----------------|-------|
| CRM: Заказ создан | 332069613 | `has_crm_created`, `goal_332069613` | Лид взят в работу |
| **CRM: Заказ оплачен** | **332069614** | **`has_crm_paid`**, `goal_332069614` | **Финальный KPI — сделка** |
| АМО — Лид квалифицирован | 318012077 | `goal_318012077` | Прошёл квалификацию |
| АМО — Успешно реализовано | 318012287 | `goal_318012287` | Завершён успешно |

⚠️ **Исключать из анализа качества лидов:** goal_405315077 (Спам), goal_405315078 (Отменён), goal_541504123 (Отказ).

### Last-touch атрибуция по CRM-оплатам (dm_client_profile)

```sql
-- Какой канал присутствовал на последнем визите у оплативших клиентов:
SELECT
    last_traffic_source,
    last_utm_source,
    last_utm_campaign,
    count()   AS crm_paid_clients
FROM dm_client_profile
WHERE has_crm_paid = 1
  AND crm_paid_date != '1970-01-01'
GROUP BY last_traffic_source, last_utm_source, last_utm_campaign
ORDER BY crm_paid_clients DESC
LIMIT 30
```

### First-touch атрибуция по CRM-оплатам (dm_client_profile)

```sql
-- Какой канал первым привёл клиентов, которые в итоге оплатили:
SELECT
    first_traffic_source,
    first_utm_source,
    first_utm_campaign,
    count()                                                   AS crm_paid_clients,
    round(avg(days_to_first_lead), 1)                         AS avg_days_to_lead,
    round(avg(days_active), 1)                                AS avg_days_active
FROM dm_client_profile
WHERE has_crm_paid = 1
  AND crm_paid_date != '1970-01-01'
GROUP BY first_traffic_source, first_utm_source, first_utm_campaign
ORDER BY crm_paid_clients DESC
LIMIT 30
```

### Last-touch через конвертирующий визит (dm_client_journey)

```sql
-- Канал на конвертирующем визите (is_converting_visit = 1 = первый визит с лидом):
SELECT
    j.traffic_source,
    j.utm_source,
    j.utm_campaign,
    count()   AS lead_conversions
FROM dm_client_journey j
WHERE j.is_converting_visit = 1
GROUP BY j.traffic_source, j.utm_source, j.utm_campaign
ORDER BY lead_conversions DESC
LIMIT 30

-- То же для CRM-оплат (JOIN с профилем):
SELECT
    j.traffic_source,
    j.utm_source,
    count()   AS crm_paid_clients
FROM dm_client_journey j
JOIN dm_client_profile p ON j.client_id = p.client_id
WHERE j.is_converting_visit = 1
  AND p.has_crm_paid = 1
GROUP BY j.traffic_source, j.utm_source
ORDER BY crm_paid_clients DESC
LIMIT 30
```

### Markov атрибуция по оплатам (dm_conversion_paths)

Для Markov по CRM-оплатам — вместо `has_lead` использовать `has_crm_paid`.
Null-пути: клиенты с `has_crm_paid = 0` (включая тех, кто стал лидом, но не оплатил).

```sql
-- Все has_crm_paid=1 + ~1/5 случайных has_crm_paid=0
-- (оплат меньше, чем лидов — выборка шире для стат. значимости)
SELECT
    client_id,
    has_crm_paid,
    channels_path
FROM dm_conversion_paths
WHERE has_crm_paid = 1
   OR (has_crm_paid = 0 AND rand() % 5 = 0)
```

В Python-коде Markov заменить `has_lead` на `has_crm_paid`:
```python
terminal = CONV if row['has_crm_paid'] == 1 else NULL
```

---

---

## Шаг 1 — SQL-выгрузка

### Для Linear / U-Shape / Time Decay (только конвертировавшие в лид)

```sql
SELECT
    client_id,
    has_lead,
    has_crm_paid,
    path_length,
    channels_path,
    sources_path,
    campaigns_path,
    days_from_first_path
FROM dm_conversion_paths
WHERE has_lead = 1
```

### Для Markov Chain — по каналам (channels_path)

```sql
-- Все has_lead=1 + ~1/13 случайных has_lead=0 (null-пути)
SELECT
    client_id,
    has_lead,
    has_crm_paid,
    channels_path
FROM dm_conversion_paths
WHERE has_lead = 1
   OR (has_lead = 0 AND rand() % 13 = 0)
```

> Выборка 1/13 от неконвертировавших даёт ~29K строк. Итого ~34K строк — достаточно для надёжного Markov.

### Для Markov Chain — по источникам или кампаниям

```sql
-- ВАЖНО: НЕ убирать has_lead=0 — без null-путей base_p = 1.0 (математически неверно)
SELECT
    client_id,
    has_lead,
    has_crm_paid,
    channels_path,
    sources_path,
    campaigns_path
FROM dm_conversion_paths
WHERE has_lead = 1
   OR (has_lead = 0 AND rand() % 13 = 0)
```

---

## Шаг 2 — Python-код для python_analysis

> `df` уже загружен. Всегда устанавливать переменную `result`.
> Вместо `revenue` используем **счёт лидов** как единицу атрибуции.
> Для взвешенной атрибуции можно использовать `has_crm_paid` как вес (1 = ценный клиент).

---

### Алгоритм: Linear Attribution

```python
from collections import defaultdict

credits = defaultdict(float)
total_leads = 0

for _, row in df[df['has_lead'] == 1].iterrows():
    path = list(row['channels_path'])
    if not path:
        continue
    w = 1.0 / len(path)
    for ch in path:
        credits[ch] += w
    total_leads += 1

total_credit = sum(credits.values())
rows = []
for ch, credit in sorted(credits.items(), key=lambda x: -x[1]):
    rows.append(f"| {ch} | {credit:,.1f} | {credit / total_credit:.1%} |")

result = "## Linear Attribution — вклад каналов по лидам\n\n"
result += "| Канал | Attribution Credit | Доля |\n|---|---|---|\n"
result += "\n".join(rows)
result += f"\n\nПокрытие: {total_leads:,} лидов"
```

---

### Алгоритм: U-Shaped (Position-Based) Attribution

```python
from collections import defaultdict

credits = defaultdict(float)

for _, row in df[df['has_lead'] == 1].iterrows():
    path = list(row['channels_path'])
    n = len(path)
    if not path:
        continue
    if n == 1:
        credits[path[0]] += 1.0
    elif n == 2:
        credits[path[0]] += 0.5
        credits[path[1]] += 0.5
    else:
        credits[path[0]] += 0.4     # first touch
        credits[path[-1]] += 0.4   # last touch
        mid_w = 0.2 / (n - 2)
        for ch in path[1:-1]:
            credits[ch] += mid_w

total = sum(credits.values())
rows = []
for ch, credit in sorted(credits.items(), key=lambda x: -x[1]):
    rows.append(f"| {ch} | {credit:,.1f} | {credit / total:.1%} |")

result = "## U-Shaped Attribution — вклад каналов\n\n"
result += "| Канал | Attribution Credit | Доля |\n|---|---|---|\n"
result += "\n".join(rows)
```

---

### Алгоритм: Time Decay Attribution

```python
import numpy as np
from collections import defaultdict

credits = defaultdict(float)

for _, row in df[df['has_lead'] == 1].iterrows():
    path = list(row['channels_path'])
    days = list(row['days_from_first_path'])
    if not path:
        continue
    max_day = max(days) if days else len(path) - 1
    raw_w = np.array([np.exp(-0.5 * (max_day - d)) for d in days], dtype=float)
    if raw_w.sum() == 0:
        raw_w = np.ones(len(path))
    norm_w = raw_w / raw_w.sum()
    for ch, w in zip(path, norm_w):
        credits[ch] += float(w)

total = sum(credits.values())
rows = []
for ch, credit in sorted(credits.items(), key=lambda x: -x[1]):
    rows.append(f"| {ch} | {credit:,.1f} | {credit / total:.1%} |")

result = "## Time Decay Attribution — вклад каналов\n\n"
result += "| Канал | Attribution Credit | Доля |\n|---|---|---|\n"
result += "\n".join(rows)
```

---

### Алгоритм: Markov Chain Attribution (основной data-driven)

> Использовать выборочную SQL-выгрузку выше (~34K строк).

```python
import numpy as np
from collections import defaultdict

START, CONV, NULL = '(start)', '(conversion)', '(null)'

print(f"Строк: {len(df):,} | Лидов: {df['has_lead'].sum():,}")

# 1. Строим пути с терминальными состояниями
paths = []
for _, row in df.iterrows():
    ch = list(row['channels_path'])
    if not ch:
        continue
    terminal = CONV if row['has_lead'] == 1 else NULL
    paths.append([START] + ch + [terminal])

print(f"Путей собрано: {len(paths):,}")

# 2. Подсчёт переходов
trans_counts = defaultdict(lambda: defaultdict(int))
for path in paths:
    for a, b in zip(path[:-1], path[1:]):
        trans_counts[a][b] += 1

# 3. Матрица переходов
states = sorted({s for path in paths for s in path})
idx = {s: i for i, s in enumerate(states)}
n = len(states)

T = np.zeros((n, n))
for fr, to_dict in trans_counts.items():
    total = sum(to_dict.values())
    for to, cnt in to_dict.items():
        T[idx[fr]][idx[to]] = cnt / total

# Поглощающие состояния
for absorbing in [CONV, NULL]:
    if absorbing in idx:
        T[idx[absorbing]] = 0.0
        T[idx[absorbing]][idx[absorbing]] = 1.0

def conv_prob(matrix):
    """Вероятность достичь (conversion) из (start) за 100 шагов."""
    Tp = np.linalg.matrix_power(matrix, 100)
    return float(Tp[idx[START], idx[CONV]])

base_p = conv_prob(T)
print(f"Базовая вероятность конверсии в лид: {base_p:.4f}")

# Защита: base_p ≈ 1.0 означает отсутствие null-путей (has_lead=0)
if base_p >= 0.99:
    result = (
        "⚠️ ОШИБКА ДАННЫХ: base_p = {:.4f}\n\n"
        "В выгрузке отсутствуют пути `has_lead=0` — Markov работает некорректно.\n"
        "Повтори `clickhouse_query` с условием:\n"
        "```sql\nWHERE has_lead = 1\n   OR (has_lead = 0 AND rand() % 13 = 0)\n```"
    ).format(base_p)
else:
    # 4. Removal effect: убираем канал → считаем падение вероятности
    channels = [s for s in states if s not in (START, CONV, NULL)]
    removal = {}
    for ch in channels:
        T_rem = T.copy()
        ci, ni = idx[ch], idx[NULL]
        for i in range(n):
            if T_rem[i][ci] > 0:
                T_rem[i][ni] += T_rem[i][ci]
                T_rem[i][ci] = 0.0
        removal[ch] = max(0.0, base_p - conv_prob(T_rem))

    # 5. Attribution credits (по лидам, не выручке)
    total_removal = sum(removal.values())
    total_leads_conv = int(df['has_lead'].sum())

    if total_removal == 0:
        result = "⚠️ Markov: нулевые removal effects — недостаточно данных."
    else:
        rows = []
        for ch in sorted(removal, key=lambda x: -removal[x]):
            share = removal[ch] / total_removal
            attr_leads = share * total_leads_conv
            re_pct = removal[ch] / base_p * 100
            rows.append(f"| {ch} | {re_pct:.1f}% | {share:.1%} | {attr_leads:,.0f} |")

        result = "## Markov Chain Attribution\n\n"
        result += "| Канал | Removal Effect | Attribution Share | Attributed Leads |\n"
        result += "|---|---|---|---|\n"
        result += "\n".join(rows)
        result += (
            f"\n\n**Removal Effect** — на сколько падает вероятность лида при удалении канала из всех путей.\n"
            f"База: {base_p:.4f} | Лидов: {total_leads_conv:,} | Путей: {len(paths):,}"
        )
```

---

## Сравнительная таблица моделей

```python
# models = {'Linear': {'organic': 0.35, 'ad': 0.28, ...},
#            'U-Shaped': {...}, 'Markov': {...}}

channels_all = sorted({ch for m in models.values() for ch in m})
model_names = list(models.keys())
header = "| Канал | " + " | ".join(model_names) + " |"
sep = "|---|" + "---|" * len(model_names)
rows = [header, sep]
for ch in channels_all:
    vals = [f"{models[m].get(ch, 0):.1%}" for m in model_names]
    rows.append(f"| {ch} | " + " | ".join(vals) + " |")

result = "## Сравнение моделей атрибуции\n\n" + "\n".join(rows)
```

---

## Рекомендации по бюджету (без spend-данных)

Spend в ClickHouse **отсутствует** — точный ROAS недоступен.
Если пользователь просит бюджетные рекомендации:

1. **Дать attribution share** по каналам из Markov
2. **Объяснить**: "Для точного ROAS нужны расходы из Яндекс Директа — подключи данные о spend"
3. **Дать качественный вывод без домыслов:**

| Ситуация | Вывод |
|----------|-------|
| Высокий Markov + низкий First Touch | Канал важен стратегически, но не инициирует первый интерес |
| Высокий First Touch + низкий Markov | "Охватный" канал — инициирует спрос, но не решает исход |
| Высокий Last Touch + низкий Markov | "Закрыватель" — без него лид не состоится, но сам не генерирует спрос |
| Расхождение между моделями > 2× | Сигнал: канал либо сильно недооценён, либо переоценён в простых моделях |

---

## Правила интерпретации

- **n < 100 лидов в сегменте** → не строить Markov, использовать Linear или U-Shaped
- **Removal effect < 1% от base_p** → канал статистически незначим, отметить ⚠️
- **dm_conversion_paths ≠ все клиенты** — только клиенты с отслеживаемым journey; анонимные не входят
- **sources_path содержит `""`** — это organic/direct, не удалять из путей, считать отдельным каналом
- Всегда указывать: покрытие (сколько лидов попало в модель)
- Для анализа "ценных" лидов — фильтровать `has_crm_paid = 1` и строить отдельную атрибуцию
