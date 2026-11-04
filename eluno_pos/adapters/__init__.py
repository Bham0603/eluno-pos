"""Adapters package for external system integrations."""

from eluno_pos.adapters.exceptions import (
    ElunoPosError,
    ApiClientError,
    ConnectionError,
    TimeoutError,
    RateLimitError,
    ServerError,
    NotFoundError,
    ValidationError,
    AmbiguousOperationError,
)
from eluno_pos.adapters.http_client import HttpClient, HttpResponse
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.adapters.smt_client import SmtClient
from eluno_pos.adapters.terminal_client import TerminalClient

__all__ = [
    "ElunoPosError",
    "ApiClientError",
    "ConnectionError",
    "TimeoutError",
    "RateLimitError",
    "ServerError",
    "NotFoundError",
    "ValidationError",
    "AmbiguousOperationError",
    "HttpClient",
    "HttpResponse",
    "ShopifyClient",
    "SmtClient",
    "TerminalClient",
]

