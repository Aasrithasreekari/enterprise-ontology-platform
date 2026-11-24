"""Runtime configuration from environment variables (prefix OP_) or a .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OP_", env_file=".env", extra="ignore")

    environment: str = Field(default="local", description="Deployment environment label (local, dev, prod...)")
    database_url: str = Field(default="sqlite:///./ontology.db")
    # Connection pool (PostgreSQL). Bounds how many requests/agents can hold a connection at once.
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=10, ge=0, le=200)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0)
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

    # Agent workflow limits
    agent_step_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    agent_max_retries: int = Field(default=1, ge=0, le=3)
    agent_max_parallel: int = Field(default=4, ge=1, le=32)
    agent_max_steps: int = Field(default=12, ge=1, le=50)
    workflow_runs_retained: int = Field(default=1000, ge=10)

    # Query limits
    max_traversal_depth: int = Field(default=6, ge=1, le=12)
    max_results: int = Field(default=200, ge=1, le=5000)

    @field_validator("jwt_key", "jwt_audience", "jwt_issuer", "mcp_api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Compose passes unset optional variables as empty strings; treat them as "not configured".
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _strong_hmac_key(self) -> "Settings":
        # A short shared secret makes HS256 tokens brute-forceable (RFC 7518 §3.2: key >= hash size).
        if self.jwt_key is not None and self.jwt_algorithm == "HS256" and len(self.jwt_key.get_secret_value()) < 32:
            raise ValueError("OP_JWT_KEY must be at least 32 characters for HS256")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
