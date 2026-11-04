"""Customer service for looking up and retrieving customer details."""

from typing import Optional

from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.domain.customer import Customer


class CustomerService:
    """Service to search and manage customers via Shopify."""

    def __init__(self, shopify_client: ShopifyClient) -> None:
        self.shopify = shopify_client

    def find_by_phone(self, phone: str) -> Optional[Customer]:
        """Find a customer in Shopify by phone number.

        Returns:
            Customer domain entity if found, or None if no matching customer exists.
        Raises:
            ApiClientError if Shopify API returns an error or times out.
        """
        clean_phone = phone.strip()
        if not clean_phone:
            return None

        customers_data = self.shopify.search_customer_by_phone(clean_phone)
        if not customers_data:
            return None

        # Return the exact matching customer
        return Customer.from_shopify_dict(customers_data[0])

