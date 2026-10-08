from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

DEFAULT_GMAIL_QUERY = (
    "in:inbox newer_than:{days}d "
    "(rechnung OR invoice OR iban OR zahlung OR überweisung OR fällig OR "
    "zahlungserinnerung OR mahnung OR bill OR payment)"
)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    openai_model: str
    openai_reasoning_effort: str | None
    gmail_mcp_url: str
    gmail_query: str
    google_client_id: str | None
    google_client_secret: str | None
    google_refresh_token: str | None
    google_oauth_port: int
    telegram_bot_token: str | None
    telegram_chat_id: int | None
    poll_interval_seconds: int
    remind_after_hours: float
    timezone: ZoneInfo
    data_dir: Path = field(default=Path(".data"))
    # Own addresses: invoices forwarded from them are judged by the original sender.
    owner_emails: frozenset[str] = frozenset()

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @property
    def store_db(self) -> Path:
        return self.data_dir / "store.sqlite"

    @property
    def google_token_file(self) -> Path:
        return self.data_dir / "google_token.json"

    @property
    def gmail_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


def load_settings() -> Settings:
    load_dotenv()
    chat_id = _env("TELEGRAM_CHAT_ID")
    lookback_days = int(_env("GMAIL_LOOKBACK_DAYS", "7"))
    settings = Settings(
        openai_model=_env("OPENAI_MODEL", "gpt-5.4-mini"),
        # Only sent for reasoning models; set OPENAI_REASONING_EFFORT="" for e.g. gpt-4.1.
        openai_reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "low") or None,
        gmail_mcp_url=_env("GMAIL_MCP_URL", "https://katechat.tech/mcp/gmail"),
        gmail_query=_env("GMAIL_QUERY", DEFAULT_GMAIL_QUERY).format(days=lookback_days),
        google_client_id=_env("GOOGLE_CLIENT_ID"),
        google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
        google_refresh_token=_env("GOOGLE_REFRESH_TOKEN"),
        google_oauth_port=int(_env("GOOGLE_OAUTH_PORT", "8765")),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=int(chat_id) if chat_id else None,
        poll_interval_seconds=int(_env("POLL_INTERVAL_SECONDS", "300")),
        remind_after_hours=float(_env("REMIND_AFTER_HOURS", "24")),
        timezone=ZoneInfo(_env("TIMEZONE", "Europe/Vienna")),
        data_dir=Path(_env("DATA_DIR", ".data")),
        owner_emails=frozenset(a.strip().lower() for a in (_env("OWNER_EMAILS", "") or "").split(",") if a.strip()),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
