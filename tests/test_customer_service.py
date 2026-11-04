"""Unit tests for CustomerService."""

import unittest
from unittest.mock import MagicMock

from eluno_pos.adapters.exceptions import ServerError
from eluno_pos.domain.customer import Customer
from eluno_pos.services.customer_service import CustomerService


class TestCustomerService(unittest.TestCase):
    """Test suite for customer lookup operations."""

    def setUp(self) -> None:
        self.mock_shopify = MagicMock()
        self.service = CustomerService(shopify_client=self.mock_shopify)

    def test_successful_customer_lookup(self) -> None:
        # Seed data customer 80001005 (Karthik)
        self.mock_shopify.search_customer_by_phone.return_value = [
            {
                "id": 80001005,
                "first_name": "Karthik",
                "last_name": "Iyer",
                "phone": "+919000000005",
                "email": "karthik.iyer@example.test",
                "tags": "campaign:progressive_oct26,priority",
                "total_spent": "52485.00",
                "orders_count": 4,
                "eluno": {"home_store": "HSR"},
            }
        ]

        customer = self.service.find_by_phone("+919000000005")
        self.assertIsNotNone(customer)
        self.assertEqual(customer.id, 80001005)
        self.assertEqual(customer.full_name, "Karthik Iyer")
        self.assertEqual(customer.phone, "+919000000005")
        self.assertTrue(customer.is_priority)
        self.assertTrue(customer.has_tag("campaign:progressive_oct26"))
        self.assertEqual(customer.home_store, "HSR")

    def test_customer_not_found(self) -> None:
        self.mock_shopify.search_customer_by_phone.return_value = []

        customer = self.service.find_by_phone("+919999999999")
        self.assertIsNone(customer)

    def test_empty_phone_returns_none(self) -> None:
        customer = self.service.find_by_phone("   ")
        self.assertIsNone(customer)
        self.mock_shopify.search_customer_by_phone.assert_not_called()

    def test_api_error_bubbles_up(self) -> None:
        self.mock_shopify.search_customer_by_phone.side_effect = ServerError("Shopify down", status_code=500)

        with self.assertRaises(ServerError):
            self.service.find_by_phone("+919000000005")


if __name__ == "__main__":
    unittest.main()

