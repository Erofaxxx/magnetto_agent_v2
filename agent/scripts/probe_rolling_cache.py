"""
Probe: проверка что CachingMiddleware правильно кэширует растущую tool-chain.

Симулируем внутренний цикл агента на N шагов. На каждом шаге добавляем в
messages пару (AIMessage(tool_call), ToolMessage(result)) и смотрим, что
Anthropic возвращает в usage_metadata.

Без фикса (старая схема):
  - step 2: cache_write ≈ размер system+H+AI+T1  (~15K)
  - step 3: cache_write ≈ size(AI1+T1+AI2+T2)    (~2K новый, + T1 переписан)
  - step 4: cache_write ≈ size(AI1+T1+AI2+T2+AI3+T3)  (+T1 и T2 переписаны)
  → cache_write растёт линейно

После фикса (rolling window на 2 последних ToolMessage):
  - step 2: cache_write ≈ size(system+H+AI+T1)   (первичный)
  - step 3: cache_write ≈ size(AI2+T2)           (только дельта!)
  - step 4: cache_write ≈ size(AI3+T3)           (только дельта!)
  → cache_write плоский

Запуск:
  cd /root/clickhouse_analytics_agent/agent
  /root/clickhouse_analytics_agent/venv/bin/python scripts/probe_rolling_cache.py
"""
import os
import sys
import time
import uuid
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

# Делаем CachingMiddleware импортируемым
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from core.caching_middleware import CachingMiddleware, _apply_breakpoints


class _FakeRequest:
    def __init__(self, system_message, messages, model):
        self.system_message = system_message
        self.messages = messages
        self.model = model


def _fake_model():
    """Минимальный шим под _is_anthropic detector."""
    class M:
        model_name = "anthropic/claude-sonnet-4.6"
    return M()


def _build_messages(n_tools: int) -> list:
    """Собрать [Human, AI1, T1, AI2, T2, ...] с n_tools ToolMessage."""
    msgs = [HumanMessage(content="Исходный запрос пользователя")]
    for i in range(1, n_tools + 1):
        tc_id = f"call_{i}"
        msgs.append(
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "clickhouse_query",
                    "args": {"sql": f"SELECT {i}"},
                    "id": tc_id,
                }],
            )
        )
        msgs.append(
            ToolMessage(
                content=f"result of step {i}: " + ("x" * 200),
                tool_call_id=tc_id,
                name="clickhouse_query",
            )
        )
    return msgs


def main():
    system = SystemMessage(content="Stable system prompt for probe. " * 100)
    model = _fake_model()

    print("=" * 70)
    print("Проверка маркеров, которые ставит новая CachingMiddleware")
    print("=" * 70)

    for n in (0, 1, 2, 3, 5):
        msgs = _build_messages(n)
        req = _FakeRequest(system_message=system, messages=list(msgs), model=model)
        _apply_breakpoints(req)

        print(f"\nn_tools={n}")
        print(f"  system cache_control: {_has_cc(req.system_message.content)}")
        marked = []
        for i, m in enumerate(req.messages):
            if _has_cc(m.content):
                kind = type(m).__name__
                marked.append(f"[{i}]={kind}")
        print(f"  messages marked: {marked if marked else '(none)'}")
        total = (1 if _has_cc(req.system_message.content) else 0) + len(marked)
        print(f"  total breakpoints in request: {total} (Anthropic max = 4)")

    # ── Опциональный live-probe через OpenRouter (требует OPENROUTER_API_KEY) ──
    if os.environ.get("PROBE_LIVE") == "1":
        _live_probe(system)


def _has_cc(content):
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("cache_control")
            for b in content
        )
    return False


def _live_probe(system):
    """
    Реальный запрос к OpenRouter на 4 последовательных шага с растущим
    tool-chain. Распечатывает usage_metadata для каждого.
    """
    from langchain_openai import ChatOpenAI

    print("\n" + "=" * 70)
    print("LIVE PROBE to OpenRouter — смотрим cache_creation / cache_read")
    print("=" * 70)

    llm = ChatOpenAI(
        model="anthropic/claude-sonnet-4.6",
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api/v1",
        max_tokens=50,
        default_headers={"HTTP-Referer": "https://server.asktab.ru", "X-Title": "cache-probe-rolling"},
        extra_body={
            "provider": {"order": ["Anthropic"], "allow_fallbacks": False},
            "usage": {"include": True},
        },
    )

    # Делаем огромный system, чтобы cache_write был заметен.
    big_system = SystemMessage(content="Probe system. " * 1500)
    for n_tools in (0, 1, 2, 3, 4):
        msgs = _build_messages(n_tools)
        req = _FakeRequest(system_message=big_system, messages=list(msgs), model=_fake_model())
        _apply_breakpoints(req)

        full = [req.system_message] + req.messages + [HumanMessage(content=f"probe step n_tools={n_tools}")]
        resp = llm.invoke(full)
        usage = resp.usage_metadata or {}
        itd = usage.get("input_token_details", {}) or {}
        print(
            f"n_tools={n_tools:>2}  input={usage.get('input_tokens', '?'):>6}  "
            f"cache_read={itd.get('cache_read', 0):>6}  "
            f"cache_creation={itd.get('cache_creation', 0):>6}  "
            f"out={usage.get('output_tokens', '?')}"
        )
        time.sleep(2)


if __name__ == "__main__":
    main()
