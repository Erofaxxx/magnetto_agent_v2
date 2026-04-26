"""
Pydantic-модель структурированного ответа MAIN агента.

Используется через `response_format=MainFinalAnswer` в `create_deep_agent`.
Аналогично тому как у subagents работает `SubagentResult`, но для main.

ЗАЧЕМ
─────
Main агент имеет тенденцию переписывать sub'овский ответ своими словами
(дорого + риск дубля + риск обрыва при cap'е). Единственный надёжный
способ это предотвратить — НЕ ДАВАТЬ main писать длинный free-text.
`response_format=MainFinalAnswer` с `max_length=600` chars на единственное
поле `text` обеспечивает это структурно: модель ОБЯЗАНА положить весь
свой финальный ответ в `text`, и Pydantic валидация физически отрежет
если он превысит 600 chars.

КАК РАБОТАЕТ
────────────
- Main делает обычные tool calls (think_tool, task, python_analysis, ...)
- Когда main готов закончить — он вызывает специальный structured-output
  tool (LangChain автоматически добавляет его при response_format)
- Аргумент tool'а должен соответствовать `MainFinalAnswer` Pydantic схеме
- `max_length=600` на `text` поле = main НЕ МОЖЕТ выкатить переписку
  sub'овского ответа (тот обычно 3000+ chars)
- api_adapter извлекает `structured_response.text` и склеивает с
  sub'овскими summary

CASE 1 — main ДЕЛЕГИРОВАЛ через task():
  Финал = sub.summary(s) + "\\n---\\n" + main.text
  main.text — короткий комментарий «следующий шаг», «доп.график X», etc.
  Не повторяет sub.summary (даже не может, max=600 chars).

CASE 2 — main отвечает САМ (без task, через sample_table или из system):
  Финал = main.text
  600 chars хватает на короткий ответ. Если задача требует длинного
  ответа — нужно делегировать в подагент (тот не ограничен 600 chars).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class MainFinalAnswer(BaseModel):
    """Структурированный финальный ответ main агента."""

    text: str = Field(
        default="",
        max_length=600,
        description=(
            "Текст от main для финального ответа. Жёсткий лимит 600 chars.\n\n"
            "ПОСЛЕ task() делегирования: ОЧЕНЬ короткий комментарий поверх "
            "ответа подагента — следующий шаг расследования, контекст из "
            "истории сессии, ссылка на доп.график построенный через "
            "python_analysis. НЕ повторяй ответ подагента — он показывается "
            "пользователю автоматически целиком. Если нечего добавить — "
            "оставь пустым (default).\n\n"
            "БЕЗ task() (когда отвечаешь сам через sample_table или из "
            "system prompt): полный ответ для пользователя в Markdown. Если "
            "ответ требует больше 600 chars — это сигнал что надо было "
            "делегировать в подагент."
        ),
    )
