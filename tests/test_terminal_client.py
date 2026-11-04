"""Unit tests for TerminalClient adapter."""

import unittest
from unittest.mock import MagicMock

from eluno_pos.adapters.exceptions import (
    AmbiguousOperationError,
    ConnectionError,
    ServerError,
    TimeoutError,
)
from eluno_pos.adapters.http_client import HttpResponse
from eluno_pos.adapters.terminal_client import TerminalClient


class TestTerminalClient(unittest.TestCase):
    """Test suite for payment terminal client."""

    def setUp(self) -> None:
        self.mock_http = MagicMock()
        self.client = TerminalClient(http_client=self.mock_http, max_retries_503=3)

    def test_create_charge_preserves_order_ref_and_allows_503_retry(self) -> None:
        self.mock_http.post.return_value = HttpResponse(
            status_code=201,
            data={
                "charge_id": "chg_99000001",
                "status": "captured",
                "amount_paise": 1907215,
                "method": "upi",
                "order_ref": "ELU/HSR/FY26/00099",
            },
            headers={},
        )

        res = self.client.create_charge(
            amount_paise=1907215,
            method="upi",
            order_ref="ELU/HSR/FY26/00099",
        )

        self.assertEqual(res["charge_id"], "chg_99000001")
        self.assertEqual(res["status"], "captured")
        self.assertEqual(res["order_ref"], "ELU/HSR/FY26/00099")

        # Verify that allow_retry_503 was True and order_ref preserved
        self.mock_http.post.assert_called_once_with(
            "charges",
            json_data={
                "amount_paise": 1907215,
                "method": "upi",
                "order_ref": "ELU/HSR/FY26/00099",
            },
            allow_retry_429=True,
            allow_retry_503=True,
            max_retries_503=3,
        )

    def test_create_charge_timeout_raises_ambiguous_operation_error(self) -> None:
        self.mock_http.post.side_effect = TimeoutError("Socket timeout on charge")

        with self.assertRaises(AmbiguousOperationError) as ctx:
            self.client.create_charge(1000, "upi", "REF_TIMEOUT")

        self.assertIn("ambiguous outcome", ctx.exception.message)
        self.assertIn("Do not automatically replay", ctx.exception.message)

    def test_create_charge_connection_error_raises_ambiguous_operation_error(self) -> None:
        self.mock_http.post.side_effect = ConnectionError("Connection reset by peer")

        with self.assertRaises(AmbiguousOperationError) as ctx:
            self.client.create_charge(1000, "upi", "REF_CONN_RESET")

        self.assertIn("ambiguous outcome", ctx.exception.message)

    def test_get_charge(self) -> None:
        self.mock_http.get.return_value = HttpResponse(
            status_code=200,
            data={"charge_id": "chg_123", "status": "captured"},
            headers={},
        )

        res = self.client.get_charge("chg_123")
        self.assertEqual(res["charge_id"], "chg_123")
        self.mock_http.get.assert_called_once_with("charges/chg_123")

    def test_create_refund(self) -> None:
        self.mock_http.post.return_value = HttpResponse(
            status_code=201,
            data={"refund_id": "trf_123", "status": "processed", "amount_paise": 50000},
            headers={},
        )

        res = self.client.create_refund(
            charge_id="chg_123",
            amount_paise=50000,
            reason="CUSTOMER_RETURN",
        )
        self.assertEqual(res["refund_id"], "trf_123")
        self.mock_http.post.assert_called_once_with(
            "refunds",
            json_data={
                "charge_id": "chg_123",
                "amount_paise": 50000,
                "reason": "CUSTOMER_RETURN",
            },
            allow_retry_429=True,
            allow_retry_503=False,
        )

    def test_create_refund_timeout_raises_ambiguous_operation_error(self) -> None:
        self.mock_http.post.side_effect = TimeoutError("Network timeout during refund")

        with self.assertRaises(AmbiguousOperationError) as ctx:
            self.client.create_refund("chg_123", 50000, "CUSTOMER_RETURN")

        self.assertIn("ambiguous outcome", ctx.exception.message)
        self.assertIn("reconcile by checking get_charge", ctx.exception.message)

    def test_create_refund_server_error_raises_ambiguous_operation_error(self) -> None:
        self.mock_http.post.side_effect = ServerError("HTTP 500 Server Error", status_code=500)

        with self.assertRaises(AmbiguousOperationError) as ctx:
            self.client.create_refund("chg_123", 50000, "CUSTOMER_RETURN")

        self.assertIn("ambiguous outcome", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()

