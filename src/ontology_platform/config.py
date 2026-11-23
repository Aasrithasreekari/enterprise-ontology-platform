"""Runtime configuration from environment variables (prefix OP_) or a .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OP_", env_file=".env", extra="ignore")

    environment: str = Field(default="local", description="Deployment environment label (local, dev, prod...)")
    database_url: str = Field(default="sqlite:///./ontology.db")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # JWT verification for tokens issued by an external IdP. Disabled when no key is set.
    jwt_algorithm: Literal["HS256", "RS256"] = "HS256"
    jwt_key: SecretStr | None = Field(default=None, description="HS256 secret or RS256 public key (PEM)")
    jwt_audience: str | None = None
    jwt_issuer: str | None = None

    # MCP stdio transport authenticates as the owner of this API key.
    mcp_api_key: SecretStr | None = None
    # MCP HTTP transport: Host header allow-list (DNS-rebinding protection), e.g. "localhost:*,mcp:*".
    # Empty → protection only when bound to localhost (SDK default).
    mcp_allowed_hosts: str = ""

    # Entity resolution
    fuzzy_match_threshold: float = Field(default=0.92, ge=0.5, le=1.0)

    # Connector HTTP behaviour
    http_timeout_seconds: float = 30.0
    http_max_retries: int = Field(default=4, ge=0, le=10)
    http_backoff_base_seconds: float = 0.5

    # Query limits
    max_traversal_depth: int = Field(default=6, ge=1, le=12)
    max_results: int = Field(default=200, ge=1, le=5000)

    @field_validator("jwt_key", "jwt_audience", "jwt_issuer", "mcp_api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Compose passes unset optional variables as empty strings; treat them as "not configured".
        return None if isinstance(value, str) and not value.strip() else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
