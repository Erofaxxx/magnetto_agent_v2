# Инструкция по интеграции Frontend (Lovable) с Analytics Agent API

## Базовый URL

```
https://server.asktab.ru
```

---

## API Endpoints

| Метод | URL | Описание |
|-------|-----|----------|
| `GET` | `/health` | Проверка работоспособности |
| `GET` | `/api/info` | Информация о сервисе |
| `POST` | `/api/session/new` | Создать новую сессию |
| `GET` | `/api/session/{id}` | Данные сессии |
| `POST` | `/api/analyze` | **Главный endpoint — отправить вопрос** |

---

## Концепция сессий

Каждый пользователь имеет свой `session_id`. Агент хранит историю переписки в SQLite,
поэтому каждый последующий вопрос в той же сессии учитывает контекст предыдущих.

**Важно:** `session_id` — это просто UUID-строка. Храните её в localStorage браузера.

---

## Пример полной интеграции (JavaScript / TypeScript)

### 1. Utility функции (`lib/analytics-api.ts`)

```typescript
const API_BASE = "https://server.asktab.ru";

// Типы данных
export interface AnalyzeRequest {
  query: string;
  session_id?: string;
}

export interface AnalyzeResponse {
  success: boolean;
  session_id: string;
  text_output: string;        // Markdown текст от агента
  plots: string[];            // base64 PNG: "data:image/png;base64,..."
  tool_calls: ToolCall[];     // Лог вызовов инструментов
  error: string | null;
  timestamp: string;
}

export interface ToolCall {
  tool: string;    // "list_tables" | "clickhouse_query" | "python_analysis"
  input: Record<string, unknown>;
}

// Получить или создать session_id для пользователя
export function getOrCreateSessionId(): string {
  const key = "analytics_session_id";
  let sessionId = localStorage.getItem(key);
  if (!sessionId) {
    sessionId = crypto.randomUUID();
    localStorage.setItem(key, sessionId);
  }
  return sessionId;
}

// Создать новую сессию (например, кнопка "Новый чат")
export async function createNewSession(): Promise<string> {
  const res = await fetch(`${API_BASE}/api/session/new`, { method: "POST" });
  const data = await res.json();
  localStorage.setItem("analytics_session_id", data.session_id);
  return data.session_id;
}

// Отправить вопрос агенту
export async function analyzeQuery(
  query: string,
  sessionId?: string
): Promise<AnalyzeResponse> {
  const session_id = sessionId || getOrCreateSessionId();

  const response = await fetch(`${API_BASE}/api/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, session_id }),
    // Важно: агент может работать 30–120 секунд
    // Не устанавливайте таймаут меньше 3 минут
  });

  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.detail || err.error || "API error");
  }

  return response.json();
}

// Health check
export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/health`);
    const data = await res.json();
    return data.status === "healthy";
  } catch {
    return false;
  }
}
```

---

### 2. React хук для чата (`hooks/useAnalyticsChat.ts`)

```typescript
import { useState, useCallback, useRef } from "react";
import { analyzeQuery, getOrCreateSessionId, createNewSession, AnalyzeResponse } from "@/lib/analytics-api";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;       // Markdown текст
  plots: string[];       // base64 PNG data URIs
  tool_calls: any[];     // инструменты вызванные агентом
  timestamp: Date;
  isLoading?: boolean;
}

export function useAnalyticsChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessionIdRef = useRef<string>(getOrCreateSessionId());

  const sendMessage = useCallback(async (query: string) => {
    if (!query.trim() || isLoading) return;

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: query,
      plots: [],
      tool_calls: [],
      timestamp: new Date(),
    };

    // Добавляем сообщение пользователя + placeholder для ответа
    const loadingMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      plots: [],
      tool_calls: [],
      timestamp: new Date(),
      isLoading: true,
    };

    setMessages(prev => [...prev, userMessage, loadingMessage]);
    setIsLoading(true);
    setError(null);

    try {
      const result: AnalyzeResponse = await analyzeQuery(query, sessionIdRef.current);

      // Обновляем session_id если сервер вернул новый
      if (result.session_id) {
        sessionIdRef.current = result.session_id;
        localStorage.setItem("analytics_session_id", result.session_id);
      }

      // Заменяем loading placeholder на реальный ответ
      setMessages(prev => [
        ...prev.slice(0, -1), // убираем loading
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: result.text_output,
          plots: result.plots,
          tool_calls: result.tool_calls,
          timestamp: new Date(),
        },
      ]);

    } catch (err: any) {
      setError(err.message || "Ошибка запроса к API");
      // Убираем loading placeholder при ошибке
      setMessages(prev => prev.slice(0, -1));
    } finally {
      setIsLoading(false);
    }
  }, [isLoading]);

  const startNewChat = useCallback(async () => {
    const newSessionId = await createNewSession();
    sessionIdRef.current = newSessionId;
    setMessages([]);
    setError(null);
  }, []);

  return {
    messages,
    isLoading,
    error,
    sessionId: sessionIdRef.current,
    sendMessage,
    startNewChat,
  };
}
```

