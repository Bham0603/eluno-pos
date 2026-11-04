"""Shopify Admin REST API client adapter."""

from typing import Any, Dict, List, Optional

from eluno_pos.adapters.exceptions import (
    AmbiguousOperationError,
    ConnectionError,
    ServerError,
    TimeoutError,
)
from eluno_pos.adapters.http_client import HttpClient


class ShopifyClient:
    """Client for Shopify Admin REST API (version 2026-10)."""

    def __init__(self, http_client: HttpClient) -> None:
        self.http = http_client

    def search_customer_by_phone(self, phone: str) -> List[Dict[str, Any]]:
        """Find customer(s) by phone number."""
        query = f"phone:{phone}"
        resp = self.http.get("customers/search.json", params={"query": query})
        return resp.data.get("customers", [])

    def get_locations(self) -> List[Dict[str, Any]]:
        """List all store and warehouse locations."""
        resp = self.http.get("locations.json")
        return resp.data.get("locations", [])

    def get_products(
        self,
        product_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List products, optionally filtered by product_type (e.g. 'Frame', 'Lens')."""
        params: Dict[str, Any] = {"limit": limit}
        if product_type:
            params["product_type"] = product_type
        resp = self.http.get("products.json", params=params)
        return resp.data.get("products", [])

    def get_product(self, product_id: int) -> Dict[str, Any]:
        """Fetch a single product by ID."""
        resp = self.http.get(f"products/{product_id}.json")
        return resp.data.get("product", {})

    def get_variant(self, variant_id: int) -> Dict[str, Any]:
        """Fetch a single variant by ID."""
        resp = self.http.get(f"variants/{variant_id}.json")
        return resp.data.get("variant", {})

    def get_inventory_levels(
        self,
        inventory_item_ids: Optional[List[int]] = None,
        location_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch stock levels for specified inventory items and locations."""
        params: Dict[str, Any] = {}
        if inventory_item_ids:
            params["inventory_item_ids"] = inventory_item_ids
        if location_ids:
            params["location_ids"] = location_ids
        resp = self.http.get("inventory_levels.json", params=params)
        return resp.data.get("inventory_levels", [])

    def adjust_inventory_level(
        self,
        inventory_item_id: int,
        location_id: int,
        available_adjustment: int,
    ) -> Dict[str, Any]:
        """Adjust inventory stock up or down for an item at a location."""
        payload = {
            "inventory_item_id": inventory_item_id,
            "location_id": location_id,
            "available_adjustment": available_adjustment,
        }
        resp = self.http.post("inventory_levels/adjust.json", json_data=payload)
        return resp.data.get("inventory_level", {})

    def get_orders(
        self,
        created_at_min: Optional[str] = None,
        created_at_max: Optional[str] = None,
        location_id: Optional[int] = None,
        status: str = "any",
        financial_status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List orders with optional filtering by date range, store, and status."""
        params: Dict[str, Any] = {"status": status, "limit": limit}
        if created_at_min:
            params["created_at_min"] = created_at_min
        if created_at_max:
            params["created_at_max"] = created_at_max
        if location_id is not None:
            params["location_id"] = location_id
        if financial_status:
            params["financial_status"] = financial_status

        resp = self.http.get("orders.json", params=params)
        return resp.data.get("orders", [])

    def get_order(self, order_id: int) -> Dict[str, Any]:
        """Fetch a single order by ID."""
        resp = self.http.get(f"orders/{order_id}.json")
        return resp.data.get("order", {})

    def create_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new order in Shopify.

        CRITICAL IDEMPOTENCY / RECOVERY RULE:
        Shopify REST order creation is not idempotent. If this call experiences
        a timeout or ambiguous server error, it MUST NOT be blindly retried.
        We raise AmbiguousOperationError so the caller keeps the order in
        PENDING_RECONCILIATION and verifies existence before re-attempting.
        """
        payload = {"order": order_data}
        try:
            resp = self.http.post(
                "orders.json",
                json_data=payload,
                allow_retry_429=True,
                allow_retry_503=False,  # Never automatically retry 503 on order creation
            )
            return resp.data.get("order", {})
        except (TimeoutError, ServerError, ConnectionError) as err:
            raise AmbiguousOperationError(
                message=(
                    f"Shopify order creation had an ambiguous outcome ({type(err).__name__}: {err}). "
                    "Do not blindly retry; keep invoice in PENDING_RECONCILIATION."
                ),
                status_code=getattr(err, "status_code", None),
                response_body=getattr(err, "response_body", None),
                url=getattr(err, "url", None),
                method="POST",
            ) from err

    def create_transaction(
        self,
        order_id: int,
        transaction_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Record a transaction (sale or refund) against an order."""
        payload = {"transaction": transaction_data}
        resp = self.http.post(f"orders/{order_id}/transactions.json", json_data=payload)
        return resp.data.get("transaction", {})

    def calculate_refund(
        self,
        order_id: int,
        refund_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Pre-calculate a refund to verify refundable amounts and taxes."""
        payload = {"refund": refund_data}
        resp = self.http.post(f"orders/{order_id}/refunds/calculate.json", json_data=payload)
        return resp.data.get("refund", {})

    def create_refund(
        self,
        order_id: int,
        refund_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create an official refund and restock items in Shopify."""
        payload = {"refund": refund_data}
        resp = self.http.post(f"orders/{order_id}/refunds.json", json_data=payload)
        return resp.data.get("refund", {})

