"""Configuration management for Eluno POS."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    """Application configuration with sensible defaults for local development."""
    shopify_base_url: str = "http://localhost:8787/admin/api/2026-10"
    smt_base_url: str = "http://localhost:8787/smt"
    terminal_base_url: str = "http://localhost:8787/terminal"
    http_timeout_seconds: float = 5.0
    max_retries_429: int = 3
    max_retries_terminal_503: int = 3
    backoff_factor: float = 1.0

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Load configuration from environment variables or use defaults."""
        return cls(
            shopify_base_url=os.getenv("SHOPIFY_BASE_URL", "http://localhost:8787/admin/api/2026-10"),
            smt_base_url=os.getenv("SMT_BASE_URL", "http://localhost:8787/smt"),
            terminal_base_url=os.getenv("TERMINAL_BASE_URL", "http://localhost:8787/terminal"),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "5.0")),
            max_retries_429=int(os.getenv("MAX_RETRIES_429", "3")),
            max_retries_terminal_503=int(os.getenv("MAX_RETRIES_TERMINAL_503", "3")),
            backoff_factor=float(os.getenv("BACKOFF_FACTOR", "1.0")),
        )

