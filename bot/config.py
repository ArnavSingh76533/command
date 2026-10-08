import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    log_group: int
    owner_id: int | None
    owner_username: str
    database: str
    base_url: str
    model: str
    key_env: str
    threshold: float
    mute_minutes: int
    concurrency: int
    llm_format: str

    @classmethod
    def load(cls):
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        log_group = os.getenv("LOG_GROUP_ID", "").strip()
        if not token or not log_group:
            raise ValueError("Set BOT_TOKEN and LOG_GROUP_ID in .env")
        threshold = float(os.getenv("MODERATION_THRESHOLD", "0.92"))
        if not 0.8 <= threshold <= 1:
            raise ValueError("MODERATION_THRESHOLD must be between 0.8 and 1")
        fmt = os.getenv("LLM_FORMAT", "schema")
        if fmt not in {"schema", "json", "text"}:
            raise ValueError("LLM_FORMAT must be schema, json, or text")
        return cls(
            token,
            int(log_group),
            int(os.environ["OWNER_ID"]) if os.getenv("OWNER_ID") else None,
            os.getenv("OWNER_USERNAME", "yucant").lstrip("@").lower(),
            os.getenv("DATABASE_PATH", "data/bot.sqlite3"),
            os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
            os.getenv("LLM_MODEL", "openai/gpt-oss-120b"),
            os.getenv("LLM_KEY_ENV", "GROQ_API_KEY"),
            threshold,
            max(1, int(os.getenv("MUTE_MINUTES", "60"))),
            max(1, int(os.getenv("LLM_CONCURRENCY", "3"))),
            fmt,
        )
