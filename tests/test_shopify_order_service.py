"""Tests for ShopifyOrderService payload generation, order creation and reconciliation."""

from datetime import date
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from eluno_pos.adapters.exceptions import AmbiguousOperationError
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.domain.cart import Cart, CartLine, DiscountAllocation
from eluno_pos.domain.customer import Customer
from eluno_pos.domain.invoice import Invoice, InvoiceLine, InvoiceStatus
from eluno_pos.domain.offer import AppliedOffer
from eluno_pos.domain.payment import PaymentRecord, PaymentStatus
from eluno_pos.repositories.invoice_repository import InvoiceRepository
from eluno_pos.services.gst_service import GstService
from eluno_pos.services.shopify_order_service import ShopifyOrderService


class TestShopifyOrderService(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_shopify_order.db")
        self.repo = InvoiceRepository(self.db_path)
        self.mock_shopify_client = MagicMock(spec=ShopifyClient)
        self.service = ShopifyOrderService(
            shopify_client=self.mock_shopify_client,
            invoice_repository=self.repo,
            gst_service=GstService(),
        )

        self.customer = Customer(
            id=80001001,
            first_name="Aarav",
            last_name="Sharma",
            phone="+919000000001",
            email="aarav.sharma@example.test",
            tags="priority",
            total_spent="25000.00",
            orders_count=2,
        )

        self.frame_line = CartLine(
            line_id="L1",
            line_type="frame",
            variant_id=91000003,
            sku="FR-0003",
            title="Eluno Atelier Rectangle 03",
            unit_price_paise=1099800,
            hsn="90031900",
            pair_ref="1",
            discount_allocations=[
                DiscountAllocation(offer_id="FESTIVE10", offer_name="Festive 10%", amount_paise=59980)
            ],
        )

        self.lens_line = CartLine(
            line_id="L2",
            line_type="lens",
            variant_id=91000083,
            sku="LN-0043",
            title="Progressive 1.60 Blue Cut",
            unit_price_paise=949900,
            hsn="90015000",
            pair_ref="1",
            lens_type="progressive",
            index=1.6,
            coating="blue_cut",
            rx_id="RX5007",
        )

        self.cart = Cart(lines=[self.frame_line, self.lens_line], customer=self.customer)

        self.invoice = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_shp_unit_1",
            customer_id=self.customer.id,
            gross_paise=self.cart.gross_total_paise,
            discount_paise=self.cart.total_discount_paise,
            net_paise=self.cart.net_total_paise,
            taxable_paise=1734898,
            tax_paise=254822,
            lines=[],
        )

        self.payment = PaymentRecord(
            checkout_ref=self.invoice.checkout_ref,
            invoice_no=self.invoice.invoice_no,
            order_ref="pay_chk_shp_unit_1",
            amount_paise=self.cart.net_total_paise,
            method="upi",
            status=PaymentStatus.CAPTURED,
            charge_id="chg_mock_9999",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_build_order_payload_structure(self) -> None:
        applied_offers = [
            AppliedOffer(
                offer_id="FESTIVE10",
                name="Festive 10%",
                discount_paise=59980,
                line_allocations={"L1": 59980},
                description="10% off frames",
            )
        ]

        payload = self.service.build_order_payload(
            cart=self.cart,
            invoice=self.invoice,
            payment=self.payment,
            applied_offers=applied_offers,
            location_id=90001,
            store_code="HSR",
            channel="whatsapp_booked",
            staff_id="S105",
            transaction_date=date(2026, 10, 2),
            prescription_map={"RX5007": {"od": {"sph": -3.25}, "os": {"sph": -3.5}, "pd": 60}},
        )

        self.assertEqual(payload["location_id"], 90001)
        self.assertTrue(payload["taxes_included"])
        self.assertEqual(payload["currency"], "INR")
        self.assertEqual(payload["customer"]["id"], 80001001)

        # Note attributes
        attrs = {a["name"]: a["value"] for a in payload["note_attributes"]}
        self.assertEqual(attrs["invoice_no"], "ELU/HSR/FY26/00001")
        self.assertEqual(attrs["checkout_ref"], "chk_shp_unit_1")
        self.assertEqual(attrs["terminal_charge_id"], "chg_mock_9999")
        self.assertEqual(attrs["store_code"], "HSR")
        self.assertEqual(attrs["channel"], "whatsapp_booked")
        self.assertEqual(attrs["staff_id"], "S105")

        # Transactions
        txs = payload["transactions"]
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0]["kind"], "sale")
        self.assertEqual(txs[0]["status"], "success")
        self.assertEqual(txs[0]["amount"], f"{self.cart.net_total_paise / 100:.2f}")
        self.assertEqual(txs[0]["authorization"], "chg_mock_9999")

        # Line items
        self.assertEqual(len(payload["line_items"]), 2)
        frame_item = payload["line_items"][0]
        self.assertEqual(frame_item["price"], "10998.00")
        self.assertEqual(frame_item["total_discount"], "599.80")
        self.assertEqual(len(frame_item["tax_lines"]), 2)

        lens_item = payload["line_items"][1]
        lens_props = {p["name"]: p["value"] for p in lens_item["properties"]}
        self.assertEqual(lens_props["line_type"], "lens")
        self.assertEqual(lens_props["rx_id"], "RX5007")
        self.assertIn("lens_config", lens_props)
        self.assertIn("rx", lens_props)

    def test_create_order_success_updates_invoice_to_posted(self) -> None:
        payload = {"line_items": []}
        self.mock_shopify_client.create_order.return_value = {
            "id": 99000101,
            "order_number": 1013,
            "name": "#1013",
        }

        created = self.service.create_order(
            payload=payload,
            invoice=self.invoice,
            location_id=90001,
        )

        self.assertEqual(created["id"], 99000101)
        self.assertEqual(created["order_number"], 1013)

        # Check DB invoice updated
        inv_db = self.repo.get_invoice(self.invoice.invoice_no)
        self.assertEqual(inv_db.status, InvoiceStatus.POSTED)
        self.assertEqual(inv_db.shopify_order_id, 99000101)

    def test_ambiguous_creation_reconciles_against_existing_orders(self) -> None:
        payload = {"line_items": []}
        # First call to create_order fails with AmbiguousOperationError
        self.mock_shopify_client.create_order.side_effect = AmbiguousOperationError(
            "Gateway timeout during order creation"
        )

        # Reconcile lookup returns the order that was actually created by Shopify
        self.mock_shopify_client.get_orders.return_value = [
            {
                "id": 99000105,
                "order_number": 1014,
                "note_attributes": [
                    {"name": "invoice_no", "value": self.invoice.invoice_no},
                    {"name": "checkout_ref", "value": self.invoice.checkout_ref},
                ],
            }
        ]

        # create_order should catch ambiguous error and successfully reconcile!
        created = self.service.create_order(
            payload=payload,
            invoice=self.invoice,
            location_id=90001,
        )

        self.assertEqual(created["id"], 99000105)
        inv_db = self.repo.get_invoice(self.invoice.invoice_no)
        self.assertEqual(inv_db.status, InvoiceStatus.POSTED)
        self.assertEqual(inv_db.shopify_order_id, 99000105)

    def test_ambiguous_creation_unresolved_leaves_pending_reconciliation(self) -> None:
        payload = {"line_items": []}
        self.mock_shopify_client.create_order.side_effect = AmbiguousOperationError(
            "Socket reset"
        )
        self.mock_shopify_client.get_orders.return_value = []  # Not found yet

        with self.assertRaises(AmbiguousOperationError):
            self.service.create_order(
                payload=payload,
                invoice=self.invoice,
                location_id=90001,
            )

        inv_db = self.repo.get_invoice(self.invoice.invoice_no)
        self.assertEqual(inv_db.status, InvoiceStatus.PENDING_RECONCILIATION)
        self.assertIsNone(inv_db.shopify_order_id)


if __name__ == "__main__":
    unittest.main()
