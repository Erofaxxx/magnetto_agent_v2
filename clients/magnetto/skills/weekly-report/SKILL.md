---
name: weekly-report
description: |
  еженедельный отчёт, сводка за неделю, итоги периода, дашборд, отчёт за месяц, общая сводка, ключевые метрики за период, weekly report, WoW, week over week, итоговый отчёт
---

## Skill: Еженедельный / периодический отчёт

### Структура отчёта

1. **Ключевые метрики периода** — сводная таблица с WoW/MoM изменениями
2. **Разбивка по каналам** — визиты, выручка, CR по источникам трафика
3. **Топ-кампании** — топ-5 по выручке и ROAS (если есть spend)
4. **Аномалии и сигналы** — флаги отклонений >20% от предыдущего периода
5. **Следующий шаг** — одна строка с рекомендуемым действием

### SQL для WoW сравнения

```sql
WITH текущая AS (
    SELECT
        SUM(visits) AS visits,
        SUM(revenue) AS revenue,
        COUNT(DISTINCT clientID) AS clients
    FROM dm_traffic_performance
    WHERE date >= today() - INTERVAL 7 DAY
      AND date < today()
),
прошлая AS (
    SELECT
        SUM(visits) AS visits,
        SUM(revenue) AS revenue,
        COUNT(DISTINCT clientID) AS clients
    FROM dm_traffic_performance
    WHERE date >= today() - INTERVAL 14 DAY
      AND date < today() - INTERVAL 7 DAY
)
SELECT
    т.visits AS visits_current,
    п.visits AS visits_prev,
    round((т.visits - п.visits) / п.visits * 100, 1) AS visits_wow_pct,
    т.revenue AS revenue_current,
    п.revenue AS revenue_prev,
    round((т.revenue - п.revenue) / п.revenue * 100, 1) AS revenue_wow_pct
FROM текущая т, прошлая п
```

### Форматирование WoW в Python

```python
def fmt_wow(current, prev):
    if prev == 0:
        return "н/д"
    pct = (current - prev) / prev * 100
    arrow = "▲" if pct > 0 else "▼"
    return f"{arrow} {abs(pct):.1f}%"

rows = [
    ["Визиты", f"{visits_cur:,.0f}", f"{visits_prev:,.0f}", fmt_wow(visits_cur, visits_prev)],
    ["Выручка", f"{rev_cur:,.0f} ₽", f"{rev_prev:,.0f} ₽", fmt_wow(rev_cur, rev_prev)],
]
result = "## Отчёт за неделю\n\n"
result += "| Метрика | Текущая | Прошлая | WoW |\n|---|---|---|---|\n"
result += "\n".join(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |" for r in rows)
```

### Флаги для аномалий в отчёте

```python
# Пометить строки с отклонением > 20%:
def wow_flag(pct):
    if abs(pct) > 20:
        return f"⚠️ {pct:+.1f}%"
    return f"{pct:+.1f}%"
```

### Правила составления отчёта

- Явно указывай даты сравниваемых периодов: "7–13 марта vs 28 февраля – 6 марта"
- Не более 3 инсайтов в итоговом блоке — только самые значимые
- Без раздела "Ключевые выводы" если инсайт уже виден в таблице
- Рекомендации — только если есть данные для них (не придумывать)
- В конце отчёта — одна строка "Следующий шаг: X"
