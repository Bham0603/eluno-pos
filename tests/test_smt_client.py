"""Unit tests for SmtClient adapter."""

import unittest
from unittest.mock import MagicMock

from eluno_pos.adapters.http_client import HttpResponse
from eluno_pos.adapters.smt_client import SmtClient


class TestSmtClient(unittest.TestCase):
    """Test suite for SMT prescriptions and staff client."""

    def setUp(self) -> None:
        self.mock_http = MagicMock()
        self.client = SmtClient(http_client=self.mock_http)

    def test_get_prescriptions_by_phone(self) -> None:
        self.mock_http.get.return_value = HttpResponse(
            status_code=200,
            data={"prescriptions": [{"rx_id": "RX5001", "phone": "+919000000001", "valid_until": "2027-01-01"}]},
            headers={},
        )

        rxs = self.client.get_prescriptions_by_phone("+919000000001")
        self.assertEqual(len(rxs), 1)
        self.assertEqual(rxs[0]["rx_id"], "RX5001")
        self.mock_http.get.assert_called_once_with(
            "prescriptions",
            params={"phone": "+919000000001"},
        )

    def test_get_staff_with_store_filter(self) -> None:
        self.mock_http.get.return_value = HttpResponse(
            status_code=200,
            data={"staff": [{"staff_id": "S101", "name": "Nikhil", "store_code": "HSR"}]},
            headers={},
        )

        staff = self.client.get_staff(store_code="HSR")
        self.assertEqual(len(staff), 1)
        self.assertEqual(staff[0]["staff_id"], "S101")
        self.mock_http.get.assert_called_once_with(
            "staff",
            params={"store": "HSR"},
        )

    def test_get_staff_without_store_filter(self) -> None:
        self.mock_http.get.return_value = HttpResponse(
            status_code=200,
            data={"staff": [{"staff_id": "S101"}, {"staff_id": "S102"}]},
            headers={},
        )

        staff = self.client.get_staff()
        self.assertEqual(len(staff), 2)
        self.mock_http.get.assert_called_once_with("staff", params={})


if __name__ == "__main__":
    unittest.main()

