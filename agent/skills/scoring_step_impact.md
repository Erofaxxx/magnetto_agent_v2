# Lift-анализ целей по шагам визитов

## Таблица magnetto.dm_step_goal_impact

Фундамент скоринга. Для каждой пары (номер визита × цель Метрики) вычисляет, насколько выполнение цели повышает вероятность CRM-сделки. 234 строки (10 шагов × ~24 значимые цели, минимум 20 клиентов на комбинацию).

**Поля**: visit_number (1-10), goal_id, goal_name, clients_at_step, clients_with_goal, clients_without_goal, converters_with_goal, converters_without_goal, rate_with_goal, rate_without_goal, lift, snapshot_date.

## Как работает lift

```
rate_with_goal    = converters_with_goal / clients_with_goal
rate_without_goal = converters_without_goal / clients_without_goal
lift              = rate_with_goal / rate_without_goal
```

**Lift = 284**: клиент, выполнивший цель, конвертируется в 284 раза чаще. Базовая конверсия ~0.09%.

## Интерпретация lift

| Диапазон | Значение | Пример |
|----------|----------|--------|
| > 100 | Почти гарантия | CRM Заказ создан (1798) — тавтология |
| 50-100 | Сильный сигнал | Уникальный звонок (66), Клик по телефону (64) |
| 10-50 | Умеренный | Просмотр квартир (~20), Квиз (19.6) |
| 1-10 | Слабый | Переход в соцсеть |
| < 1 | Негативная корреляция | Ассоциация с НЕконверсией |

**Тавтологии** (исключать из рекомендаций): CRM Заказ создан (332069613), CRM Заказ оплачен (332069614) — срабатывают ПОСЛЕ конверсии.
**Мусор** (исключать): Спам (402733217), CRM Отказ (405315077, 405315078), Мусорный трафик (407450615), CRM Статус изменён (541504123).

## Реально полезные цели (для рекомендаций)

**Шаг 1 (первый визит):**
- Отправка формы ипотека → lift 284
- Отправил контактные данные → lift 165
- Заполнил контактные данные → lift 157
- Все лиды magnetto → lift 124
- Уникально-целевой звонок → lift 69
- Клик по телефону Magnetto → lift 64

**Шаги 2-5:** те же цели, lift снижается (379→65 → 239→24). Добавляется "Просмотр квартир" (lift ~20).
**Шаги 7-10:** lift всех целей < 35. Самые значимые: Все лиды, Автоцель: отправка формы, Клик по телефону.

## SQL-шаблоны

### Какие цели стимулировать в рекламе
```sql
SELECT goal_name, visit_number, lift, clients_with_goal, converters_with_goal
FROM magnetto.dm_step_goal_impact
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_step_goal_impact)
  AND lift > 10
  AND goal_id NOT IN (332069613, 332069614, 402733217, 405315077, 405315078, 407450615, 541504123)
ORDER BY lift DESC
```

### Работает ли конкретный инструмент (квиз, чат, jivo)
```sql
SELECT visit_number, goal_name, lift, clients_with_goal, converters_with_goal
FROM magnetto.dm_step_goal_impact
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_step_goal_impact)
  AND goal_name LIKE '%квиз%'  -- или '%Jivo%', '%чат%'
ORDER BY visit_number
```

### На каком шаге клиент "дозревает"
```sql
SELECT visit_number,
       max(lift) AS max_lift,
       argMax(goal_name, lift) AS strongest_goal,
       sum(converters_with_goal) AS total_converters
FROM magnetto.dm_step_goal_impact
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_step_goal_impact)
  AND goal_id NOT IN (332069613, 332069614, 402733217, 405315077, 405315078, 407450615, 541504123)
GROUP BY visit_number
ORDER BY visit_number
```

### Сравнение двух целей
```sql
SELECT visit_number, goal_name, lift, clients_with_goal
FROM magnetto.dm_step_goal_impact
WHERE snapshot_date = (SELECT max(snapshot_date) FROM magnetto.dm_step_goal_impact)
  AND goal_name IN ('Отправка формы телефон', 'Уникальный звонок')
ORDER BY goal_name, visit_number
```
