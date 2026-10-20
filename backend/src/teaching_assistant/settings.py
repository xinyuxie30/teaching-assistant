import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TA_", env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+asyncpg://teaching:local-development-only@127.0.0.1:5432/teaching"
    )
    dev_api_token: str = ""
    # Batch requests opt in to a real profile explicitly; configuring a key never changes defaults.
    evaluator: Literal["fake-v1"] = "fake-v1"
    jev_api_key: SecretStr | None = None
    jev_model: str | None = Field(default=None, max_length=80)
    jev_endpoint: str = "https://api.typesafe.ai/v1/systemone"
    jev_max_input_characters: int = Field(default=20000, ge=1000, le=200000)
    call_timeout_seconds: float = Field(default=10, gt=0, le=60)
    lease_seconds: int = Field(default=30, ge=5, le=300)
    poll_seconds: float = Field(default=1, gt=0)
    retry_delay_seconds: int = Field(default=2, ge=0, le=3600)

    @model_validator(mode="after")
    def validate_runtime(self):
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("Only PostgreSQL with asyncpg is supported")
        if self.lease_seconds <= self.call_timeout_seconds + 2:
            raise ValueError("lease_seconds must exceed call_timeout_seconds by more than 2")
        if self.jev_model is not None and not re.fullmatch(r"jev-\d+\.\d+\.\d+", self.jev_model):
            raise ValueError("TA_JEV_MODEL must be a pinned version, e.g. jev-1.13.0, not an alias")
        endpoint = urlsplit(self.jev_endpoint)
        if (
            endpoint.scheme not in {"https", "http"}
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path != "/v1/systemone"
        ):
            raise ValueError(
                "TA_JEV_ENDPOINT must be an absolute /v1/systemone URL without credentials"
            )
        if endpoint.scheme == "http" and endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Real provider endpoints must use HTTPS")
        return self
