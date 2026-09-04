"""Runtime settings. Everything comes from environment variables prefixed ``RELAY_``.

See ``.env.example`` for the documented list. Relay stores no LLM API keys for users; the
only model credentials it reads are the team's extraction/embedding key, used by workers.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RELAY_", env_file=".env", extra="ignore")

    # Identity of this node
    public_url: str = Field(
        default="http://localhost:8000",
        description="URL clients use to reach relay-api (behind Caddy in production).",
    )
    team_name: str = "relay"
    environment: Literal["dev", "prod", "test"] = "dev"
    log_level: str = "INFO"

    # Storage
    database_url: str = "postgresql+asyncpg://relay:relay@postgres:5432/relay"
    redis_url: str = "redis://redis:6379/0"
    data_dir: Path = Path("/var/lib/relay")

    # Auth
    token_pepper: str = Field(
        default="",
        description="Optional server-side pepper mixed into token hashes. Rotate = revoke all.",
    )
    token_ttl_days: int = 365

    # Team memory (graph). Backend is pluggable; see docs/protocols.md.
    memory_backend: Literal["graphiti-kuzu", "none"] = "graphiti-kuzu"
    graph_path: Path = Path("/var/lib/relay/graph.kuzu")

    # Team model key: used ONLY by workers for extraction, digests, and embeddings.
    extraction_model: str = Field(
        default="keyword",
        description=(
            "Pydantic AI model string such as 'openai:gpt-5-mini' or 'anthropic:claude-sonnet-5'. "
            "'keyword' selects the deterministic offline extractor (no API key needed)."
        ),
    )
    embedding_model: str = Field(
        default="openai:text-embedding-3-small",
        description="Pydantic AI embedding model, 1536-d (see EMBEDDING_DIM). Empty disables the vector leg.",
    )

    # Uploads
    max_upload_mb: int = Field(
        default=512, description="Largest accepted recording. Caddy enforces the same limit."
    )

    # Transcription
    transcriber: Literal["whisperx", "fixture"] = "whisperx"
    whisperx_model: str = "large-v3"
    whisperx_device: Literal["auto", "cuda", "cpu"] = "auto"
    whisperx_compute_type: str = "auto"
    hf_token: str = Field(default="", description="Hugging Face token for pyannote diarization.")

    # Slack: one app, Socket Mode, no public URL.
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_team_channel: str = Field(default="", description="Channel id for summaries/digests.")

    # Google Workspace via workspace-mcp (per-user OAuth handled by that service).
    workspace_mcp_url: str = "http://workspace-mcp:8000/mcp"

    # Coding-agent sandbox image, advertised to user agents (they run it; Relay does not).
    sandbox_image: str = "ghcr.io/relayagents/relay-sandbox:latest"

    # Digest schedule (UTC cron fields for arq)
    digest_hour_utc: int = 17
    digest_minute_utc: int = 0

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def slack_enabled(self) -> bool:
        """Can post via the Web API (workers and API). Needs only the bot token."""
        return bool(self.slack_bot_token)

    @property
    def slack_socket_mode_enabled(self) -> bool:
        """Can receive button clicks over Socket Mode. API only; needs the app token too."""
        return bool(self.slack_bot_token and self.slack_app_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
