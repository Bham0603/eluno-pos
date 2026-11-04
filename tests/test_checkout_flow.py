"""Comprehensive end-to-end checkout orchestration, failure and recovery tests."""

from datetime import date
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from eluno_pos.adapters.exceptions import AmbiguousOperationError
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.adapters.terminal_client import TerminalClient
from eluno_pos.domain.cart import Cart, CartLine
from eluno_pos.domain.customer import Customer
from eluno_pos.domain.invoice import InvoiceStatus
from eluno_pos.repositories.invoice_repository import InvoiceRepository
from eluno_pos.services.checkout_service import CheckoutService
from eluno_pos.services.gst_service import GstService
from eluno_pos.services.offer_service import OfferConflictStrategy, OfferService
from eluno_pos.services.payment_service import PaymentService
from eluno_pos.services.shopify_order_service import ShopifyOrderService


class TestCheckoutFlow(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_checkout.db")
        self.repo = InvoiceRepository(self.db_path)

        self.mock_terminal_client = MagicMock(spec=TerminalClient)
        self.mock_shopify_client = MagicMock(spec=ShopifyClient)

        self.payment_service = PaymentService(
            terminal_client=self.mock_terminal_client,
            invoice_repository=self.repo,
        )
        self.shopify_order_service = ShopifyOrderService(
            shopify_client=self.mock_shopify_client,
            invoice_repository=self.repo,
            gst_service=GstService(),
        )
        self.checkout_service = CheckoutService(
            invoice_repository=self.repo,
            payment_service=self.payment_service,
            shopify_order_service=self.shopify_order_service,
            offer_service=OfferService(),
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

        self.frame = CartLine(
            line_id="L1",
            line_type="frame",
            variant_id=91000003,
            sku="FR-0003",
            title="Eluno Atelier Rectangle 03",
            unit_price_paise=1099800,
            hsn="90031900",
            pair_ref="PAIR-1",
        )

        self.lens = CartLine(
            line_id="L2",
            line_type="lens",
            variant_id=91000083,
            sku="LN-0043",
            title="Progressive 1.60 Blue Cut",
            unit_price_paise=949900,
            hsn="90015000",
            pair_ref="PAIR-1",
            lens_type="progressive",
            index=1.6,
            coating="blue_cut",
            rx_id="RX5007",
        )

        self.cart = Cart(lines=[self.frame, self.lens], customer=self.customer)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_end_to_end_successful_checkout(self) -> None:
        self.mock_terminal_client.create_charge.return_value = {
            "charge_id": "chg_term_001",
            "status": "captured",
            "amount_paise": 1869700,
            "method": "upi",
            "order_ref": "pay_chk_ref",
        }

        self.mock_shopify_client.create_order.return_value = {
            "id": 99000201,
            "order_number": 1020,
            "name": "#1020",
        }

        session = self.checkout_service.prepare_checkout(
            cart=self.cart,
            store_code="HSR",
            location_id=90001,
            financial_year="FY26",
            channel="walk_in",
            staff_id="S105",
            transaction_date=date(2026, 10, 20),
            offer_strategy=OfferConflictStrategy.MAX_SAVINGS,
        )

        self.assertEqual(session.invoice.invoice_no, "ELU/HSR/FY26/00001")
        self.assertEqual(session.invoice.status, InvoiceStatus.PENDING_RECONCILIATION)

        result = self.checkout_service.complete_checkout(session, payment_method="upi")

        self.assertTrue(result.success)
        self.assertEqual(result.invoice_no, "ELU/HSR/FY26/00001")
        self.assertEqual(result.status, InvoiceStatus.POSTED)
        self.assertEqual(result.terminal_charge_id, "chg_term_001")
        self.assertEqual(result.shopify_order_id, 99000201)
        self.assertEqual(result.shopify_order_number, 1020)

        # In DB
        db_inv = self.repo.get_invoice("ELU/HSR/FY26/00001")
        self.assertEqual(db_inv.status, InvoiceStatus.POSTED)
        self.assertEqual(db_inv.shopify_order_id, 99000201)
        self.assertEqual(db_inv.terminal_charge_id, "chg_term_001")

    def test_pre_payment_failure_cancels_invoice_and_does_not_reuse_seq(self) -> None:
        self.mock_terminal_client.create_charge.side_effect = ValueError("Card declined by bank")

        session1 = self.checkout_service.prepare_checkout(
            cart=self.cart,
            store_code="HSR",
            location_id=90001,
            financial_year="FY26",
        )
        self.assertEqual(session1.invoice.invoice_no, "ELU/HSR/FY26/00001")

        result1 = self.checkout_service.complete_checkout(session1, payment_method="card")

        self.assertFalse(result1.success)
        self.assertEqual(result1.status, InvoiceStatus.CANCELLED)

        # Verify DB marks CANCELLED
        db_inv1 = self.repo.get_invoice("ELU/HSR/FY26/00001")
        self.assertEqual(db_inv1.status, InvoiceStatus.CANCELLED)
        self.assertIsNone(db_inv1.terminal_charge_id)

        # Allocate second checkout at same store: MUST be 00002, NOT reusing 00001!
        session2 = self.checkout_service.prepare_checkout(
            cart=self.cart,
            store_code="HSR",
            location_id=90001,
            financial_year="FY26",
        )
        self.assertEqual(session2.invoice.invoice_no, "ELU/HSR/FY26/00002")

    def test_post_payment_shopify_timeout_remains_pending_reconciliation_and_recovers(self) -> None:
        # Step 1: Terminal capture succeeds
        self.mock_terminal_client.create_charge.return_value = {
            "charge_id": "chg_term_captured_safe",
            "status": "captured",
            "amount_paise": 1869700,
            "method": "upi",
            "order_ref": "pay_chk_test",
        }

        # Step 2: Shopify order creation times out
        self.mock_shopify_client.create_order.side_effect = AmbiguousOperationError(
            "Shopify API timed out during order creation"
        )
        # Immediate reconciliation in Shopify get_orders does not find it initially
        self.mock_shopify_client.get_orders.return_value = []

        session = self.checkout_service.prepare_checkout(
            cart=self.cart,
            store_code="HSR",
            location_id=90001,
            financial_year="FY26",
        )

        result = self.checkout_service.complete_checkout(session, payment_method="upi")

        # Must report failure to complete, but invoice MUST remain PENDING_RECONCILIATION
        self.assertFalse(result.success)
        self.assertTrue(result.is_pending_reconciliation)
        self.assertEqual(result.status, InvoiceStatus.PENDING_RECONCILIATION)
        self.assertEqual(result.terminal_charge_id, "chg_term_captured_safe")

        # Invariant check: cannot cancel!
        with self.assertRaises(ValueError):
            self.repo.update_invoice_status("ELU/HSR/FY26/00001", InvoiceStatus.CANCELLED)

        # Step 3: Reconciliation later (or upon recovery check)
        self.mock_shopify_client.get_orders.return_value = [
            {
                "id": 99000205,
                "order_number": 1025,
                "note_attributes": [
                    {"name": "invoice_no", "value": "ELU/HSR/FY26/00001"},
                    {"name": "checkout_ref", "value": session.checkout_ref},
                ],
            }
        ]

        rec_result = self.checkout_service.reconcile_pending_checkout(
            invoice_no="ELU/HSR/FY26/00001",
            location_id=90001,
        )

        self.assertTrue(rec_result.success)
        self.assertEqual(rec_result.status, InvoiceStatus.POSTED)
        self.assertEqual(rec_result.shopify_order_id, 99000205)

        # DB is updated to POSTED
        final_inv = self.repo.get_invoice("ELU/HSR/FY26/00001")
        self.assertEqual(final_inv.status, InvoiceStatus.POSTED)
        self.assertEqual(final_inv.shopify_order_id, 99000205)

    def test_recovery_across_application_restart(self) -> None:
        # 1. First run: complete payment, but simulate process kill right before Shopify call
        inv = self.repo.allocate_invoice(
            store_code="IND",
            financial_year="FY26",
            checkout_ref="chk_restart_test",
            customer_id=self.customer.id,
            gross_paise=1000000,
            discount_paise=0,
            net_paise=1000000,
            taxable_paise=847458,
            tax_paise=152542,
            lines=[],
        )
        self.repo.update_invoice_payment(inv.invoice_no, "chg_pre_crash_999")

        # 2. Simulate application crash & restart: create fresh repository & services pointing to same DB
        new_repo = InvoiceRepository(self.db_path)
        new_shopify_order_service = ShopifyOrderService(
            shopify_client=self.mock_shopify_client,
            invoice_repository=new_repo,
            gst_service=GstService(),
        )
        new_checkout_service = CheckoutService(
            invoice_repository=new_repo,
            payment_service=PaymentService(self.mock_terminal_client, new_repo),
            shopify_order_service=new_shopify_order_service,
            offer_service=OfferService(),
            gst_service=GstService(),
        )

        # Verify new instance finds pending invoice
        pending = new_repo.list_pending_invoices(store_code="IND")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].invoice_no, "ELU/IND/FY26/00001")
        self.assertEqual(pending[0].terminal_charge_id, "chg_pre_crash_999")

        # Simulate finding the order during recovery reconciliation
        self.mock_shopify_client.get_orders.return_value = [
            {
                "id": 99000300,
                "order_number": 1030,
                "note_attributes": [
                    {"name": "invoice_no", "value": "ELU/IND/FY26/00001"},
                    {"name": "checkout_ref", "value": "chk_restart_test"},
                ],
            }
        ]

        rec_result = new_checkout_service.reconcile_pending_checkout("ELU/IND/FY26/00001", location_id=90002)
        self.assertTrue(rec_result.success)
        self.assertEqual(rec_result.status, InvoiceStatus.POSTED)
        self.assertEqual(rec_result.shopify_order_id, 99000300)

        # No pending invoices remain
        self.assertEqual(len(new_repo.list_pending_invoices(store_code="IND")), 0)


if __name__ == "__main__":
    unittest.main()