---

### 3. Компонент чата (`components/AnalyticsChat.tsx`)

```tsx
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { useAnalyticsChat } from "@/hooks/useAnalyticsChat";

export function AnalyticsChat() {
  const { messages, isLoading, error, sendMessage, startNewChat, sessionId } = useAnalyticsChat();
  const [input, setInput] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim()) return;
    sendMessage(input.trim());
    setInput("");
  };

  return (
    <div className="flex flex-col h-screen max-w-4xl mx-auto p-4">
      {/* Header */}
      <div className="flex justify-between items-center mb-4">
        <h1 className="text-xl font-bold">📊 Analytics Agent</h1>
        <button
          onClick={startNewChat}
          className="text-sm px-3 py-1 rounded border hover:bg-gray-100"
        >
          + Новый чат
        </button>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto space-y-4 mb-4">
        {messages.length === 0 && (
          <div className="text-center text-gray-400 mt-20">
            <p className="text-lg">Привет! Я аналитик рекламных данных.</p>
            <p className="text-sm mt-2">Спросите меня о ваших кампаниях, метриках или трендах.</p>
          </div>
        )}

        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}

        {isLoading && (
          <div className="flex items-center space-x-2 text-gray-500">
            <LoadingSpinner />
            <span className="text-sm">Агент анализирует данные...</span>
          </div>
        )}

        {error && (
          <div className="bg-red-50 border border-red-200 rounded p-3 text-red-700 text-sm">
            ⚠️ {error}
          </div>
        )}
      </div>

      {/* Input */}
      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Спросите о данных... (напр. CTR по кампаниям за январь)"
          disabled={isLoading}
          className="flex-1 border rounded-lg px-4 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <button
          type="submit"
          disabled={isLoading || !input.trim()}
          className="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
        >
          Отправить
        </button>
      </form>

      {/* Session info */}
      <div className="mt-2 text-xs text-gray-300 text-right">
        Session: {sessionId.slice(0, 8)}...
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: any }) {
  if (message.isLoading) {
    return (
      <div className="flex justify-start">
        <div className="bg-gray-100 rounded-lg p-3 max-w-2xl animate-pulse">
          <div className="h-4 bg-gray-300 rounded w-48" />
        </div>
      </div>
    );
  }

  return (
    <div className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}>
      <div
        className={`rounded-lg p-4 max-w-3xl ${
          message.role === "user"
            ? "bg-blue-600 text-white"
            : "bg-gray-50 border"
        }`}
      >
        {/* Text (Markdown) */}
        {message.content && (
          <div className="prose prose-sm max-w-none">
            <ReactMarkdown>{message.content}</ReactMarkdown>
          </div>
        )}

        {/* Charts */}
        {message.plots?.length > 0 && (
          <div className="mt-3 space-y-3">
            {message.plots.map((plot: string, i: number) => (
              <div key={i} className="rounded overflow-hidden border">
                <img
                  src={plot}
                  alt={`График ${i + 1}`}
                  className="w-full"
                />
                <div className="flex justify-end p-1">
                  <a
                    href={plot}
                    download={`chart_${i + 1}.png`}
                    className="text-xs text-blue-500 hover:underline"
                  >
                    ⬇ Скачать PNG
                  </a>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Tool calls (collapsible debug info) */}
        {message.tool_calls?.length > 0 && (
          <details className="mt-2">
            <summary className="text-xs text-gray-400 cursor-pointer">
              🔧 {message.tool_calls.length} вызов(а) инструментов
            </summary>
            <div className="mt-1 space-y-1">
              {message.tool_calls.map((tc: any, i: number) => (
                <div key={i} className="text-xs bg-gray-100 rounded p-1 font-mono">
                  {tc.tool}({tc.input.sql?.slice(0, 80) || JSON.stringify(tc.input).slice(0, 80)}...)
                </div>
              ))}
            </div>
          </details>
        )}

        <div className="text-xs text-gray-300 mt-1">
          {message.timestamp.toLocaleTimeString()}
        </div>
      </div>
    </div>
  );
}

function LoadingSpinner() {
  return (
    <svg className="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
    </svg>
  );
}
```

