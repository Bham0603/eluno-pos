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

    def _create_line(
        self,
        line_id: str,
        line_type: str,
        price_paise: int,
        hsn: str,
        taxable_paise: int = 0,
        cgst_paise: int = 0,
        sgst_paise: int = 0,
        tax_paise: int = 0,
    ) -> InvoiceLine:
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
            taxable_paise=taxable_paise,
            cgst_paise=cgst_paise,
            sgst_paise=sgst_paise,
            tax_paise=tax_paise,
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
            lines=[self._create_line("L1", "frame", 1000000, "90031900", 847458, 76271, 76271, 152542)],
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
            lines=[self._create_line("L2", "lens", 500000, "90015000", 476190, 11905, 11905, 23810)],
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
            gross_paise=200000,
            discount_paise=0,
            net_paise=200000,
            taxable_paise=169492,
            tax_paise=30508,
            lines=[self._create_line("L3", "frame", 200000, "90031900", 169492, 15254, 15254, 30508)],
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
            lines=[self._create_line("L4", "frame", 300000, "90031900", 254237, 22882, 22881, 45763)],
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

        # Separate tax breakdown validation
        self.assertEqual(report.sales_taxable_value_paise, 847458 + 476190)
        self.assertEqual(report.sales_total_gst_paise, 152542 + 23810)
        self.assertEqual(report.refund_taxable_value_paise, 847458)
        self.assertEqual(report.refund_total_gst_paise, 152542)
        self.assertEqual(report.net_taxable_value_paise, 476190)
        self.assertEqual(report.net_total_gst_paise, 23810)

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
        self.assertEqual(report.hsn_totals["90031900"].sales_total_gst_paise, 152542)
        self.assertEqual(report.hsn_totals["90031900"].refund_total_gst_paise, 152542)
        self.assertEqual(report.hsn_totals["90031900"].net_total_gst_paise, 0)
        self.assertEqual(report.hsn_totals["90015000"].sales_total_gst_paise, 23810)
        self.assertEqual(report.hsn_totals["90015000"].net_total_gst_paise, 23810)

        # Multi-system reconciliation
        self.assertTrue(report.is_reconciled)
        self.assertEqual(len(report.discrepancies), 0)

    def test_same_date_sale_and_full_refund_with_multiple_hsns(self) -> None:
        """Requirement 3A, 3C, 3D: Sale and full refund on the same date with multiple HSNs."""
        business_date = "2026-10-20"

        # Multi-line invoice: Frame (HSN 90031900, 18%) ₹10,998 and Lens (HSN 90015000, 5%) ₹8,499
        frame_line = self._create_line("L1", "frame", 1099800, "90031900", 932034, 83883, 83883, 167766)
        lens_line = self._create_line("L2", "lens", 849900, "90015000", 809429, 20236, 20235, 40471)

        inv = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_multi_hsn",
            customer_id=80001005,
            gross_paise=1949700,
            discount_paise=0,
            net_paise=1949700,
            taxable_paise=1741463,
            tax_paise=208237,
            lines=[frame_line, lens_line],
        )
        self.repo.update_invoice_payment(inv.invoice_no, "chg_multi_hsn")
        self.repo.update_invoice_shopify_order(inv.invoice_no, 95000005, InvoiceStatus.POSTED)

        # Full refund on the same date
        ref = RefundRecord(
            refund_id="ref_multi_hsn_01",
            credit_note_no="ELU/HSR/FY26/CN/00001",
            invoice_no=inv.invoice_no,
            checkout_ref=inv.checkout_ref,
            charge_id="chg_multi_hsn",
            shopify_order_id=95000005,
            amount_paise=1949700,
            status=RefundStatus.COMPLETED,
            reason_code="CUSTOMER_RETURN",
            terminal_refund_id="trf_multi_01",
            shopify_refund_id=98000005,
        )
        self.repo.record_refund_intent(ref)
        self.repo.update_refund_completed(ref.refund_id, 98000005)

        with self.repo._get_connection() as conn:
            conn.execute("UPDATE invoices SET created_at = '2026-10-20T11:00:00+05:30';")
            conn.execute("UPDATE refunds SET created_at = '2026-10-20T17:00:00+05:30';")

        report = self.service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=business_date,
            reconcile_with_shopify=False,
        )

        # Net sales and net tax reconcile exactly to 0
        self.assertEqual(report.gross_sales_paise, 1949700)
        self.assertEqual(report.refunds_amount_paise, 1949700)
        self.assertEqual(report.net_sales_after_refunds_paise, 0)

        # Exact reversal of taxable value, CGST, and SGST
        self.assertEqual(report.sales_taxable_value_paise, 1741463)
        self.assertEqual(report.refund_taxable_value_paise, 1741463)
        self.assertEqual(report.net_taxable_value_paise, 0)

        self.assertEqual(report.sales_cgst_paise, 83883 + 20236)
        self.assertEqual(report.refund_cgst_paise, 83883 + 20236)
        self.assertEqual(report.net_cgst_paise, 0)

        self.assertEqual(report.sales_sgst_paise, 83883 + 20235)
        self.assertEqual(report.refund_sgst_paise, 83883 + 20235)
        self.assertEqual(report.net_sgst_paise, 0)

        # Per HSN net is zero
        hsn_frame = report.hsn_totals["90031900"]
        self.assertEqual(hsn_frame.sales_taxable_paise, 932034)
        self.assertEqual(hsn_frame.refund_taxable_paise, 932034)
        self.assertEqual(hsn_frame.net_taxable_paise, 0)
        self.assertEqual(hsn_frame.net_total_gst_paise, 0)

        hsn_lens = report.hsn_totals["90015000"]
        self.assertEqual(hsn_lens.sales_taxable_paise, 809429)
        self.assertEqual(hsn_lens.refund_taxable_paise, 809429)
        self.assertEqual(hsn_lens.net_taxable_paise, 0)
        self.assertEqual(hsn_lens.net_total_gst_paise, 0)

    def test_sale_on_one_date_and_full_refund_on_later_date(self) -> None:
        """Requirement 3B: Sale on Date 1 and refund on Date 2."""
        sale_date = "2026-10-20"
        refund_date = "2026-10-25"

        inv = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_cross_date",
            customer_id=80001006,
            gross_paise=1000000,
            discount_paise=0,
            net_paise=1000000,
            taxable_paise=847458,
            tax_paise=152542,
            lines=[self._create_line("L1", "frame", 1000000, "90031900", 847458, 76271, 76271, 152542)],
        )
        self.repo.update_invoice_payment(inv.invoice_no, "chg_cross_date")
        self.repo.update_invoice_shopify_order(inv.invoice_no, 95000006, InvoiceStatus.POSTED)

        ref = RefundRecord(
            refund_id="ref_cross_date_01",
            credit_note_no="ELU/HSR/FY26/CN/00001",
            invoice_no=inv.invoice_no,
            checkout_ref=inv.checkout_ref,
            charge_id="chg_cross_date",
            shopify_order_id=95000006,
            amount_paise=1000000,
            status=RefundStatus.COMPLETED,
            reason_code="CUSTOMER_RETURN",
            terminal_refund_id="trf_cross_01",
            shopify_refund_id=98000006,
        )
        self.repo.record_refund_intent(ref)
        self.repo.update_refund_completed(ref.refund_id, 98000006)

        with self.repo._get_connection() as conn:
            conn.execute("UPDATE invoices SET created_at = '2026-10-20T14:00:00+05:30';")
            conn.execute("UPDATE refunds SET created_at = '2026-10-25T11:00:00+05:30';")

        # Report on Sale Date (2026-10-20): Reflects sales only, no refund yet
        report_d1 = self.service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=sale_date,
            reconcile_with_shopify=False,
        )
        self.assertEqual(report_d1.posted_sales_count, 1)
        self.assertEqual(report_d1.gross_sales_paise, 1000000)
        self.assertEqual(report_d1.refunds_count, 0)
        self.assertEqual(report_d1.refunds_amount_paise, 0)
        self.assertEqual(report_d1.net_sales_after_refunds_paise, 1000000)

        # Report on Refund Date (2026-10-25): Reflects refund deduction against zero new sales
        report_d2 = self.service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=refund_date,
            reconcile_with_shopify=False,
        )
        self.assertEqual(report_d2.posted_sales_count, 0)
        self.assertEqual(report_d2.gross_sales_paise, 0)
        self.assertEqual(report_d2.refunds_count, 1)
        self.assertEqual(report_d2.refunds_amount_paise, 1000000)
        self.assertEqual(report_d2.net_sales_after_refunds_paise, -1000000)
        self.assertEqual(report_d2.refund_taxable_value_paise, 847458)
        self.assertEqual(report_d2.refund_total_gst_paise, 152542)
        self.assertEqual(report_d2.net_taxable_value_paise, -847458)
        self.assertEqual(report_d2.net_total_gst_paise, -152542)

    def test_asia_kolkata_timezone_date_boundary(self) -> None:
        """Requirement 3E: Strict Asia/Kolkata (UTC+05:30) business date boundary."""
        # 2026-10-20T18:45:00Z in UTC is 2026-10-21T00:15:00+05:30 in Asia/Kolkata
        inv = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_tz",
            customer_id=80001007,
            gross_paise=500000,
            discount_paise=0,
            net_paise=500000,
            taxable_paise=476190,
            tax_paise=23810,
            lines=[self._create_line("L1", "lens", 500000, "90015000", 476190, 11905, 11905, 23810)],
        )
        self.repo.update_invoice_payment(inv.invoice_no, "chg_tz")
        self.repo.update_invoice_shopify_order(inv.invoice_no, 95000007, InvoiceStatus.POSTED)

        with self.repo._get_connection() as conn:
            conn.execute("UPDATE invoices SET created_at = '2026-10-20T18:45:00Z';")

        # In Asia/Kolkata, this falls on 2026-10-21, NOT 2026-10-20
        rep_20 = self.service.generate_report("HSR", 70000002, "2026-10-20", reconcile_with_shopify=False)
        self.assertEqual(rep_20.posted_sales_count, 0)

        rep_21 = self.service.generate_report("HSR", 70000002, "2026-10-21", reconcile_with_shopify=False)
        self.assertEqual(rep_21.posted_sales_count, 1)
        self.assertEqual(rep_21.net_sales_paise, 500000)

    def test_no_duplicate_refund_deduction(self) -> None:
        """Requirement 3F: No duplicate refund deduction in day-end report."""
        business_date = "2026-10-20"
        inv = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_no_dup",
            customer_id=80001008,
            gross_paise=1000000,
            discount_paise=0,
            net_paise=1000000,
            taxable_paise=847458,
            tax_paise=152542,
            lines=[self._create_line("L1", "frame", 1000000, "90031900", 847458, 76271, 76271, 152542)],
        )
        self.repo.update_invoice_payment(inv.invoice_no, "chg_no_dup")
        self.repo.update_invoice_shopify_order(inv.invoice_no, 95000008, InvoiceStatus.POSTED)

        ref = RefundRecord(
            refund_id="ref_single_01",
            credit_note_no="ELU/HSR/FY26/CN/00001",
            invoice_no=inv.invoice_no,
            checkout_ref=inv.checkout_ref,
            charge_id="chg_no_dup",
            shopify_order_id=95000008,
            amount_paise=1000000,
            status=RefundStatus.COMPLETED,
            reason_code="CUSTOMER_RETURN",
            terminal_refund_id="trf_single_01",
            shopify_refund_id=98000008,
        )
        self.repo.record_refund_intent(ref)
        self.repo.update_refund_completed(ref.refund_id, 98000008)

        with self.repo._get_connection() as conn:
            conn.execute("UPDATE invoices SET created_at = '2026-10-20T10:00:00+05:30';")
            conn.execute("UPDATE refunds SET created_at = '2026-10-20T15:00:00+05:30';")

        report = self.service.generate_report("HSR", 70000002, business_date, reconcile_with_shopify=False)
        self.assertEqual(report.refunds_count, 1)
        self.assertEqual(report.refunds_amount_paise, 1000000)
        self.assertEqual(report.net_sales_after_refunds_paise, 0)

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
            lines=[self._create_line("L1", "frame", 1000000, "90031900", 847458, 76271, 76271, 152542)],
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
