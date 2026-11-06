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


    def test_get_orders_single_page_no_next_link(self) -> None:
        """Single page result with no Link header terminates immediately."""
        self.mock_http.get.return_value = HttpResponse(
            status_code=200,
            data={"orders": [{"id": 1001}, {"id": 1002}]},
            headers={},
        )
        orders = self.client.get_orders(
            created_at_min="2026-10-20T00:00:00+05:30",
            created_at_max="2026-10-20T23:59:59+05:30",
            location_id=70000002,
        )
        self.assertEqual(len(orders), 2)
        self.mock_http.get.assert_called_once()

    def test_get_orders_pagination_follows_next_link(self) -> None:
        """Two-page result: first page has Link rel=next, second has no next link."""
        page1_resp = HttpResponse(
            status_code=200,
            data={"orders": [{"id": 1001}]},
            headers={"Link": '<https://shopify.test/orders.json?page_info=abc>; rel="next"'},
        )
        page2_resp = HttpResponse(
            status_code=200,
            data={"orders": [{"id": 1002}]},
            headers={},
        )
        self.mock_http.get.side_effect = [page1_resp, page2_resp]

        orders = self.client.get_orders(
            created_at_min="2026-10-20T00:00:00+05:30",
            created_at_max="2026-10-20T23:59:59+05:30",
            location_id=70000002,
        )
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[0]["id"], 1001)
        self.assertEqual(orders[1]["id"], 1002)
        self.assertEqual(self.mock_http.get.call_count, 2)

        # Second call must use the cursor URL unchanged, without extra params
        second_call_args = self.mock_http.get.call_args_list[1]
        self.assertEqual(second_call_args[0][0], "https://shopify.test/orders.json?page_info=abc")
        # No params argument on the cursor call
        self.assertNotIn("params", second_call_args[1] if second_call_args[1] else {})

    def test_get_orders_link_header_case_insensitive(self) -> None:
        """Link header matching is case-insensitive."""
        page1_resp = HttpResponse(
            status_code=200,
            data={"orders": [{"id": 2001}]},
            headers={"LINK": '<https://shopify.test/orders.json?page_info=xyz>; rel="next"'},
        )
        page2_resp = HttpResponse(
            status_code=200,
            data={"orders": [{"id": 2002}]},
            headers={},
        )
        self.mock_http.get.side_effect = [page1_resp, page2_resp]

        orders = self.client.get_orders()
        self.assertEqual(len(orders), 2)
        self.assertEqual(self.mock_http.get.call_count, 2)

    def test_get_orders_pagination_loop_raises_error(self) -> None:
        """Repeated cursor URL raises RuntimeError instead of infinite loop."""
        cursor_url = "https://shopify.test/orders.json?page_info=loop"
        loop_resp = HttpResponse(
            status_code=200,
            data={"orders": [{"id": 3001}]},
            headers={"Link": f'<{cursor_url}>; rel="next"'},
        )
        self.mock_http.get.side_effect = [loop_resp, loop_resp]

        with self.assertRaises(RuntimeError) as ctx:
            self.client.get_orders()
        self.assertIn("pagination loop", str(ctx.exception).lower())

    def test_get_orders_later_page_failure_raises_error(self) -> None:
        """If a later page request fails, the error is raised (not partial results)."""
        from eluno_pos.adapters.exceptions import ServerError

        page1_resp = HttpResponse(
            status_code=200,
            data={"orders": [{"id": 4001}]},
            headers={"Link": '<https://shopify.test/orders.json?page_info=def>; rel="next"'},
        )
        self.mock_http.get.side_effect = [page1_resp, ServerError("HTTP 500", status_code=500)]

        with self.assertRaises(ServerError):
            self.client.get_orders()

    def test_get_orders_created_at_params_passed_correctly(self) -> None:
        """created_at_min and created_at_max are included in the first-page request params."""
        self.mock_http.get.return_value = HttpResponse(
            status_code=200,
            data={"orders": []},
            headers={},
        )
        self.client.get_orders(
            created_at_min="2026-10-20T00:00:00+05:30",
            created_at_max="2026-10-20T23:59:59+05:30",
            location_id=70000002,
            status="any",
        )
        call_params = self.mock_http.get.call_args[1]["params"]
        self.assertEqual(call_params["created_at_min"], "2026-10-20T00:00:00+05:30")
        self.assertEqual(call_params["created_at_max"], "2026-10-20T23:59:59+05:30")
        self.assertEqual(call_params["location_id"], 70000002)
        self.assertEqual(call_params["status"], "any")

    def test_get_orders_updated_at_params_passed_correctly(self) -> None:
        """updated_at_min and updated_at_max are included when provided."""
        self.mock_http.get.return_value = HttpResponse(
            status_code=200,
            data={"orders": []},
            headers={},
        )
        self.client.get_orders(
            updated_at_min="2026-10-20T00:00:00+05:30",
            updated_at_max="2026-10-20T23:59:59+05:30",
            location_id=70000002,
        )
        call_params = self.mock_http.get.call_args[1]["params"]
        self.assertEqual(call_params["updated_at_min"], "2026-10-20T00:00:00+05:30")
        self.assertEqual(call_params["updated_at_max"], "2026-10-20T23:59:59+05:30")
        self.assertNotIn("created_at_min", call_params)
        self.assertNotIn("created_at_max", call_params)


    def test_get_orders_status_param_passed_correctly(self) -> None:
        """status param is passed through to query params."""
        self.mock_http.get.return_value = HttpResponse(
            status_code=200,
            data={"orders": []},
            headers={},
        )
        self.client.get_orders(status="closed")
        call_params = self.mock_http.get.call_args[1]["params"]
        self.assertEqual(call_params["status"], "closed")


if __name__ == "__main__":
    unittest.main()

