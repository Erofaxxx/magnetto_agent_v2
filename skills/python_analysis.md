## Skill: Анализ данных в Python (Parquet)

### Обязательные правила (нарушение = сломанный код):

1. **df уже загружен** — НЕ вызывай `pd.read_parquet()`. DataFrame готов к использованию.

   ❌ Запрещено:
   ```python
   df2 = pd.read_parquet('/root/.../query_abc.parquet')  # обходит конвертацию типов
   ```
   ✅ Правильно: использовать `df` напрямую. Все трансформации (numpy→list для Array-колонок, авто-приведение типов) уже применены.

### Состояние df не сохраняется между вызовами (КРИТИЧНО)

Каждый вызов python_analysis загружает `df` **заново** из parquet-файла.
Колонки, добавленные в предыдущем вызове, **не сохраняются**.

❌ Неправильно — второй вызов упадёт с KeyError:
```python
# Вызов 1: df['channel'] = df['utm_source'].apply(...)
# Вызов 2: df.groupby('channel')...  ← KeyError: 'channel'
```

✅ Правильно — повторяй все трансформации в каждом вызове:
```python
# Каждый вызов сам добавляет нужные колонки:
df['channel'] = df['utm_source'].astype(str).apply(
    lambda v: 'ya-direct' if v == 'ya-direct' else ('no_utm' if v.strip() == '' else 'other')
)
df.groupby('channel')...  # теперь работает
```
2. **ВСЕГДА устанавливай `result`** — переменная типа Markdown-строка с итоговым выводом.
   ```python
   result = "## Результат\n\n| Метрика | Значение |\n|---|---|\n| Всего | 1 234 |"
   ```
3. **Логирование через print()**:
   ```python
   print("📊 Шаг 1: группировка по кампаниям")
   ```

### Работа с типами данных

Sandbox автоматически конвертирует object-столбцы, но если тип неожиданный:
```python
# Даты:
df['date'] = pd.to_datetime(df['date'], errors='coerce')
# затем: df['date'].dt.year, df['date'].dt.month

# Числа:
df['revenue'] = pd.to_numeric(df['revenue'], errors='coerce')

# Диагностика:
print(df_info)  # словарь {колонка: тип}
```

`col_stats` в ответе clickhouse_query содержит реальные pandas-типы — ориентируйся на них.

### Защита от аномалий и неожиданных данных

Реальные данные всегда содержат выбросы, пустые значения и аномальные строки.
Перед агрегацией и построением графиков — защищай код:

```python
# Отсечение экстремальных выбросов перед визуализацией:
q99 = df['value'].quantile(0.99)
df_clean = df[df['value'] <= q99]
print(f"Отсечено {len(df) - len(df_clean)} строк (> p99={q99:.0f})")

# Деление на ноль — всегда проверяй знаменатель:
df_safe = df[df['visits'] > 0].copy()
df_safe['cr'] = df_safe['orders'] / df_safe['visits']

# Array-колонки могут быть длиной 1 или 5000 — не печатай сырой массив:
# ❌  print(row['channels_path'])   # может вывести тысячи элементов
# ✅  print(row['channels_path'][:5], f"(len={len(row['channels_path'])})")

# Если col_stats показал max_len > 100 для Array-колонки — фильтруй аномалии:
df = df[df['path_length'] < df['path_length'].quantile(0.99)]

# Пустые строки в источниках — заменяй явно, не удаляй:
df['source'] = df['source'].apply(lambda v: v if (v and str(v).strip()) else 'direct')
```

Если результат вычисления выглядит странно (CR > 100%, отрицательная выручка,
медиана = 0 при ненулевом среднем) — добавь диагностический `print` и сообщи в `result`.

### Запрещено

- Вызывать python_analysis только для `df.shape` / `df.dtypes` / `df.head()` — это данные из col_stats.
- Каждый вызов python_analysis должен производить вычисления или строить таблицу для ответа.
- Печатать сырые Array-колонки целиком через `print(df)` или `print(row['arr'])` — используй `.head()`, срез `[:5]`, или `len()`.

### Обработка пропусков

```python
df = df.dropna(subset=['revenue'])   # удалить строки без revenue
df['revenue'] = df['revenue'].fillna(0)  # заменить NaN нулём
```

### Безопасное деление

```python
# Всегда проверяй знаменатель перед делением:
df_safe = df[df['visits'] > 0].copy()
df_safe['cr'] = df_safe['orders'] / df_safe['visits']

# Или через replace:
df['ctr'] = df['clicks'] / df['impressions'].replace(0, np.nan)
```

### Строковые столбцы с NULL

Никогда не пиши `if row['field']` в `.apply()` — сломается на NaN. Безопасный паттерн:
```python
df['label'] = df['utm_campaign'].apply(
    lambda v: str(v) if pd.notna(v) and str(v).strip() else 'unknown'
)
```

### Несмешиваемые треки в dm_campaign_funnel (КРИТИЧНО)

В dm_campaign_funnel два несовместимых трека:
- **Сессионный**: visits → pre_purchase_visits → sessions_with_purchase
- **Клиентский**: unique_clients_pre_purchase → unique_buyers

Делить клиентский трек на сессионный (и наоборот) — НЕЛЬЗЯ.
Результат >100% — маркер этой ошибки, не аномалия данных.

### Форматирование чисел в result

```python
f"{value:,.0f}"   # целые: 1,234,567
f"{value:,.2f}"   # дробные: 12.34
f"{value:.1%}"    # проценты: 12.3%
```

### Ранжирование по среднему / CR

При ранжировании — всегда показывай n (количество заказов/сессий).
Если n < 5 — помечай ⚠️, выводов не строить.