---

### 4. Зависимости для Lovable

Добавьте в проект:
```bash
npm install react-markdown
# Опционально для better Markdown:
npm install remark-gfm rehype-highlight
```

---

## Важные замечания для фронтенда

### 1. Время ответа
Агент работает **15–120 секунд** — он делает несколько вызовов к LLM и ClickHouse.
- Всегда показывайте индикатор загрузки
- НЕ устанавливайте `fetch` timeout менее 3 минут
- Если нужен streaming — сообщите, это реализуется отдельно

### 2. Хранение session_id
```typescript
// При старте приложения
const sessionId = localStorage.getItem("analytics_session_id")
  || crypto.randomUUID();
localStorage.setItem("analytics_session_id", sessionId);
```

### 3. Отображение Markdown
Ответ `text_output` содержит Markdown с:
- Заголовками `##`
- Таблицами `| col | col |`
- Списками `-`
- Жирным `**текст**`

Используйте `react-markdown` или аналог.

### 4. Отображение графиков
`plots` — массив строк вида `"data:image/png;base64,iVBOR..."`.
Используйте как `<img src={plot} />` напрямую.

### 5. Обработка ошибок
```typescript
const result = await analyzeQuery(query, sessionId);
if (!result.success) {
  // result.error содержит текст ошибки
  showErrorToast(result.error);
  return;
}
```

---

## Пример запросов к API

### Создать сессию
```bash
curl -X POST https://server.asktab.ru/api/session/new
# → {"session_id": "abc-123-def", "created_at": "..."}
```

### Отправить вопрос
```bash
curl -X POST https://server.asktab.ru/api/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Покажи CTR по кампаниям за январь 2025, постройте bar chart",
    "session_id": "abc-123-def"
  }'
```

### Ответ
```json
{
  "success": true,
  "session_id": "abc-123-def",
  "text_output": "## 📊 CTR по кампаниям за январь 2025\n\nСредний CTR составил **2.34%**...",
  "plots": [
    "data:image/png;base64,iVBORw0KGgoAAAANS..."
  ],
  "tool_calls": [
    {"tool": "list_tables", "input": {}},
    {"tool": "clickhouse_query", "input": {"sql": "SELECT campaign_name, SUM(clicks)/SUM(impressions)*100 as ctr ..."}},
    {"tool": "python_analysis", "input": {"code": "import matplotlib.pyplot as plt\n...", "parquet_path": "..."}}
  ],
  "error": null,
  "timestamp": "2025-01-15T12:00:00"
}
```

### Задать следующий вопрос в той же сессии
```bash
curl -X POST https://server.asktab.ru/api/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "query": "А теперь по неделям — покажи тренд",
    "session_id": "abc-123-def"
  }'
# Агент помнит контекст (предыдущие таблицы, запросы)
```

---

## Swagger UI для тестирования API

Откройте в браузере:
```
https://server.asktab.ru/docs
```

Там можно:
- Посмотреть все эндпоинты
- Тестировать API прямо из браузера
- Скопировать примеры запросов
