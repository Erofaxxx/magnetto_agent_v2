"""
Configuration for ClickHouse Analytics Agent.
Loaded from .env file at project root.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the same directory as this file
load_dotenv(Path(__file__).parent / ".env")

# ─── OpenRouter ──────────────────────────────────────────────────────────────
OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")

# Which provider to use: "anthropic" (Claude, supports prompt caching)
#                     or "deepseek"  (DeepSeek, no caching, cheaper)
MODEL_PROVIDER: str = os.environ.get("MODEL_PROVIDER", "anthropic")

# Default model name per provider — can be overridden via MODEL env var.
_PROVIDER_DEFAULT_MODELS = {
    "anthropic": "anthropic/claude-sonnet-4.6",
    "deepseek":  "deepseek/deepseek-v3.2",
}
MODEL: str = os.environ.get("MODEL", _PROVIDER_DEFAULT_MODELS.get(MODEL_PROVIDER, "anthropic/claude-sonnet-4.6"))

MAX_TOKENS: int = int(os.environ.get("MAX_TOKENS", "8192"))

# Supported models: model_name → provider
# Provider determines prompt-caching strategy and extra_body settings.
ALLOWED_MODELS: dict[str, str] = {
    "anthropic/claude-sonnet-4.6": "anthropic",
    "deepseek/deepseek-v3.2": "deepseek",
}

# Router: модель для классификации запросов и загрузки нужных skills.
# Sonnet выбран намеренно: правильно игнорирует приветствия в начале сложных запросов,
# надёжно возвращает строгий JSON без объяснений.
ROUTER_MODEL: str = os.environ.get("ROUTER_MODEL", "anthropic/claude-sonnet-4.6")

# ─── ClickHouse ──────────────────────────────────────────────────────────────
CLICKHOUSE_HOST: str = (
    os.environ.get("CLICKHOUSE_HOST", "")
    .replace("https://", "")
    .replace("http://", "")
)
CLICKHOUSE_PORT: int = int(os.environ.get("CLICKHOUSE_PORT", "8443"))
CLICKHOUSE_USER: str = os.environ.get("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD: str = os.environ.get("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_DATABASE: str = os.environ.get("CLICKHOUSE_DATABASE", "default")

# Custom CA certificate (for self-signed or private CA).
# Leave CLICKHOUSE_SSL_CERT_PATH empty when using a trusted CA (e.g. Let's Encrypt).
CLICKHOUSE_SSL_CERT: str = ""
_ssl_path = os.environ.get("CLICKHOUSE_SSL_CERT_PATH", "")
if _ssl_path:
    _cert = Path(_ssl_path)
    if not _cert.is_absolute():
        _cert = Path(__file__).parent / _cert
    if _cert.exists():
        CLICKHOUSE_SSL_CERT = str(_cert.resolve())

# ─── Server ───────────────────────────────────────────────────────────────────
SERVER_URL: str = os.environ.get("SERVER_URL", "http://localhost:8000")
HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8000"))

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).parent
TEMP_DIR: Path = BASE_DIR / "temp_data"
TEMP_DIR.mkdir(exist_ok=True)
DB_PATH: str = str(BASE_DIR / "chat_history.db")

# ─── Limits ───────────────────────────────────────────────────────────────────
MAX_AGENT_ITERATIONS: int = int(os.environ.get("MAX_AGENT_ITERATIONS", "15"))
TEMP_FILE_TTL_SECONDS: int = int(os.environ.get("TEMP_FILE_TTL_SECONDS", "3600"))  # 1 hour
# How many past HumanMessage turns to keep in context (sliding window).
# Older turns are dropped; current turn is always kept in full.
MAX_HISTORY_TURNS: int = int(os.environ.get("MAX_HISTORY_TURNS", "10"))
