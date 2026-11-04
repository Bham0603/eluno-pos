"""Unit tests for the resilient HttpClient."""

import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from eluno_pos.adapters.exceptions import (
    ApiClientError,
    ConnectionError,
    NotFoundError,
    RateLimitError,
    ServerError,
    TimeoutError,
    ValidationError,
)
from eluno_pos.adapters.http_client import HttpClient


class TestHttpClient(unittest.TestCase):
    """Test suite for HttpClient error handling and retry behaviors."""

    def setUp(self) -> None:
        self.sleep_calls = []
        self.client = HttpClient(
            base_url="http://test.local/api",
            timeout=2.0,
            max_retries_429=2,
            sleep_fn=lambda s: self.sleep_calls.append(s),
        )

    def _make_http_response(self, status=200, body=None, headers=None):
        resp = MagicMock()
        resp.status = status
        resp.headers = headers or {}
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        resp.read.return_value = raw
        return resp

    def _make_http_error(self, code=400, body=None, headers=None):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        fp = io.BytesIO(raw)
        return urllib.error.HTTPError(
            url="http://test.local/api/test",
            code=code,
            msg=f"HTTP {code}",
            hdrs=headers or {},
            fp=fp,
        )

    @patch("urllib.request.urlopen")
    def test_successful_get_request(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value = self._make_http_response(
            status=200,
            body={"key": "value"},
            headers={"Content-Type": "application/json"},
        )

        resp = self.client.get("test", params={"q": "glasses"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {"key": "value"})

        # Verify URL called has encoded parameters
        req_arg = mock_urlopen.call_args[0][0]
        self.assertEqual(req_arg.full_url, "http://test.local/api/test?q=glasses")
        self.assertEqual(req_arg.get_method(), "GET")

    @patch("urllib.request.urlopen")
    def test_successful_post_request(self, mock_urlopen):
        mock_urlopen.return_value.__enter__.return_value = self._make_http_response(
            status=201,
            body={"id": 123},
        )

        resp = self.client.post("items", json_data={"name": "frame"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data, {"id": 123})

        req_arg = mock_urlopen.call_args[0][0]
        self.assertEqual(req_arg.data, b'{"name": "frame"}')
        self.assertEqual(req_arg.headers["Content-type"], "application/json; charset=utf-8")

    @patch("urllib.request.urlopen")
    def test_404_not_found(self, mock_urlopen):
        mock_urlopen.side_effect = self._make_http_error(404, {"error": "not found"})
        with self.assertRaises(NotFoundError) as ctx:
            self.client.get("missing")
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("urllib.request.urlopen")
    def test_422_validation_error(self, mock_urlopen):
        mock_urlopen.side_effect = self._make_http_error(422, {"field": ["invalid"]})
        with self.assertRaises(ValidationError) as ctx:
            self.client.post("create", json_data={})
        self.assertEqual(ctx.exception.status_code, 422)

    @patch("urllib.request.urlopen")
    def test_429_retry_success(self, mock_urlopen):
        err_429 = self._make_http_error(429, {"errors": "Too Many Requests"}, {"Retry-After": "1.5"})
        success_200 = self._make_http_response(200, {"status": "ok"})

        # First call fails with 429, second succeeds
        mock_urlopen.side_effect = [
            err_429,
            MagicMock(__enter__=MagicMock(return_value=success_200)),
        ]

        resp = self.client.get("rate-limited", allow_retry_429=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.sleep_calls, [1.5])
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_429_exhaustion_raises_rate_limit_error(self, mock_urlopen):
        err_429 = self._make_http_error(429, {"errors": "rate limit"}, {"Retry-After": "0.5"})
        mock_urlopen.side_effect = err_429

        with self.assertRaises(RateLimitError) as ctx:
            self.client.get("always-throttled")

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.retry_after_seconds, 0.5)
        # With max_retries_429 = 2, total attempts = 1 initial + 2 retries = 3
        self.assertEqual(mock_urlopen.call_count, 3)
        self.assertEqual(len(self.sleep_calls), 2)

    @patch("urllib.request.urlopen")
    def test_503_not_retried_by_default(self, mock_urlopen):
        err_503 = self._make_http_error(503, {"error": "unavailable"})
        mock_urlopen.side_effect = err_503

        with self.assertRaises(ServerError) as ctx:
            self.client.post("orders", json_data={}, allow_retry_503=False)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertEqual(len(self.sleep_calls), 0)

    @patch("urllib.request.urlopen")
    def test_503_retried_when_explicitly_allowed(self, mock_urlopen):
        err_503 = self._make_http_error(503, {"error": "timeout, retry with order_ref"})
        success_201 = self._make_http_response(201, {"charge_id": "chg_123", "status": "captured"})

        mock_urlopen.side_effect = [
            err_503,
            MagicMock(__enter__=MagicMock(return_value=success_201)),
        ]

        resp = self.client.post("terminal/charges", json_data={"order_ref": "REF1"}, allow_retry_503=True)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], "captured")
        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertTrue(len(self.sleep_calls) >= 1)

    @patch("urllib.request.urlopen")
    def test_socket_timeout_maps_to_timeout_error(self, mock_urlopen):
        mock_urlopen.side_effect = socket.timeout("timed out")

        with self.assertRaises(TimeoutError):
            self.client.get("slow-endpoint")

    @patch("urllib.request.urlopen")
    def test_connection_refused_maps_to_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError(ConnectionRefusedError("Connection refused"))

        with self.assertRaises(ConnectionError):
            self.client.get("unreachable")


if __name__ == "__main__":
    unittest.main()

