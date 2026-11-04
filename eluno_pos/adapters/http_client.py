"""Reusable HTTP client built on Python standard library with resilient retry handling."""

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from eluno_pos.adapters.exceptions import (
    ApiClientError,
    ConnectionError,
    NotFoundError,
    RateLimitError,
    ServerError,
    TimeoutError,
    ValidationError,
)


@dataclass
class HttpResponse:
    """Standard HTTP response container."""
    status_code: int
    data: Any
    headers: Dict[str, str]


class HttpClient:
    """Robust, dependency-free HTTP client with rate-limiting and selective retry support."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        max_retries_429: int = 3,
        backoff_factor: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries_429 = max_retries_429
        self.backoff_factor = backoff_factor
        self.sleep_fn = sleep_fn

    def _build_url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        """Construct full URL with optional query parameters."""
        if path.startswith("http://") or path.startswith("https://"):
            full_url = path
        else:
            clean_path = path if path.startswith("/") else f"/{path}"
            full_url = f"{self.base_url}{clean_path}"

        if params:
            # Filter out None values
            query_items = []
            for k, v in params.items():
                if v is not None:
                    if isinstance(v, (list, tuple, set)):
                        query_items.append((k, ",".join(str(x) for x in v)))
                    else:
                        query_items.append((k, str(v)))
            if query_items:
                query_string = urllib.parse.urlencode(query_items)
                delimiter = "&" if "?" in full_url else "?"
                full_url = f"{full_url}{delimiter}{query_string}"

        return full_url

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        allow_retry_429: bool = True,
        allow_retry_503: bool = False,
        max_retries_503: int = 3,
    ) -> HttpResponse:
        """Execute an HTTP request with structured error handling and selective retries."""
        url = self._build_url(path, params)
        req_headers = {"Accept": "application/json"}
        if headers:
            req_headers.update(headers)

        body_bytes = None
        if json_data is not None:
            body_bytes = json.dumps(json_data).encode("utf-8")
            req_headers["Content-Type"] = "application/json; charset=utf-8"

        retries_429 = 0
        retries_503 = 0

        while True:
            req = urllib.request.Request(
                url=url,
                data=body_bytes,
                headers=req_headers,
                method=method.upper(),
            )

            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_headers = {k: v for k, v in resp.headers.items()}
                    raw_body = resp.read()
                    data = None
                    if raw_body:
                        try:
                            data = json.loads(raw_body.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            data = raw_body.decode("utf-8", errors="replace")

                    return HttpResponse(
                        status_code=resp.status,
                        data=data,
                        headers=resp_headers,
                    )

            except urllib.error.HTTPError as err:
                resp_headers = {k: v for k, v in err.headers.items()}
                raw_body = err.read()
                err_data = None
                if raw_body:
                    try:
                        err_data = json.loads(raw_body.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        err_data = raw_body.decode("utf-8", errors="replace")

                # 1. Handle HTTP 429 Too Many Requests
                if err.code == 429:
                    retry_after_str = resp_headers.get("Retry-After", "1.0")
                    try:
                        retry_after = float(retry_after_str)
                    except ValueError:
                        retry_after = 1.0

                    if allow_retry_429 and retries_429 < self.max_retries_429:
                        retries_429 += 1
                        self.sleep_fn(retry_after)
                        continue
                    raise RateLimitError(
                        message=f"Rate limit exceeded (HTTP 429) for {method} {url}",
                        retry_after_seconds=retry_after,
                        status_code=429,
                        response_body=err_data,
                        url=url,
                        method=method,
                    )

                # 2. Handle HTTP 503 Service Unavailable (selective retry)
                if err.code == 503 and allow_retry_503:
                    if retries_503 < max_retries_503:
                        retries_503 += 1
                        self.sleep_fn(0.2 * retries_503 * self.backoff_factor)
                        continue
                    raise ServerError(
                        message=f"Service unavailable (HTTP 503) after {retries_503} retries for {method} {url}",
                        status_code=503,
                        response_body=err_data,
                        url=url,
                        method=method,
                    )

                # 3. Handle HTTP 404
                if err.code == 404:
                    raise NotFoundError(
                        message=f"Resource not found (HTTP 404): {method} {url}",
                        status_code=404,
                        response_body=err_data,
                        url=url,
                        method=method,
                    )

                # 4. Handle HTTP 422
                if err.code == 422:
                    raise ValidationError(
                        message=f"Validation failed (HTTP 422) for {method} {url}: {err_data}",
                        status_code=422,
                        response_body=err_data,
                        url=url,
                        method=method,
                    )

                # 5. Handle HTTP 5xx
                if err.code >= 500:
                    raise ServerError(
                        message=f"Server error (HTTP {err.code}) for {method} {url}",
                        status_code=err.code,
                        response_body=err_data,
                        url=url,
                        method=method,
                    )

                # 6. Other 4xx client errors
                raise ApiClientError(
                    message=f"API error (HTTP {err.code}) for {method} {url}: {err_data}",
                    status_code=err.code,
                    response_body=err_data,
                    url=url,
                    method=method,
                )

            except (socket.timeout, TimeoutError) as err:
                raise TimeoutError(
                    message=f"Request timed out for {method} {url}: {err}",
                    url=url,
                    method=method,
                ) from err

            except urllib.error.URLError as err:
                if isinstance(err.reason, socket.timeout) or "timed out" in str(err.reason).lower():
                    raise TimeoutError(
                        message=f"Connection timed out for {method} {url}: {err.reason}",
                        url=url,
                        method=method,
                    ) from err
                raise ConnectionError(
                    message=f"Connection failed for {method} {url}: {err.reason}",
                    url=url,
                    method=method,
                ) from err

    def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        allow_retry_429: bool = True,
    ) -> HttpResponse:
        """Convenience method for HTTP GET."""
        return self.request(
            method="GET",
            path=path,
            params=params,
            headers=headers,
            allow_retry_429=allow_retry_429,
        )

    def post(
        self,
        path: str,
        json_data: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        allow_retry_429: bool = True,
        allow_retry_503: bool = False,
        max_retries_503: int = 3,
    ) -> HttpResponse:
        """Convenience method for HTTP POST."""
        return self.request(
            method="POST",
            path=path,
            params=params,
            json_data=json_data,
            headers=headers,
            allow_retry_429=allow_retry_429,
            allow_retry_503=allow_retry_503,
            max_retries_503=max_retries_503,
        )

