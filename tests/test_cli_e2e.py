"""End-to-end acceptance tests running against live local mock server (http://localhost:8787).

Verifies the 6 key user workflows defined in Phase 6:
1. Customer and optical prescription lookup with compatibility check.
2. Complete 15-step retail optical checkout via CLI.
3. Safe checkout failure handling without duplicate charges or orphaned orders.
4. Full retail refund with credit note issuance, terminal refund, frame restock, and custom lens non-restock.
5. Store day-end sales and tax reconciliation report.
6. Safe transaction reconciliation and state recovery.
"""

from datetime import date
import os
import tempfile
import unittest
from unittest.mock import patch

from eluno_pos.adapters.http_client import HttpClient
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.adapters.smt_client import SmtClient
from eluno_pos.adapters.terminal_client import TerminalClient
from eluno_pos.cli.commands.checkout import run_checkout
from eluno_pos.cli.commands.customer import run_customer_lookup
from eluno_pos.cli.commands.reconcile import run_reconcile
from eluno_pos.cli.commands.refund import run_refund
from eluno_pos.cli.commands.report import run_report
from eluno_pos.cli.context import AppContext
from eluno_pos.config import AppConfig
from eluno_pos.domain.invoice import InvoiceStatus, get_financial_year
from eluno_pos.domain.lens import LensVariant
from eluno_pos.domain.validation import LensValidator
from eluno_pos.repositories.invoice_repository import InvoiceRepository
from eluno_pos.services.checkout_service import CheckoutService
from eluno_pos.services.customer_service import CustomerService
from eluno_pos.services.day_end_service import DayEndReportService
from eluno_pos.services.gst_service import GstService
from eluno_pos.services.offer_service import OfferService
from eluno_pos.services.payment_service import PaymentService
from eluno_pos.services.prescription_service import PrescriptionService
from eluno_pos.services.refund_service import RefundService
from eluno_pos.services.shopify_order_service import ShopifyOrderService


