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
        # Data minimization verification: "rx" object must NOT be in properties
        self.assertNotIn("rx", lens_props)

    def test_prescription_data_minimization_shopify_payload(self) -> None:
        """Verify that Shopify line-item properties never leak clinical prescription data."""
        payload = self.service.build_order_payload(
            cart=self.cart,
            invoice=self.invoice,
            payment=self.payment,
            applied_offers=[],
            location_id=90001,
            store_code="HSR",
            transaction_date=date(2026, 10, 2),
        )

        for item in payload["line_items"]:
            prop_names = {p["name"] for p in item.get("properties", [])}
            # "rx" must never be sent to Shopify
            self.assertNotIn("rx", prop_names)

            # Neither OD, OS, SPH, CYL, AXIS, ADD, PD should exist as properties
            clinical_terms = {"od", "os", "sph", "cyl", "axis", "add", "pd"}
            for prop in item.get("properties", []):
                self.assertNotIn(prop["name"].lower(), clinical_terms)
                for term in clinical_terms:
                    self.assertNotIn(f'"{term}"', prop["value"].lower())

            # Only rx_id is allowed as a foreign reference
            if any(p["name"] == "line_type" and p["value"] == "lens" for p in item.get("properties", [])):
                self.assertIn("rx_id", prop_names)

    def test_exact_gst_paisa_reconciliation_at_5_percent(self) -> None:
        """Regression test: verify exact paisa reconciliation for ₹9,499.00 lens with ₹1,000.00 discount at 5% GST.

        Gross: ₹9,499.00 (949900 paise)
        Discount: ₹1,000.00 (100000 paise)
        Net Inclusive: ₹8,499.00 (849900 paise)
        Taxable: ₹8,094.29 (809429 paise)
        CGST: ₹202.36 (20236 paise, rate 0.025)
        SGST: ₹202.35 (20235 paise, rate 0.025)
        Total GST: ₹404.71 (40471 paise)
        Invariant: Taxable + CGST + SGST == Net Inclusive (8094.29 + 202.36 + 202.35 == 8499.00)
        """
        discounted_lens = CartLine(
            line_id="L_PGX",
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
            discount_allocations=[
                DiscountAllocation(offer_id="FREE_BLUECUT_PGX", offer_name="Free Bluecut", amount_paise=100000)
            ],
        )

        test_cart = Cart(lines=[discounted_lens], customer=self.customer)
        self.assertEqual(test_cart.net_total_paise, 849900)

        inv = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_gst_audit_5pct",
            customer_id=self.customer.id,
            gross_paise=949900,
            discount_paise=100000,
            net_paise=849900,
            taxable_paise=809429,
            tax_paise=40471,
            lines=[],
        )

        pay = PaymentRecord(
            checkout_ref=inv.checkout_ref,
            invoice_no=inv.invoice_no,
            order_ref="pay_gst_audit",
            amount_paise=849900,
            method="upi",
            status=PaymentStatus.CAPTURED,
            charge_id="chg_gst_audit",
        )

        applied = [
            AppliedOffer(
                offer_id="FREE_BLUECUT_PGX",
                name="Free Bluecut",
                discount_paise=100000,
                line_allocations={"L_PGX": 100000},
                description="₹1000 off",
            )
        ]

        payload = self.service.build_order_payload(
            cart=test_cart,
            invoice=inv,
            payment=pay,
            applied_offers=applied,
            location_id=90001,
            store_code="HSR",
            transaction_date=date(2026, 10, 20),
        )

        line_item = payload["line_items"][0]
        self.assertEqual(line_item["price"], "9499.00")
        self.assertEqual(line_item["total_discount"], "1000.00")

        tax_lines = {t["title"]: t for t in line_item["tax_lines"]}
        self.assertIn("CGST", tax_lines)
        self.assertIn("SGST", tax_lines)

        cgst = tax_lines["CGST"]
        sgst = tax_lines["SGST"]

        self.assertEqual(cgst["rate"], 0.025)
        self.assertEqual(sgst["rate"], 0.025)

        # EXACT PAISA RECONCILIATION:
        # CGST receives the odd paisa, SGST receives the remainder
        self.assertEqual(cgst["price"], "202.36")
        self.assertEqual(sgst["price"], "202.35")

        # Sum of tax components
        cgst_paise = round(float(cgst["price"]) * 100)
        sgst_paise = round(float(sgst["price"]) * 100)
        self.assertEqual(cgst_paise, 20236)
        self.assertEqual(sgst_paise, 20235)
        self.assertEqual(cgst_paise + sgst_paise, 40471)

        # Full line reconciliation
        taxable_paise = 849900 - (cgst_paise + sgst_paise)
        self.assertEqual(taxable_paise, 809429)
        self.assertEqual(taxable_paise + cgst_paise + sgst_paise, 849900)

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
