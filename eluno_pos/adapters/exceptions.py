"""Structured exceptions for external API clients."""

from typing import Any, Optional


class ElunoPosError(Exception):
    """Base exception for all Eluno POS errors."""
    pass


class ApiClientError(ElunoPosError):
    """Base exception for API communication errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[Any] = None,
        url: Optional[str] = None,
        method: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body
        self.url = url
        self.method = method

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(status_code={self.status_code}, "
            f"message={self.message!r}, url={self.url!r})"
        )


class ConnectionError(ApiClientError):
    """Network connection failure (e.g. refused connection, DNS failure)."""
    pass


class TimeoutError(ApiClientError):
    """Request or socket timed out."""
    pass


class RateLimitError(ApiClientError):
    """HTTP 429 Too Many Requests."""

    def __init__(
        self,
        message: str,
        retry_after_seconds: float = 1.0,
        status_code: int = 429,
        response_body: Optional[Any] = None,
        url: Optional[str] = None,
        method: Optional[str] = None,
    ) -> None:
        super().__init__(
            message=message,
            status_code=status_code,
            response_body=response_body,
            url=url,
            method=method,
        )
        self.retry_after_seconds = retry_after_seconds


class ServerError(ApiClientError):
    """HTTP 5xx server error."""
    pass


class NotFoundError(ApiClientError):
    """HTTP 404 resource not found."""
    pass


class ValidationError(ApiClientError):
    """HTTP 422 unprocessable entity or invalid request data."""
    pass


class AmbiguousOperationError(ApiClientError):
    """Raised when an operation has an ambiguous result (e.g. Shopify order creation timeout).

    The caller must NOT blindly retry this operation, as doing so may cause duplicate
    orders or double-decremented inventory. The operation should be kept in a
    reconciliation-pending state.
    """
    pass

