"""Tests for DayEndReportService store sales aggregation and multi-system reconciliation."""

from datetime import date
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.domain.invoice import InvoiceLine, InvoiceStatus
from eluno_pos.domain.payment import PaymentRecord, PaymentStatus
from eluno_pos.domain.refund import RefundRecord, RefundStatus
from eluno_pos.repositories.invoice_repository import InvoiceRepository
from eluno_pos.services.day_end_service import DayEndReportService
from eluno_pos.services.gst_service import GstService


class TestDayEndReportService(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_dayend.db")
        self.repo = InvoiceRepository(self.db_path)
        self.mock_shopify = MagicMock(spec=ShopifyClient)
        self.service = DayEndReportService(
            invoice_repository=self.repo,
            shopify_client=self.mock_shopify,
            gst_service=GstService(),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_line(self, line_id: str, line_type: str, price_paise: int, hsn: str) -> InvoiceLine:
        return InvoiceLine(
            line_id=line_id,
            line_type=line_type,
            variant_id=91000001,
            sku=f"SKU-{line_id}",
            title=f"Item {line_id}",
            quantity=1,
            unit_price_paise=price_paise,
            gross_paise=price_paise,
            discount_paise=0,
            net_paise=price_paise,
            hsn=hsn,
        )

    def test_day_end_financial_totals_and_refund_deduction(self) -> None:
        business_date = "2026-10-20"

        # 1. Posted Invoice 1: Frame (HSN 90031900, 18%) ₹10,000 net
        inv1 = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_hsr_01",
            customer_id=80001001,
            gross_paise=1000000,
            discount_paise=0,
            net_paise=1000000,
            taxable_paise=847458,
            tax_paise=152542,
            lines=[self._create_line("L1", "frame", 1000000, "90031900")],
        )
        self.repo.update_invoice_payment(inv1.invoice_no, "chg_01")
        self.repo.update_invoice_shopify_order(inv1.invoice_no, 95000001, InvoiceStatus.POSTED)
        self.repo.record_payment_attempt(
            PaymentRecord(
                checkout_ref=inv1.checkout_ref,
                invoice_no=inv1.invoice_no,
                order_ref=f"pay_{inv1.checkout_ref}",
                amount_paise=1000000,
                method="upi",
                status=PaymentStatus.CAPTURED,
                charge_id="chg_01",
            )
        )
        self.repo.record_shopify_intent(
            inv1.invoice_no,
            inv1.checkout_ref,
            {"note_attributes": [{"name": "channel", "value": "walk_in"}]},
        )

        # 2. Posted Invoice 2: Lens (HSN 90015000, 5%) ₹5,000 net
        inv2 = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_hsr_02",
            customer_id=80001002,
            gross_paise=500000,
            discount_paise=0,
            net_paise=500000,
            taxable_paise=476190,
            tax_paise=23810,
            lines=[self._create_line("L2", "lens", 500000, "90015000")],
        )
        self.repo.update_invoice_payment(inv2.invoice_no, "chg_02")
        self.repo.update_invoice_shopify_order(inv2.invoice_no, 95000002, InvoiceStatus.POSTED)
        self.repo.record_payment_attempt(
            PaymentRecord(
                checkout_ref=inv2.checkout_ref,
                invoice_no=inv2.invoice_no,
                order_ref=f"pay_{inv2.checkout_ref}",
                amount_paise=500000,
                method="card",
                status=PaymentStatus.CAPTURED,
                charge_id="chg_02",
            )
        )
        self.repo.record_shopify_intent(
            inv2.invoice_no,
            inv2.checkout_ref,
            {"note_attributes": [{"name": "channel", "value": "whatsapp_booked"}]},
        )

        # 3. Cancelled Invoice 3 (Pre-payment failure)
        inv3 = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_hsr_03_cancelled",
            customer_id=80001003,
            gross_paise=800000,
            discount_paise=0,
            net_paise=800000,
            taxable_paise=677966,
            tax_paise=122034,
            lines=[self._create_line("L3", "frame", 800000, "90031900")],
        )
        self.repo.update_invoice_status(inv3.invoice_no, InvoiceStatus.CANCELLED)

        # 4. Pending Reconciliation Invoice 4
        inv4 = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_hsr_04_pending",
            customer_id=80001004,
            gross_paise=300000,
            discount_paise=0,
            net_paise=300000,
            taxable_paise=254237,
            tax_paise=45763,
            lines=[self._create_line("L4", "frame", 300000, "90031900")],
        )

        # 5. Completed Refund on Invoice 1 for ₹10,000
        ref_rec = RefundRecord(
            refund_id="ref_dayend_01",
            credit_note_no="ELU/HSR/FY26/CN/00001",
            invoice_no=inv1.invoice_no,
            checkout_ref=inv1.checkout_ref,
            charge_id="chg_01",
            shopify_order_id=95000001,
            amount_paise=1000000,
            status=RefundStatus.COMPLETED,
            reason_code="CUSTOMER_RETURN",
            terminal_refund_id="trf_01",
            shopify_refund_id=98000001,
        )
        self.repo.record_refund_intent(ref_rec)
        self.repo.update_refund_completed(ref_rec.refund_id, 98000001)

        # Explicitly set created_at timestamps to target test business date
        with self.repo._get_connection() as conn:
            conn.execute("UPDATE invoices SET created_at = '2026-10-20T10:00:00+05:30';")
            conn.execute("UPDATE refunds SET created_at = '2026-10-20T16:00:00+05:30';")

        # Mock Shopify orders response matching the 2 posted orders
        self.mock_shopify.get_orders.return_value = [
            {"id": 95000001, "total_price": "10000.00", "created_at": f"{business_date}T10:00:00+05:30"},
            {"id": 95000002, "total_price": "5000.00", "created_at": f"{business_date}T14:30:00+05:30"},
        ]

        report = self.service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=business_date,
            reconcile_with_shopify=True,
        )

        # Sales bill count: only the 2 posted invoices
        self.assertEqual(report.posted_sales_count, 2)
        self.assertEqual(report.gross_sales_paise, 1500000)  # ₹15,000.00
        self.assertEqual(report.net_sales_paise, 1500000)

        # Refund count and amount
        self.assertEqual(report.refunds_count, 1)
        self.assertEqual(report.refunds_amount_paise, 1000000)  # ₹10,000.00
        # Net after refunds: ₹15,000 - ₹10,000 = ₹5,000
        self.assertEqual(report.net_sales_after_refunds_paise, 500000)

        # Tax calculations
        self.assertEqual(report.taxable_value_paise, 847458 + 476190)
        self.assertEqual(report.total_gst_paise, 152542 + 23810)

        # Exclusions
        self.assertEqual(report.cancelled_invoices_count, 1)
        self.assertEqual(report.pending_reconciliation_count, 1)

        # Payment methods split
        self.assertEqual(report.payment_method_totals.get("upi"), 1000000)
        self.assertEqual(report.payment_method_totals.get("card"), 500000)

        # Channels split
        self.assertEqual(report.channel_totals.get("walk_in"), 1000000)
        self.assertEqual(report.channel_totals.get("whatsapp_booked"), 500000)

        # HSN breakdown
        self.assertIn("90031900", report.hsn_totals)
        self.assertIn("90015000", report.hsn_totals)
        self.assertEqual(report.hsn_totals["90031900"].total_gst_paise, 152542)
        self.assertEqual(report.hsn_totals["90015000"].total_gst_paise, 23810)

        # Multi-system reconciliation
        self.assertTrue(report.is_reconciled)
        self.assertEqual(len(report.discrepancies), 0)

    def test_discrepancy_flagged_when_shopify_order_count_differs(self) -> None:
        business_date = "2026-10-20"

        # Posted Invoice
        inv = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_discrepancy",
            customer_id=80001001,
            gross_paise=1000000,
            discount_paise=0,
            net_paise=1000000,
            taxable_paise=847458,
            tax_paise=152542,
            lines=[self._create_line("L1", "frame", 1000000, "90031900")],
        )
        self.repo.update_invoice_payment(inv.invoice_no, "chg_disc_01")
        self.repo.update_invoice_shopify_order(inv.invoice_no, 95000001, InvoiceStatus.POSTED)

        with self.repo._get_connection() as conn:
            conn.execute("UPDATE invoices SET created_at = '2026-10-20T10:00:00+05:30';")

        # Shopify returns 0 orders (simulating cloud sync drop or discrepancy)
        self.mock_shopify.get_orders.return_value = []

        report = self.service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=business_date,
            reconcile_with_shopify=True,
        )

        self.assertFalse(report.is_reconciled)
        self.assertGreater(len(report.discrepancies), 0)
        self.assertIn("Bill count mismatch", report.discrepancies[0])


if __name__ == "__main__":
    unittest.main()
