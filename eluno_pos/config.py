"""Configuration management for Eluno POS."""

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


def load_env_file(env_path: Optional[Path] = None) -> None:
    """Safely load key-value pairs from a .env file if present without overriding existing env vars."""
    target_path = env_path or Path(".env")
    if not target_path.is_file():
        return

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        # Gracefully handle reading errors on non-critical .env files
        pass


@dataclass(frozen=True)
class AppConfig:
    """Application configuration with sensible defaults for local development and mock testing."""
    shopify_base_url: str = "http://localhost:8787/admin/api/2026-10"
    smt_base_url: str = "http://localhost:8787/smt"
    terminal_base_url: str = "http://localhost:8787/terminal"
    db_path: str = "eluno_pos.db"
    http_timeout_seconds: float = 5.0
    max_retries_429: int = 3
    max_retries_terminal_503: int = 3
    backoff_factor: float = 1.0

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "AppConfig":
        """Load configuration from environment variables (loading .env if present) or use defaults."""
        load_env_file(env_file)

        return cls(
            shopify_base_url=os.getenv("SHOPIFY_BASE_URL", "http://localhost:8787/admin/api/2026-10"),
            smt_base_url=os.getenv("SMT_BASE_URL", "http://localhost:8787/smt"),
            terminal_base_url=os.getenv("TERMINAL_BASE_URL", "http://localhost:8787/terminal"),
            db_path=os.getenv("DATABASE_PATH", "eluno_pos.db"),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "5.0")),
            max_retries_429=int(os.getenv("MAX_RETRIES_429", "3")),
            max_retries_terminal_503=int(os.getenv("MAX_RETRIES_TERMINAL_503", "3")),
            backoff_factor=float(os.getenv("BACKOFF_FACTOR", "1.0")),
        )

    def is_local_mock(self) -> bool:
        """Check if all endpoints point to local mock endpoints to prevent accidental production mutations."""
        endpoints = [self.shopify_base_url, self.smt_base_url, self.terminal_base_url]
        return all("localhost" in ep or "127.0.0.1" in ep for ep in endpoints)
