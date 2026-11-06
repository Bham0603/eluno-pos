"""Unit tests for mock server Shopify order status filtering and parameter rejection."""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

# Add mock-apis to path for importing mock_server
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "starter-kit", "mock-apis")))
import mock_server


class TestMockServerStatusFiltering(unittest.TestCase):
    """Verifies Shopify status filtering rules and error handling directly against mock_server.H."""

    @classmethod
    def setUpClass(cls) -> None:
        mock_server.FLAKE = False
        cls.server = HTTPServer(("127.0.0.1", 0), mock_server.H)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}/admin/api/2026-10"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _req(self, method: str, path: str, data: dict = None):
        url = f"{self.base_url}/{path}"
        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_unsupported_status_values_rejected_with_http_400(self) -> None:
        """Unsupported status query values return HTTP 400 with an explicit error message."""
        for invalid_status in ["pending", "partially_paid", "archived", "draft", "random"]:
            status_code, data = self._req("GET", f"orders.json?status={invalid_status}")
            self.assertEqual(status_code, 400, f"Expected 400 for status={invalid_status}")
            self.assertIn("errors", data)
            self.assertIn("Invalid status", data["errors"])

    def test_all_four_status_filtering_rules(self) -> None:
        """Test status=open, status=closed, status=cancelled, and status=any."""
        # Mutate orders for lifecycle test:
        # Order 95000001: closed
        # Order 95000002: cancelled
        # Order 95000004: closed and cancelled
        # Order 95000003: open
        try:
            self._req("POST", "orders/95000001/close.json")
            self._req("POST", "orders/95000002/cancel.json")
            self._req("POST", "orders/95000004/close.json")
            self._req("POST", "orders/95000004/cancel.json")

            # 1. status=open: return orders that are not closed or cancelled
            code, data_open = self._req("GET", "orders.json?status=open")
            self.assertEqual(code, 200)
            open_ids = {o["id"] for o in data_open["orders"]}
            self.assertIn(95000003, open_ids)
            self.assertNotIn(95000001, open_ids)
            self.assertNotIn(95000002, open_ids)
            self.assertNotIn(95000004, open_ids)
            for o in data_open["orders"]:
                self.assertIsNone(o.get("closed_at"))
                self.assertIsNone(o.get("cancelled_at"))

            # 2. status=closed: return closed, non-cancelled orders
            code, data_closed = self._req("GET", "orders.json?status=closed")
            self.assertEqual(code, 200)
            closed_ids = {o["id"] for o in data_closed["orders"]}
            self.assertIn(95000001, closed_ids)
            self.assertNotIn(95000003, closed_ids)
            self.assertNotIn(95000002, closed_ids)
            self.assertNotIn(95000004, closed_ids)
            for o in data_closed["orders"]:
                self.assertIsNotNone(o.get("closed_at"))
                self.assertIsNone(o.get("cancelled_at"))

            # 3. status=cancelled: return cancelled orders
            code, data_cancelled = self._req("GET", "orders.json?status=cancelled")
            self.assertEqual(code, 200)
            cancelled_ids = {o["id"] for o in data_cancelled["orders"]}
            self.assertIn(95000002, cancelled_ids)
            self.assertIn(95000004, cancelled_ids)
            self.assertNotIn(95000003, cancelled_ids)
            self.assertNotIn(95000001, cancelled_ids)
            for o in data_cancelled["orders"]:
                self.assertIsNotNone(o.get("cancelled_at"))

            # 4. status=any: return all orders, including cancelled/archived
            code, data_any = self._req("GET", "orders.json?status=any")
            self.assertEqual(code, 200)
            any_ids = {o["id"] for o in data_any["orders"]}
            self.assertIn(95000001, any_ids)
            self.assertIn(95000002, any_ids)
            self.assertIn(95000003, any_ids)
            self.assertIn(95000004, any_ids)
            self.assertEqual(len(data_open["orders"]) + len(data_closed["orders"]) + len(data_cancelled["orders"]), len(data_any["orders"]))

            # 5. Default status when parameter is omitted must be 'open'
            code, data_default = self._req("GET", "orders.json")
            self.assertEqual(code, 200)
            default_ids = {o["id"] for o in data_default["orders"]}
            self.assertEqual(default_ids, open_ids)

        finally:
            self._req("POST", "orders/95000001/open.json")
            self._req("POST", "orders/95000002/open.json")
            self._req("POST", "orders/95000004/open.json")


if __name__ == "__main__":
    unittest.main()

