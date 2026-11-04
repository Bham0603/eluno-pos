"""Unit tests for ShopifyClient adapter."""

import unittest
from unittest.mock import MagicMock

from eluno_pos.adapters.exceptions import (
    AmbiguousOperationError,
    ConnectionError,
    ServerError,
    TimeoutError,
    ValidationError,
)
from eluno_pos.adapters.http_client import HttpResponse
from eluno_pos.adapters.shopify_client import ShopifyClient


class TestShopifyClient(unittest.TestCase):
    """Test suite for ShopifyClient adapter methods and resilience logic."""

    def setUp(self) -> None:
        self.mock_http = MagicMock()
        self.client = ShopifyClient(http_client=self.mock_http)

    def test_search_customer_by_phone(self) -> None:
        self.mock_http.get.return_value = HttpResponse(
            status_code=200,
            data={"customers": [{"id": 80001001, "phone": "+919000000001", "first_name": "Rohan"}]},
            headers={},
        )

        customers = self.client.search_customer_by_phone("+919000000001")
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0]["id"], 80001001)
        self.mock_http.get.assert_called_once_with(
            "customers/search.json",
            params={"query": "phone:+919000000001"},
        )

    def test_get_inventory_levels(self) -> None:
        self.mock_http.get.return_value = HttpResponse(
            status_code=200,
            data={"inventory_levels": [{"inventory_item_id": 92000001, "location_id": 70000001, "available": 5}]},
            headers={},
        )

        inv = self.client.get_inventory_levels(
            inventory_item_ids=[92000001],
            location_ids=[70000001],
        )
        self.assertEqual(len(inv), 1)
        self.assertEqual(inv[0]["available"], 5)
        self.mock_http.get.assert_called_once_with(
            "inventory_levels.json",
            params={"inventory_item_ids": [92000001], "location_ids": [70000001]},
        )

    def test_create_order_success(self) -> None:
        self.mock_http.post.return_value = HttpResponse(
            status_code=201,
            data={"order": {"id": 1001, "order_number": 501, "name": "#501"}},
            headers={},
        )

        order = self.client.create_order({"line_items": [{"variant_id": 91000001}]})
        self.assertEqual(order["id"], 1001)
        self.mock_http.post.assert_called_once_with(
            "orders.json",
            json_data={"order": {"line_items": [{"variant_id": 91000001}]}},
            allow_retry_429=True,
            allow_retry_503=False,  # Never automatically retry 503 on order creation
        )

    def test_create_order_timeout_raises_ambiguous_operation_error(self) -> None:
        self.mock_http.post.side_effect = TimeoutError("Read timed out")

        with self.assertRaises(AmbiguousOperationError) as ctx:
            self.client.create_order({"line_items": []})

        self.assertIn("ambiguous outcome", ctx.exception.message)
        self.assertIn("Do not blindly retry", ctx.exception.message)

    def test_create_order_server_error_raises_ambiguous_operation_error(self) -> None:
        self.mock_http.post.side_effect = ServerError("HTTP 500 Server Error", status_code=500)

        with self.assertRaises(AmbiguousOperationError) as ctx:
            self.client.create_order({"line_items": []})

        self.assertIn("ambiguous outcome", ctx.exception.message)

    def test_create_order_connection_error_raises_ambiguous_operation_error(self) -> None:
        self.mock_http.post.side_effect = ConnectionError("Connection reset by peer")

        with self.assertRaises(AmbiguousOperationError) as ctx:
            self.client.create_order({"line_items": []})

        self.assertIn("ambiguous outcome", ctx.exception.message)

    def test_create_order_validation_error_passes_through(self) -> None:
        self.mock_http.post.side_effect = ValidationError("Invalid payload", status_code=422)

        with self.assertRaises(ValidationError):
            self.client.create_order({"line_items": []})

    def test_calculate_and_create_refund(self) -> None:
        self.mock_http.post.return_value = HttpResponse(
            status_code=200,
            data={"refund": {"id": 991, "refund_line_items": []}},
            headers={},
        )

        calc = self.client.calculate_refund(1001, {"refund_line_items": []})
        self.assertEqual(calc["id"], 991)
        self.mock_http.post.assert_called_with(
            "orders/1001/refunds/calculate.json",
            json_data={"refund": {"refund_line_items": []}},
        )


if __name__ == "__main__":
    unittest.main()

