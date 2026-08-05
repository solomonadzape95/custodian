"""Environment-derived settings, loaded once at import."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


@dataclass(frozen=True)
class Config:
    gms_url: str
    gms_token: str
    provider: str
    gemini_api_key: str
    gemini_model: str
    max_tool_calls: int

    @classmethod
    def load(cls) -> "Config":
        return cls(
            gms_url=os.getenv("DATAHUB_GMS_URL", "http://localhost:8080"),
            gms_token=os.getenv("DATAHUB_GMS_TOKEN", ""),
            provider=os.getenv("CUSTODIAN_PROVIDER", "gemini"),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
            max_tool_calls=int(os.getenv("CUSTODIAN_MAX_TOOL_CALLS", "40")),
        )

    @property
    def is_local(self) -> bool:
        return "localhost" in self.gms_url or "127.0.0.1" in self.gms_url

    def require_datahub(self) -> None:
        """A token is only required where the metadata service enforces auth.

        `datahub docker quickstart` ships with METADATA_SERVICE_AUTH_ENABLED=false,
        so demanding a PAT would block the setup path the README recommends.
        """
        if not self.gms_token and not self.is_local:
            raise ConfigError(
                f"DATAHUB_GMS_TOKEN is not set and {self.gms_url} is not local. "
                "Generate a Personal Access Token in the DataHub UI "
                "(Settings -> Access Tokens) and add it to .env"
            )

    def require_provider(self) -> None:
        if self.provider == "gemini" and not self.gemini_api_key:
            raise ConfigError(
                "GEMINI_API_KEY is not set. Get a free-tier key at "
                "https://aistudio.google.com/apikey and add it to .env"
            )


CONFIG = Config.load()
