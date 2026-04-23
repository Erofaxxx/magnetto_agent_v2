# Segment Builder API — Frontend Reference

Base URL: `https://your-server/api`

## Изоляция по пользователям

Все запросы к segment API принимают заголовок **`X-User-Id`** (строка — любой уникальный идентификатор пользователя из вашей auth-системы: email, user_id, UUID).

- Каждый пользователь видит **только свои** сегменты
- Удалить можно только свой сегмент (чужой вернёт 404)
- Без заголовка — сегменты попадают в общее пространство `__shared__`

```
X-User-Id: user@example.com
```

---

## 1. Чат с агентом-сегментатором

### `POST /api/segment/chat`

Один ход диалога. Сохраняй `session_id` между вызовами.

**Headers**
```
Content-Type: application/json
X-User-Id: user@example.com
```

**Request**
```json
{
  "message": "Хочу создать сегмент тёплых лидов из Яндекс.Директа",
  "session_id": "uuid-optional"
}
```

**Response**
```json
{
  "success": true,
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "text_output": "Понял! За какой период смотреть — последние 30 дней или конкретный диапазон?",
  "segment_saved": false,
  "error": null
}
```

| Поле | Тип | Описание |
|---|---|---|
| `session_id` | string | Передавай в каждом следующем сообщении |
| `text_output` | string | Ответ агента (Markdown) |
| `segment_saved` | bool | `true` если в этом ходу был сохранён сегмент |
| `error` | string\|null | Сообщение об ошибке |

**Типичный flow диалога (5–8 ходов):**

```
→ "Хочу сегмент тёплых лидов из директа"
← "За какой период?"
→ "Последние 30 дней"
← "Сколько визитов минимум?"
→ "2 и больше, без покупок"
← "**Сегмент: Тёплые лиды Direct**\n...SQL...\nРазмер: ~8 240. Сохранить?"
→ "Да"
← "Сегмент сохранён ✓"   ← segment_saved: true
```

---

### `GET /api/segment/chat/{session_id}/history`

История диалога сессии (для восстановления чата после перезагрузки страницы).
Заголовок `X-User-Id` не требуется — история хранится по session_id.

**Response**
```json
{
  "session_id": "550e8400-...",
  "history": [
    {"role": "user", "content": "Хочу сегмент тёплых лидов"},
    {"role": "assistant", "content": "За какой период?"},
    {"role": "user", "content": "Последние 30 дней"}
  ]
}
```

---

## 2. Управление сохранёнными сегментами

Все endpoints требуют заголовок `X-User-Id`.

### `GET /api/segments`

Список сегментов текущего пользователя (сортировка по дате обновления — новые первыми).

**Headers:** `X-User-Id: user@example.com`

**Response**
```json
{
  "segments": [
    {
      "segment_id": "seg_7a3f1c",
      "name": "Тёплые лиды Direct",
      "description": "Пользователи из ya-direct, 2+ визита, без покупок, 30 дней",
      "approach": "funnel_behavioral",
      "owner": "user@example.com",
      "period": {"type": "rolling", "days": 30},
      "conditions": {
        "rfm": {"frequency_min": 2, "has_purchased": false},
        "traffic": {"attribution_type": "first_touch", "utm_source": ["ya-direct"]}
      },
      "primary_table": "dm_client_profile",
      "join_tables": [],
      "sql_query": "SELECT DISTINCT client_id FROM dm_client_profile WHERE ...",
      "last_count": 8240,
      "last_materialized": "2026-03-09",
      "used_in": [],
      "created_at": "2026-03-09",
      "updated_at": "2026-03-09"
    }
  ]
}
```

**Ключевые поля для отображения в UI:**

| Поле | Где показывать |
|---|---|
| `name` | Заголовок карточки |
| `description` | Подзаголовок |
| `last_count` | "~8 240 пользователей" |
| `last_materialized` | "Актуально на 09.03.2026" |
| `approach` | Тег/бейдж (rfm, канальный, когортный…) |
| `sql_query` | Кнопка "Скопировать SQL" |

---

### `GET /api/segments/{segment_id}`

Полный объект одного сегмента. Возвращает 404 если сегмент не найден или принадлежит другому пользователю.

**Headers:** `X-User-Id: user@example.com`

**Response** — тот же объект сегмента без обёртки `segments: [...]`.

---

### `DELETE /api/segments/{segment_id}`

Удалить сегмент. Возвращает 404 если сегмент не найден или чужой.

**Headers:** `X-User-Id: user@example.com`

**Response**
```json
{"success": true}
```

---

## 3. Пример интеграции (псевдокод)

```typescript
const userId = getCurrentUser().email;  // или id из вашей auth

const authHeaders = {
  'Content-Type': 'application/json',
  'X-User-Id': userId,
};

// ── Чат сегментации ─────────────────────────────────────────────────

const [sessionId, setSessionId] = useState<string | null>(null);
const [messages, setMessages] = useState<Message[]>([]);

async function sendMessage(text: string) {
  const res = await fetch('/api/segment/chat', {
    method: 'POST',
    headers: authHeaders,
    body: JSON.stringify({message: text, session_id: sessionId}),
  });
  const data = await res.json();

  setSessionId(data.session_id);  // сохранить для следующего запроса
  setMessages(prev => [
    ...prev,
    {role: 'user', content: text},
    {role: 'assistant', content: data.text_output},
  ]);

  if (data.segment_saved) {
    // показать уведомление + обновить список сегментов
    refreshSegmentList();
  }
}

// Восстановить историю после перезагрузки страницы
async function restoreHistory(sessionId: string) {
  const res = await fetch(`/api/segment/chat/${sessionId}/history`);
  const {history} = await res.json();
  setMessages(history);
}

// ── Список сегментов ─────────────────────────────────────────────────

async function loadSegments() {
  const res = await fetch('/api/segments', {headers: authHeaders});
  const {segments} = await res.json();
  return segments;  // только сегменты текущего пользователя
}

async function deleteSegment(segmentId: string) {
  await fetch(`/api/segments/${segmentId}`, {
    method: 'DELETE',
    headers: authHeaders,
  });
  refreshSegmentList();
}
```
