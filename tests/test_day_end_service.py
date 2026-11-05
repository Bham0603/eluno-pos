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

        # Mock Shopify orders response matching the 2 posted orders with realistic payloads
        self.mock_shopify.get_orders.return_value = [
            {
                "id": 95000001,
                "location_id": 70000002,
                "financial_status": "refunded",
                "subtotal_price": "10000.00",
                "total_discounts": "0.00",
                "total_price": "10000.00",
                "created_at": f"{business_date}T10:00:00+05:30",
                "note_attributes": [
                    {"name": "payment_method", "value": "upi"},
                    {"name": "channel", "value": "walk_in"},
                ],
                "line_items": [
                    {
                        "id": 96000001,
                        "sku": "SKU-L1",
                        "price": "10000.00",
                        "quantity": 1,
                        "total_discount": "0.00",
                        "properties": [{"name": "hsn", "value": "90031900"}, {"name": "line_type", "value": "frame"}],
                        "tax_lines": [
                            {"title": "CGST", "price": "762.71"},
                            {"title": "SGST", "price": "762.71"},
                        ],
                    }
                ],
                "refunds": [
                    {
                        "id": 98000001,
                        "order_id": 95000001,
                        "created_at": f"{business_date}T16:00:00+05:30",
                        "transactions": [{"kind": "refund", "status": "success", "amount": "10000.00"}],
                        "refund_line_items": [
                            {
                                "line_item_id": 96000001,
                                "quantity": 1,
                                "subtotal": "8474.58",
                                "total_tax": "1525.42",
                            }
                        ],
                    }
                ],
            },
            {
                "id": 95000002,
                "location_id": 70000002,
                "financial_status": "paid",
                "subtotal_price": "5000.00",
                "total_discounts": "0.00",
                "total_price": "5000.00",
                "created_at": f"{business_date}T14:30:00+05:30",
                "note_attributes": [
                    {"name": "payment_method", "value": "card"},
                    {"name": "channel", "value": "whatsapp_booked"},
                ],
                "line_items": [
                    {
                        "id": 96000002,
                        "sku": "SKU-L2",
                        "price": "5000.00",
                        "quantity": 1,
                        "total_discount": "0.00",
                        "properties": [{"name": "hsn", "value": "90015000"}, {"name": "line_type", "value": "lens"}],
                        "tax_lines": [
                            {"title": "CGST", "price": "119.05"},
                            {"title": "SGST", "price": "119.05"},
                        ],
                    }
                ],
                "refunds": [],
            },
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

    def test_phase4_database_migration_and_refunded_invoice_retained_in_gross_sales(self) -> None:
        """Requirement 2: Verify safe Phase 4 SQLite schema migration, consistent invoice enums,
        and proof that a refunded original invoice remains represented in gross sales with its
        refund separately deducted.
        """
        import sqlite3

        migrated_db = os.path.join(self.temp_dir.name, "phase4_legacy.db")

        # 1. Create a pure Phase 4 legacy SQLite schema without Phase 5 tables or columns
        raw_conn = sqlite3.connect(migrated_db)
        raw_conn.execute("""
            CREATE TABLE invoices (
                invoice_no TEXT PRIMARY KEY,
                store_code TEXT NOT NULL,
                financial_year TEXT NOT NULL,
                seq INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('POSTED', 'PENDING_RECONCILIATION', 'CANCELLED')),
                customer_id INTEGER,
                gross_paise INTEGER NOT NULL CHECK (gross_paise >= 0),
                discount_paise INTEGER NOT NULL CHECK (discount_paise >= 0),
                net_paise INTEGER NOT NULL CHECK (net_paise >= 0),
                taxable_paise INTEGER NOT NULL CHECK (taxable_paise >= 0),
                tax_paise INTEGER NOT NULL CHECK (tax_paise >= 0),
                checkout_ref TEXT UNIQUE NOT NULL,
                terminal_charge_id TEXT UNIQUE,
                shopify_order_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (store_code, financial_year, seq)
            );
        """)
        raw_conn.execute("""
            CREATE TABLE invoice_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_no TEXT NOT NULL,
                line_id TEXT NOT NULL,
                line_type TEXT NOT NULL,
                variant_id INTEGER NOT NULL,
                sku TEXT NOT NULL,
                title TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_price_paise INTEGER NOT NULL CHECK (unit_price_paise >= 0),
                gross_paise INTEGER NOT NULL CHECK (gross_paise >= 0),
                discount_paise INTEGER NOT NULL CHECK (discount_paise >= 0),
                net_paise INTEGER NOT NULL CHECK (net_paise >= 0),
                hsn TEXT NOT NULL,
                pair_ref TEXT,
                rx_id TEXT,
                lens_config TEXT,
                properties_json TEXT,
                FOREIGN KEY(invoice_no) REFERENCES invoices(invoice_no) ON DELETE RESTRICT
            );
        """)
        raw_conn.execute("""
            CREATE TABLE invoice_sequences (
                store_code TEXT NOT NULL,
                financial_year TEXT NOT NULL,
                next_seq INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (store_code, financial_year)
            );
        """)
        raw_conn.execute("""
            CREATE TABLE payment_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checkout_ref TEXT NOT NULL,
                invoice_no TEXT NOT NULL,
                order_ref TEXT UNIQUE NOT NULL,
                amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
                method TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('INITIATED', 'CAPTURED', 'FAILED', 'UNKNOWN')),
                charge_id TEXT UNIQUE,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(invoice_no) REFERENCES invoices(invoice_no) ON DELETE RESTRICT
            );
        """)
        raw_conn.execute("""
            CREATE TABLE shopify_order_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_no TEXT UNIQUE NOT NULL,
                checkout_ref TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMPLETED', 'AMBIGUOUS', 'RECONCILED')),
                shopify_order_id INTEGER,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(invoice_no) REFERENCES invoices(invoice_no) ON DELETE RESTRICT
            );
        """)

        # Insert a Phase 4 posted sale
        raw_conn.execute("""
            INSERT INTO invoices (
                invoice_no, store_code, financial_year, seq, status,
                customer_id, gross_paise, discount_paise, net_paise,
                taxable_paise, tax_paise, checkout_ref, terminal_charge_id,
                shopify_order_id, created_at, updated_at
            ) VALUES (
                'ELU/HSR/FY26/00088', 'HSR', 'FY26', 88, 'POSTED',
                80001001, 1000000, 0, 1000000, 847458, 152542,
                'chk_mig_088', 'chg_mig_088', 95000088,
                '2026-10-25T11:00:00+05:30', '2026-10-25T11:00:00+05:30'
            );
        """)
        raw_conn.execute("""
            INSERT INTO invoice_lines (
                invoice_no, line_id, line_type, variant_id, sku, title,
                quantity, unit_price_paise, gross_paise, discount_paise, net_paise, hsn
            ) VALUES (
                'ELU/HSR/FY26/00088', 'L_MIG_1', 'frame', 91000001, 'SKU-FRAME', 'Migrated Frame',
                1, 1000000, 1000000, 0, 1000000, '90031900'
            );
        """)
        raw_conn.commit()
        raw_conn.close()

        # 2. Instantiate Phase 5 InvoiceRepository against the Phase 4 database
        # This triggers automatic safe schema migration in _init_db
        migrated_repo = InvoiceRepository(migrated_db)

        # Verify new tables and columns now exist and can be queried
        with migrated_repo._get_connection() as conn:
            cur = conn.execute("PRAGMA table_info(invoice_lines);")
            col_names = {row["name"] for row in cur.fetchall()}
            self.assertIn("taxable_paise", col_names)
            self.assertIn("cgst_paise", col_names)
            self.assertIn("sgst_paise", col_names)
            self.assertIn("tax_paise", col_names)

            # Verify Phase 5 tables were created
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row["name"] for row in cur.fetchall()}
            self.assertIn("credit_note_sequences", tables)
            self.assertIn("refunds", tables)
            self.assertIn("restock_intents", tables)

        # 3. Read the migrated invoice
        migrated_inv = migrated_repo.get_invoice("ELU/HSR/FY26/00088")
        self.assertIsNotNone(migrated_inv)
        self.assertEqual(migrated_inv.status, InvoiceStatus.POSTED)

        # 4. Record a full refund for this invoice
        ref = RefundRecord(
            refund_id="ref_mig_088",
            credit_note_no="ELU/HSR/FY26/CN/00008",
            invoice_no=migrated_inv.invoice_no,
            checkout_ref=migrated_inv.checkout_ref,
            charge_id=migrated_inv.terminal_charge_id,
            shopify_order_id=migrated_inv.shopify_order_id,
            amount_paise=migrated_inv.net_paise,
            status=RefundStatus.COMPLETED,
            reason_code="CUSTOMER_RETURN",
            staff_id="STF-01",
            terminal_refund_id="trf_mig_088",
            shopify_refund_id=98000088,
            baseline_refunded_paise=0,
            error_message=None,
            created_at="2026-10-25T14:00:00+05:30",
            updated_at="2026-10-25T14:00:00+05:30",
        )
        migrated_repo.record_refund_intent(ref)
        with migrated_repo._get_connection() as conn:
            conn.execute("UPDATE refunds SET created_at = '2026-10-25T14:00:00+05:30' WHERE refund_id = 'ref_mig_088';")

        # 5. Verify the invoice in invoices table remains POSTED!
        # Indian GST compliance: Posted tax invoices are immutable. The refund is maintained
        # separately in the refunds table as a Credit Note.
        inv_after_refund = migrated_repo.get_invoice(migrated_inv.invoice_no)
        self.assertEqual(inv_after_refund.status, InvoiceStatus.POSTED)

        # 6. Generate Day-End Report on the date of sale and refund
        day_end_svc = DayEndReportService(
            invoice_repository=migrated_repo,
            shopify_client=self.mock_shopify,
            gst_service=GstService(),
        )
        report = day_end_svc.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date="2026-10-25",
            reconcile_with_shopify=False,
        )

        # 7. Verification:
        # Gross sales contains the original invoice (₹10,000)
        self.assertEqual(report.gross_sales_paise, 1000000)
        self.assertEqual(report.posted_sales_count, 1)

        # Refund sales contains the credit note (₹10,000)
        self.assertEqual(report.refunds_amount_paise, 1000000)
        self.assertEqual(report.refunds_count, 1)

        # Net sales is ₹0
        self.assertEqual(report.net_sales_after_refunds_paise, 0)
        self.assertEqual(report.net_taxable_value_paise, 0)
        self.assertEqual(report.net_total_gst_paise, 0)

        # HSN breakdown shows gross sales tax and refund tax separately
        self.assertIn("90031900", report.hsn_totals)
        hsn_stat = report.hsn_totals["90031900"]
        self.assertEqual(hsn_stat.sales_taxable_paise, 847458)
        self.assertEqual(hsn_stat.refund_taxable_paise, 847458)
        self.assertEqual(hsn_stat.net_taxable_paise, 0)

    def test_shopify_order_payloads_determine_financial_totals_when_sqlite_differs(self) -> None:
        """Requirement 1: Shopify order payloads determine financial figures even when SQLite totals deliberately differ."""
        business_date = "2026-10-20"

        # 1. Local SQLite has only 1 invoice for ₹5,000
        inv = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_local_diff",
            customer_id=80001001,
            gross_paise=500000,
            discount_paise=0,
            net_paise=500000,
            taxable_paise=476190,
            tax_paise=23810,
            lines=[self._create_line("L_LOCAL", "lens", 500000, "90015000", 476190, 11905, 11905, 23810)],
        )
        self.repo.update_invoice_payment(inv.invoice_no, "chg_local_diff")
        self.repo.update_invoice_shopify_order(inv.invoice_no, 95000099, InvoiceStatus.POSTED)
        with self.repo._get_connection() as conn:
            conn.execute("UPDATE invoices SET created_at = '2026-10-20T10:00:00+05:30';")

        # 2. Shopify Admin API returns 2 orders totaling ₹20,000 gross and ₹2,000 discounts (₹18,000 net)
        self.mock_shopify.get_orders.return_value = [
            {
                "id": 95000101,
                "location_id": 70000002,
                "financial_status": "paid",
                "subtotal_price": "10000.00",
                "total_discounts": "1000.00",
                "total_price": "9000.00",
                "created_at": f"{business_date}T11:00:00+05:30",
                "note_attributes": [
                    {"name": "payment_method", "value": "upi"},
                    {"name": "channel", "value": "walk_in"},
                ],
                "line_items": [
                    {
                        "id": 96000101,
                        "sku": "FR-0001",
                        "price": "10000.00",
                        "quantity": 1,
                        "total_discount": "1000.00",
                        "properties": [{"name": "hsn", "value": "90031900"}, {"name": "line_type", "value": "frame"}],
                        "tax_lines": [
                            {"title": "CGST", "price": "686.44"},
                            {"title": "SGST", "price": "686.44"},
                        ],
                    }
                ],
                "refunds": [],
            },
            {
                "id": 95000102,
                "location_id": 70000002,
                "financial_status": "paid",
                "subtotal_price": "10000.00",
                "total_discounts": "1000.00",
                "total_price": "9000.00",
                "created_at": f"{business_date}T15:00:00+05:30",
                "note_attributes": [
                    {"name": "payment_method", "value": "card"},
                    {"name": "channel", "value": "walk_in"},
                ],
                "line_items": [
                    {
                        "id": 96000102,
                        "sku": "LN-0001",
                        "price": "10000.00",
                        "quantity": 1,
                        "total_discount": "1000.00",
                        "properties": [{"name": "hsn", "value": "90015000"}, {"name": "line_type", "value": "lens"}],
                        "tax_lines": [
                            {"title": "CGST", "price": "214.29"},
                            {"title": "SGST", "price": "214.29"},
                        ],
                    }
                ],
                "refunds": [],
            },
        ]

        report = self.service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=business_date,
            reconcile_with_shopify=True,
        )

        # Financial totals must come from Shopify orders, NOT local SQLite!
        self.assertEqual(report.posted_sales_count, 2)
        self.assertEqual(report.gross_sales_paise, 2000000)      # ₹20,000.00 (from Shopify)
        self.assertEqual(report.total_discounts_paise, 200000)   # ₹2,000.00 (from Shopify)
        self.assertEqual(report.net_sales_paise, 1800000)        # ₹18,000.00 (from Shopify)

        # Taxable and GST values from Shopify line items
        self.assertEqual(report.sales_cgst_paise, 68644 + 21429)
        self.assertEqual(report.sales_sgst_paise, 68644 + 21429)
        self.assertEqual(report.sales_total_gst_paise, (68644 + 21429) * 2)

        # Discrepancies flagged due to intentional difference from SQLite (1 vs 2 bills)
        self.assertFalse(report.is_reconciled)
        self.assertTrue(any("Bill count mismatch" in d for d in report.discrepancies))
        self.assertTrue(any("Net sales mismatch" in d for d in report.discrepancies))

    def test_cross_date_refund_from_older_order_included_in_today_report(self) -> None:
        """Requirement 4 & 5: A refund on the target business date against an older order is included."""
        sale_date = "2026-10-10"
        refund_date = "2026-10-20"

        # Older order created on 2026-10-10, with refund created on 2026-10-20
        older_order_with_refund = {
            "id": 95000201,
            "location_id": 70000002,
            "financial_status": "refunded",
            "subtotal_price": "12998.00",
            "total_discounts": "0.00",
            "total_price": "12998.00",
            "created_at": f"{sale_date}T11:00:00+05:30",
            "note_attributes": [
                {"name": "payment_method", "value": "upi"},
                {"name": "channel", "value": "walk_in"},
            ],
            "line_items": [
                {
                    "id": 96000201,
                    "sku": "FR-0001",
                    "price": "12998.00",
                    "quantity": 1,
                    "total_discount": "0.00",
                    "properties": [{"name": "hsn", "value": "90031900"}, {"name": "line_type", "value": "frame"}],
                    "tax_lines": [
                        {"title": "CGST", "price": "991.37"},
                        {"title": "SGST", "price": "991.37"},
                    ],
                }
            ],
            "refunds": [
                {
                    "id": 98000201,
                    "order_id": 95000201,
                    "created_at": f"{refund_date}T15:30:00+05:30",
                    "transactions": [
                        {"kind": "refund", "status": "success", "amount": "12998.00"}
                    ],
                    "refund_line_items": [
                        {
                            "line_item_id": 96000201,
                            "quantity": 1,
                            "subtotal": "11015.26",
                            "total_tax": "1982.74",
                        }
                    ],
                }
            ],
        }

        self.mock_shopify.get_orders.return_value = [older_order_with_refund]

        # Generate report for refund_date (2026-10-20)
        report = self.service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=refund_date,
            reconcile_with_shopify=True,
        )

        # Sale was on 2026-10-10, so 0 posted sales for today
        self.assertEqual(report.posted_sales_count, 0)
        self.assertEqual(report.gross_sales_paise, 0)
        self.assertEqual(report.net_sales_paise, 0)

        # Refund was on 2026-10-20, so exactly 1 refund counted!
        self.assertEqual(report.refunds_count, 1)
        self.assertEqual(report.refunds_amount_paise, 1299800)
        self.assertEqual(report.net_sales_after_refunds_paise, -1299800)

        # Refund tax reversal correctly extracted from refund line items
        self.assertEqual(report.refund_taxable_value_paise, 1101526)
        self.assertEqual(report.refund_cgst_paise, 99137)
        self.assertEqual(report.refund_sgst_paise, 99137)
        self.assertEqual(report.refund_total_gst_paise, 198274)

        # HSN breakdown captures refund reversal
        self.assertIn("90031900", report.hsn_totals)
        hsn_stat = report.hsn_totals["90031900"]
        self.assertEqual(hsn_stat.refund_taxable_paise, 1101526)
        self.assertEqual(hsn_stat.refund_total_gst_paise, 198274)
        self.assertEqual(hsn_stat.net_taxable_paise, -1101526)
        self.assertEqual(hsn_stat.net_total_gst_paise, -198274)

    def test_pending_and_cancelled_orders_excluded_from_sales(self) -> None:
        """Requirement 6: Pending/unpaid orders are not counted as successful sales."""
        business_date = "2026-10-20"

        self.mock_shopify.get_orders.return_value = [
            # 1. Paid order (Valid)
            {
                "id": 95000301,
                "location_id": 70000002,
                "financial_status": "paid",
                "subtotal_price": "10000.00",
                "total_discounts": "0.00",
                "total_price": "10000.00",
                "created_at": f"{business_date}T10:00:00+05:30",
                "cancelled_at": None,
                "line_items": [
                    {
                        "id": 96000301,
                        "price": "10000.00",
                        "quantity": 1,
                        "total_discount": "0.00",
                        "tax_lines": [{"title": "CGST", "price": "762.71"}, {"title": "SGST", "price": "762.71"}],
                    }
                ],
                "refunds": [],
            },
            # 2. Pending/unpaid order (Excluded)
            {
                "id": 95000302,
                "location_id": 70000002,
                "financial_status": "pending",
                "subtotal_price": "5000.00",
                "total_discounts": "0.00",
                "total_price": "5000.00",
                "created_at": f"{business_date}T12:00:00+05:30",
                "cancelled_at": None,
                "line_items": [{"id": 96000302, "price": "5000.00", "quantity": 1, "total_discount": "0.00", "tax_lines": []}],
                "refunds": [],
            },
            # 3. Cancelled/voided order (Excluded)
            {
                "id": 95000303,
                "location_id": 70000002,
                "financial_status": "paid",
                "subtotal_price": "8000.00",
                "total_discounts": "0.00",
                "total_price": "8000.00",
                "created_at": f"{business_date}T14:00:00+05:30",
                "cancelled_at": f"{business_date}T14:05:00+05:30",
                "line_items": [{"id": 96000303, "price": "8000.00", "quantity": 1, "total_discount": "0.00", "tax_lines": []}],
                "refunds": [],
            },
        ]

        report = self.service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=business_date,
            reconcile_with_shopify=True,
        )

        # Only the 1 paid order is included
        self.assertEqual(report.posted_sales_count, 1)
        self.assertEqual(report.gross_sales_paise, 1000000)
        self.assertEqual(report.net_sales_paise, 1000000)

    def test_shopify_unavailability_triggers_sqlite_fallback_and_warning(self) -> None:
        """Requirement 7 & 8: Local operational counts remain available and Shopify unavailability triggers fallback."""
        business_date = "2026-10-20"

        # 1. Local SQLite has 1 posted invoice, 1 cancelled invoice, and 1 pending invoice
        inv = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_fallback_01",
            customer_id=80001001,
            gross_paise=1000000,
            discount_paise=0,
            net_paise=1000000,
            taxable_paise=847458,
            tax_paise=152542,
            lines=[self._create_line("L_FB", "frame", 1000000, "90031900", 847458, 76271, 76271, 152542)],
        )
        self.repo.update_invoice_payment(inv.invoice_no, "chg_fb_01")
        self.repo.update_invoice_shopify_order(inv.invoice_no, 95000401, InvoiceStatus.POSTED)

        inv_canc = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_fallback_canc",
            customer_id=80001002,
            gross_paise=200000,
            discount_paise=0,
            net_paise=200000,
            taxable_paise=169492,
            tax_paise=30508,
            lines=[],
        )
        self.repo.update_invoice_status(inv_canc.invoice_no, InvoiceStatus.CANCELLED)

        inv_pend = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_fallback_pend",
            customer_id=80001003,
            gross_paise=300000,
            discount_paise=0,
            net_paise=300000,
            taxable_paise=254237,
            tax_paise=45763,
            lines=[],
        )

        with self.repo._get_connection() as conn:
            conn.execute("UPDATE invoices SET created_at = '2026-10-20T10:00:00+05:30';")

        # 2. Simulate Shopify API failure / network timeout
        self.mock_shopify.get_orders.side_effect = ConnectionError("Upstream Shopify Gateway Timeout")

        report = self.service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=business_date,
            reconcile_with_shopify=True,
        )

        # Fallback to SQLite completes successfully
        self.assertEqual(report.posted_sales_count, 1)
        self.assertEqual(report.gross_sales_paise, 1000000)
        self.assertEqual(report.net_sales_paise, 1000000)

        # Operational metrics remain accessible from SQLite
        self.assertEqual(report.cancelled_invoices_count, 1)
        self.assertEqual(report.pending_reconciliation_count, 1)

        # Report marked not reconciled with explicit fallback warning
        self.assertFalse(report.is_reconciled)
        self.assertTrue(any("offline fallback" in d.lower() or "unavailable" in d.lower() for d in report.discrepancies))

    def test_hsn_breakdown_and_payment_methods_extracted_from_shopify(self) -> None:
        """Requirement 3: HSN breakdown and payment method are extracted from Shopify data."""
        business_date = "2026-10-20"

        self.mock_shopify.get_orders.return_value = [
            {
                "id": 95000501,
                "location_id": 70000002,
                "financial_status": "paid",
                "subtotal_price": "13997.00",
                "total_discounts": "1299.80",
                "total_price": "12697.20",
                "created_at": f"{business_date}T10:00:00+05:30",
                "note_attributes": [
                    {"name": "payment_method", "value": "upi"},
                    {"name": "channel", "value": "walk_in"},
                ],
                "line_items": [
                    {
                        "id": 96000501,
                        "sku": "FR-0001",
                        "price": "12998.00",
                        "quantity": 1,
                        "total_discount": "1299.80",
                        "properties": [{"name": "hsn", "value": "90031900"}, {"name": "line_type", "value": "frame"}],
                        "tax_lines": [
                            {"title": "CGST", "price": "892.24"},
                            {"title": "SGST", "price": "892.23"},
                        ],
                    },
                    {
                        "id": 96000502,
                        "sku": "LN-0001",
                        "price": "999.00",
                        "quantity": 1,
                        "total_discount": "0.00",
                        "properties": [{"name": "hsn", "value": "90015000"}, {"name": "line_type", "value": "lens"}],
                        "tax_lines": [
                            {"title": "CGST", "price": "23.79"},
                            {"title": "SGST", "price": "23.78"},
                        ],
                    },
                ],
                "refunds": [],
            }
        ]

        report = self.service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=business_date,
            reconcile_with_shopify=True,
        )

        # Payment method extracted from note_attributes
        self.assertEqual(report.payment_method_totals.get("upi"), 1269720)
        self.assertEqual(report.channel_totals.get("walk_in"), 1269720)

        # HSN breakdown extracted from line properties
        self.assertIn("90031900", report.hsn_totals)
        self.assertIn("90015000", report.hsn_totals)

        hsn_frame = report.hsn_totals["90031900"]
        self.assertEqual(hsn_frame.sales_cgst_paise, 89224)
        self.assertEqual(hsn_frame.sales_sgst_paise, 89223)
        self.assertEqual(hsn_frame.sales_total_gst_paise, 178447)

        hsn_lens = report.hsn_totals["90015000"]
        self.assertEqual(hsn_lens.sales_cgst_paise, 2379)
        self.assertEqual(hsn_lens.sales_sgst_paise, 2378)
        self.assertEqual(hsn_lens.sales_total_gst_paise, 4757)


if __name__ == "__main__":
    unittest.main()