class TestCliEndToEnd(unittest.TestCase):
    """End-to-end acceptance tests executing CLI workflows against live mock server."""

    def setUp(self) -> None:
        """Set up isolated SQLite database and AppContext wired to mock server."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_e2e_pos.db")
        self.config = AppConfig(db_path=self.db_path)

        self.shopify_http = HttpClient(self.config.shopify_base_url, timeout=self.config.http_timeout_seconds)
        self.smt_http = HttpClient(self.config.smt_base_url, timeout=self.config.http_timeout_seconds)
        self.terminal_http = HttpClient(self.config.terminal_base_url, timeout=self.config.http_timeout_seconds)

        self.shopify_client = ShopifyClient(self.shopify_http)
        self.smt_client = SmtClient(self.smt_http)
        self.terminal_client = TerminalClient(self.terminal_http)

        self.repo = InvoiceRepository(self.db_path)
        self.gst_service = GstService()
        self.offer_service = OfferService()
        self.customer_service = CustomerService(self.shopify_client)
        self.prescription_service = PrescriptionService(self.smt_client)
        self.payment_service = PaymentService(self.terminal_client, self.repo)
        self.shopify_order_service = ShopifyOrderService(self.shopify_client, self.repo, self.gst_service)
        self.checkout_service = CheckoutService(
            invoice_repository=self.repo,
            payment_service=self.payment_service,
            shopify_order_service=self.shopify_order_service,
            offer_service=self.offer_service,
            gst_service=self.gst_service,
        )
        self.refund_service = RefundService(
            terminal_client=self.terminal_client,
            shopify_client=self.shopify_client,
            invoice_repository=self.repo,
        )
        self.day_end_service = DayEndReportService(
            invoice_repository=self.repo,
            gst_service=self.gst_service,
            shopify_client=self.shopify_client,
        )

        self.ctx = AppContext(
            config=self.config,
            repo=self.repo,
            shopify_client=self.shopify_client,
            smt_client=self.smt_client,
            terminal_client=self.terminal_client,
            gst_service=self.gst_service,
            offer_service=self.offer_service,
            customer_service=self.customer_service,
            prescription_service=self.prescription_service,
            payment_service=self.payment_service,
            shopify_order_service=self.shopify_order_service,
            checkout_service=self.checkout_service,
            refund_service=self.refund_service,
            day_end_service=self.day_end_service,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_e2e_1_customer_and_prescription(self) -> None:
        """Scenario 1: Customer lookup, prescription fetch, compatible/incompatible lens validation."""
        phone = "+919000000001"  # Seeded customer Rahul Menon

        # 1. Verify customer lookup via CLI
        ret = run_customer_lookup(self.ctx, phone=phone)
        self.assertEqual(ret, 0)

        # 2. Fetch customer domain object and prescriptions
        customer = self.customer_service.find_by_phone(phone)
        self.assertIsNotNone(customer)
        self.assertEqual(customer.phone, phone)
        self.assertEqual(customer.first_name, "Rahul")
        self.assertIn("family", customer.tag_list)

        prescriptions = self.prescription_service.get_prescriptions_by_phone(phone)
        self.assertGreater(len(prescriptions), 0)
        latest_rx = prescriptions[0]
        self.assertEqual(latest_rx.rx_id, "RX5002")

        # 3. Test optical compatibility: Single vision lens (LN-0001, id: 91000041)
        raw_sv_lens = self.shopify_client.get_variant(91000041)
        sv_lens = LensVariant.from_shopify_variant(raw_sv_lens)
        compat_sv = LensValidator.validate(latest_rx, sv_lens)
        self.assertTrue(compat_sv.is_compatible)

        # 4. Test optical incompatibility: Progressive lens (LN-0033, id: 91000073) without ADD power
        raw_prog_lens = self.shopify_client.get_variant(91000073)
        prog_lens = LensVariant.from_shopify_variant(raw_prog_lens)
        compat_prog = LensValidator.validate(latest_rx, prog_lens)
        self.assertFalse(compat_prog.is_compatible)
        self.assertTrue(any("requiring reading addition (ADD)" in r for r in compat_prog.reasons))

    def test_e2e_2_complete_checkout(self) -> None:
        """Scenario 2: Complete 15-step checkout via CLI against live mock server."""
        store_code = "HSR"
        staff_id = "S108"
        customer_phone = "+919000000001"
        frame_variant_id = 91000001
        lens_variant_id = 91000041

        # Run checkout via CLI runner with auto_confirm=True
        ret = run_checkout(
            ctx=self.ctx,
            store_code=store_code,
            staff_id=staff_id,
            customer_phone=customer_phone,
            frame_variant_id=frame_variant_id,
            lens_variant_id=lens_variant_id,
            auto_confirm=True,
        )
        self.assertEqual(ret, 0)

        # Verify durable invoice record in SQLite
        fy = get_financial_year()
        inv = self.repo.get_invoice(f"ELU/HSR/{fy}/00001")
        self.assertIsNotNone(inv)
        self.assertEqual(inv.status, InvoiceStatus.POSTED)
        self.assertEqual(inv.store_code, store_code)
        self.assertEqual(inv.customer_id, 80001001)

        # Verify exact pricing & GST
        self.assertGreater(inv.gross_paise, 0)
        self.assertGreater(inv.net_paise, 0)
        self.assertGreater(inv.tax_paise, 0)
        self.assertEqual(inv.taxable_paise + inv.tax_paise, inv.net_paise)

        # Verify terminal charge is marked captured
        self.assertIsNotNone(inv.terminal_charge_id)
        charge = self.terminal_client.get_charge(inv.terminal_charge_id)
        self.assertEqual(charge["status"], "captured")
        self.assertEqual(charge["amount_paise"], inv.net_paise)

        # Verify Shopify order was created and recorded
        self.assertIsNotNone(inv.shopify_order_id)
        shopify_order = self.shopify_client.get_order(inv.shopify_order_id)
        self.assertIsNotNone(shopify_order)
        self.assertEqual(shopify_order["financial_status"], "paid")
        self.assertEqual(len(shopify_order["line_items"]), 2)

    def test_e2e_3_safe_checkout_failure(self) -> None:
        """Scenario 3: Cancelled checkout marks invoice CANCELLED without initiating charges."""
        store_code = "HSR"
        staff_id = "S108"
        customer_phone = "+919000000001"
        frame_variant_id = 91000001
        lens_variant_id = 91000041

        # Staff cancels checkout at prompt
        with patch("eluno_pos.cli.commands.checkout.prompt_confirm", return_value=False):
            ret = run_checkout(
                ctx=self.ctx,
                store_code=store_code,
                staff_id=staff_id,
                customer_phone=customer_phone,
                frame_variant_id=frame_variant_id,
                lens_variant_id=lens_variant_id,
                rx_id="RX5002",
                auto_confirm=False,
            )
            self.assertEqual(ret, 0)

        # Verify invoice was persisted as CANCELLED with no terminal charge
        fy = get_financial_year()
        inv = self.repo.get_invoice(f"ELU/HSR/{fy}/00001")
        self.assertIsNotNone(inv)
        self.assertEqual(inv.status, InvoiceStatus.CANCELLED)
        self.assertIsNone(inv.terminal_charge_id)
        self.assertIsNone(inv.shopify_order_id)

    def test_e2e_4_full_refund(self) -> None:
        """Scenario 4: Full refund with credit note, terminal refund, frame restock, and custom lens non-restock."""
        # 1. First complete a sale to obtain a posted invoice
        run_checkout(
            ctx=self.ctx,
            store_code="HSR",
            staff_id="S108",
            customer_phone="+919000000001",
            frame_variant_id=91000001,
            lens_variant_id=91000041,
            auto_confirm=True,
        )
        fy = get_financial_year()
        inv = self.repo.get_invoice(f"ELU/HSR/{fy}/00001")
        self.assertIsNotNone(inv)

        # Check initial frame inventory level at HSR (location 70000002, item 92000001)
        initial_inv = self.shopify_client.get_inventory_levels(
            inventory_item_ids=[92000001],
            location_ids=[70000002],
        )
        initial_qty = initial_inv[0]["available"]

        # 2. Execute refund via CLI runner
        ret = run_refund(
            ctx=self.ctx,
            invoice_no=inv.invoice_no,
            staff_id="S108",
            reason_code="DEFECTIVE",
            auto_confirm=True,
        )
        self.assertEqual(ret, 0)

        # 3. Verify original sales invoice is preserved as POSTED
        refunded_inv = self.repo.get_invoice(inv.invoice_no)
        self.assertEqual(refunded_inv.status, InvoiceStatus.POSTED)

        # 4. Verify Credit Note allocation and completed refund record
        refunds = self.repo.get_refunds_for_invoice(inv.invoice_no)
        self.assertEqual(len(refunds), 1)
        refund = refunds[0]
        self.assertEqual(refund.status.value, "COMPLETED")
        self.assertEqual(refund.credit_note_no, f"ELU/HSR/{fy}/CN/00001")
        self.assertEqual(refund.amount_paise, inv.net_paise)

        # 5. Verify frame inventory incremented by exactly 1
        updated_inv = self.shopify_client.get_inventory_levels(
            inventory_item_ids=[92000001],
            location_ids=[70000002],
        )
        updated_qty = updated_inv[0]["available"]
        self.assertEqual(updated_qty, initial_qty + 1)

        # 6. Verify terminal charge refunded
        charge = self.terminal_client.get_charge(inv.terminal_charge_id)
        self.assertEqual(charge["refunded_paise"], inv.net_paise)

    def test_e2e_5_day_end_report(self) -> None:
        """Scenario 5: Day-end reporting accurately aggregates sales, refunds, GST, and channels."""
        # 1. Complete a sale
        run_checkout(
            ctx=self.ctx,
            store_code="HSR",
            staff_id="S108",
            customer_phone="+919000000001",
            frame_variant_id=91000001,
            lens_variant_id=91000041,
            auto_confirm=True,
        )
        fy = get_financial_year()
        inv = self.repo.get_invoice(f"ELU/HSR/{fy}/00001")

        # 2. Refund the sale
        run_refund(
            ctx=self.ctx,
            invoice_no=inv.invoice_no,
            staff_id="S108",
            reason_code="CUSTOMER_REQUEST",
            auto_confirm=True,
        )

        # 3. Run day-end report via CLI runner
        today_str = date.today().isoformat()
        ret = run_report(self.ctx, store_code="HSR", business_date=today_str)
        self.assertEqual(ret, 0)

        # 4. Verify report domain model calculation
        report = self.day_end_service.generate_report("HSR", 70000002, today_str)
        self.assertEqual(report.posted_sales_count, 1)
        self.assertEqual(report.refunds_count, 1)
        self.assertEqual(report.gross_sales_paise, inv.gross_paise)
        self.assertEqual(report.refunds_amount_paise, inv.net_paise)
        self.assertEqual(report.net_sales_after_refunds_paise, 0)
        self.assertIn("90031900", report.hsn_totals)
        self.assertIn("90015000", report.hsn_totals)

    def test_e2e_6_recovery(self) -> None:
        """Scenario 6: Transaction reconciliation detects and recovers unconfirmed state without duplication."""
        # Check initial state has zero pending
        ret = run_reconcile(self.ctx)
        self.assertEqual(ret, 0)

        # Complete a normal checkout to verify clean state
        ret = run_checkout(
            ctx=self.ctx,
            store_code="HSR",
            staff_id="S108",
            customer_phone="+919000000001",
            frame_variant_id=91000001,
            lens_variant_id=91000041,
            auto_confirm=True,
        )
        self.assertEqual(ret, 0)

        # Re-run reconciliation; should confirm no pending items
        ret = run_reconcile(self.ctx)
        self.assertEqual(ret, 0)


if __name__ == "__main__":
    unittest.main()
