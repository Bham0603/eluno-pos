"""Tests for RefundService duplicate prevention, terminal reversal, and frame restocking."""

from datetime import date
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from eluno_pos.adapters.exceptions import AmbiguousOperationError, ValidationError
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.adapters.terminal_client import TerminalClient
from eluno_pos.domain.invoice import Invoice, InvoiceLine, InvoiceStatus
from eluno_pos.domain.refund import RefundStatus
from eluno_pos.repositories.invoice_repository import InvoiceRepository
from eluno_pos.services.refund_service import RefundService


class TestRefundService(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_refund.db")
        self.repo = InvoiceRepository(self.db_path)

        self.mock_terminal_client = MagicMock(spec=TerminalClient)
        self.mock_shopify_client = MagicMock(spec=ShopifyClient)
        self.service = RefundService(
            terminal_client=self.mock_terminal_client,
            shopify_client=self.mock_shopify_client,
            invoice_repository=self.repo,
        )

        # Allocate posted invoice in DB
        frame_line = InvoiceLine(
            line_id="L1",
            line_type="frame",
            variant_id=91000003,
            sku="FR-0003",
            title="Eluno Atelier Rectangle 03",
            quantity=1,
            unit_price_paise=1099800,
            gross_paise=1099800,
            discount_paise=0,
            net_paise=1099800,
            hsn="90031900",
            pair_ref="1",
        )
        lens_line = InvoiceLine(
            line_id="L2",
            line_type="lens",
            variant_id=91000083,
            sku="LN-0043",
            title="Progressive 1.60 Blue Cut",
            quantity=1,
            unit_price_paise=949900,
            gross_paise=949900,
            discount_paise=100000,
            net_paise=849900,
            hsn="90015000",
            pair_ref="1",
            rx_id="RX5007",
            lens_config="progressive/1.6/blue_cut",
        )

        self.invoice = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_ref_test_01",
            customer_id=80001005,
            gross_paise=2049700,
            discount_paise=100000,
            net_paise=1949700,
            taxable_paise=1741463,
            tax_paise=208237,
            lines=[frame_line, lens_line],
        )
        self.repo.update_invoice_payment(self.invoice.invoice_no, "chg_unit_refund_01")
        self.repo.update_invoice_shopify_order(self.invoice.invoice_no, 95000001, InvoiceStatus.POSTED)

        # Mock Shopify order structure
        self.mock_shopify_order = {
            "id": 95000001,
            "order_number": 1001,
            "location_id": 70000002,
            "line_items": [
                {
                    "id": 96000001,
                    "variant_id": 91000003,
                    "sku": "FR-0003",
                    "quantity": 1,
                    "properties": [{"name": "line_type", "value": "frame"}, {"name": "pair_ref", "value": "1"}],
                },
                {
                    "id": 96000002,
                    "variant_id": 91000083,
                    "sku": "LN-0043",
                    "quantity": 1,
                    "properties": [{"name": "line_type", "value": "lens"}, {"name": "pair_ref", "value": "1"}],
                },
            ],
            "transactions": [
                {
                    "id": 97000001,
                    "kind": "sale",
                    "status": "success",
                    "amount": "19497.00",
                    "gateway": "razorpay_terminal",
                }
            ],
            "refunds": [],
        }
        self.mock_shopify_client.get_order.return_value = self.mock_shopify_order

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_successful_full_refund_with_frame_only_restock(self) -> None:
        # Mock terminal charge
        self.mock_terminal_client.get_charge.return_value = {
            "charge_id": "chg_unit_refund_01",
            "amount_paise": 1949700,
            "refunded_paise": 0,
            "status": "captured",
        }
        self.mock_terminal_client.create_refund.return_value = {
            "refund_id": "trf_unit_success_01",
            "charge_id": "chg_unit_refund_01",
            "amount_paise": 1949700,
            "status": "processed",
        }
        self.mock_shopify_client.create_refund.return_value = {
            "id": 98000001,
            "order_id": 95000001,
            "status": "success",
        }

        res = self.service.process_full_refund(
            invoice_no=self.invoice.invoice_no,
            location_id=70000002,
            reason_code="CUSTOMER_RETURN",
            staff_id="S105",
        )

        self.assertTrue(res.success)
        self.assertEqual(res.status, RefundStatus.COMPLETED)
        self.assertEqual(res.amount_refunded_paise, 1949700)
        self.assertEqual(res.credit_note_no, "ELU/HSR/FY26/CN/00001")
        self.assertEqual(res.terminal_refund_id, "trf_unit_success_01")
        self.assertEqual(res.shopify_refund_id, 98000001)
        self.assertEqual(res.frames_restocked_count, 1)

        # Verify Shopify refund payload inspected frame-only restock
        call_args = self.mock_shopify_client.create_refund.call_args[1]["refund_data"]
        rl_items = call_args["refund_line_items"]
        self.assertEqual(len(rl_items), 2)

        frame_entry = next(r for r in rl_items if r["line_item_id"] == 96000001)
        lens_entry = next(r for r in rl_items if r["line_item_id"] == 96000002)

        # Invariant: Frame must be returned/restocked; lens must NOT be restocked!
        self.assertEqual(frame_entry["restock_type"], "return")
        self.assertEqual(frame_entry["location_id"], 70000002)
        self.assertEqual(lens_entry["restock_type"], "no_restock")
        self.assertNotIn("location_id", lens_entry)

    def test_already_fully_refunded_charge_is_rejected(self) -> None:
        self.mock_terminal_client.get_charge.return_value = {
            "charge_id": "chg_unit_refund_01",
            "amount_paise": 1949700,
            "refunded_paise": 1949700,  # Already 100% refunded
            "status": "captured",
        }

        with self.assertRaises(ValidationError) as ctx:
            self.service.process_full_refund(
                invoice_no=self.invoice.invoice_no,
                location_id=70000002,
            )
        self.assertIn("already been fully refunded", str(ctx.exception))
        self.mock_terminal_client.create_refund.assert_not_called()

    def test_duplicate_refund_prevention_on_unresolved_refund(self) -> None:
        # Simulate an in-flight refund already recorded in DB
        self.mock_terminal_client.get_charge.side_effect = [
            # 1. Baseline check
            {"charge_id": "chg_unit_refund_01", "amount_paise": 1949700, "refunded_paise": 0},
            # 2. Ambiguous post-timeout check: uncertain intermediate value
            {"charge_id": "chg_unit_refund_01", "amount_paise": 1949700, "refunded_paise": 500000},
        ]
        # First call encounters ambiguous timeout
        self.mock_terminal_client.create_refund.side_effect = AmbiguousOperationError("Terminal timeout")

        res1 = self.service.process_full_refund(
            invoice_no=self.invoice.invoice_no,
            location_id=70000002,
        )
        self.assertFalse(res1.success)
        self.assertEqual(res1.status, RefundStatus.PENDING_RECONCILIATION)

        # Second call to refund the same invoice MUST BE BLOCKED
        with self.assertRaises(AmbiguousOperationError) as ctx:
            self.service.process_full_refund(
                invoice_no=self.invoice.invoice_no,
                location_id=70000002,
            )
        self.assertIn("unresolved refund", str(ctx.exception))

    def test_ambiguous_terminal_refund_conclusive_recovery(self) -> None:
        self.mock_terminal_client.get_charge.side_effect = [
            # Baseline check before POST
            {"charge_id": "chg_unit_refund_01", "amount_paise": 1949700, "refunded_paise": 0},
            # Post-timeout status check: terminal DID process the refund!
            {"charge_id": "chg_unit_refund_01", "amount_paise": 1949700, "refunded_paise": 1949700},
        ]
        self.mock_terminal_client.create_refund.side_effect = AmbiguousOperationError("Connection drop during POST")
        self.mock_shopify_client.create_refund.return_value = {"id": 98000002}

        res = self.service.process_full_refund(
            invoice_no=self.invoice.invoice_no,
            location_id=70000002,
        )

        # Successfully recovered via read-after-write comparison!
        self.assertTrue(res.success)
        self.assertEqual(res.status, RefundStatus.COMPLETED)
        self.assertIn("trf_reconciled_", res.terminal_refund_id)

    def test_shopify_refund_recording_failure_leaves_pending_without_duplicate_terminal_refund(self) -> None:
        self.mock_terminal_client.get_charge.return_value = {
            "charge_id": "chg_unit_refund_01",
            "amount_paise": 1949700,
            "refunded_paise": 0,
        }
        self.mock_terminal_client.create_refund.return_value = {
            "refund_id": "trf_captured_ok",
            "amount_paise": 1949700,
        }
        # Shopify drops
        self.mock_shopify_client.create_refund.side_effect = AmbiguousOperationError("Shopify Gateway Timeout")

        res = self.service.process_full_refund(
            invoice_no=self.invoice.invoice_no,
            location_id=70000002,
        )

        self.assertFalse(res.success)
        self.assertEqual(res.status, RefundStatus.PENDING_RECONCILIATION)
        self.assertEqual(res.terminal_refund_id, "trf_captured_ok")

        # Reconcile later: Shopify order refunds now contains the refund
        self.mock_shopify_order["refunds"] = [
            {
                "id": 98000010,
                "note_attributes": [{"name": "credit_note_no", "value": res.credit_note_no}],
            }
        ]

        rec_res = self.service.reconcile_shopify_refund(res.refund_id)
        self.assertTrue(rec_res.success)
        self.assertEqual(rec_res.status, RefundStatus.COMPLETED)
        self.assertEqual(rec_res.shopify_refund_id, 98000010)

        # Terminal refund was called exactly once! Never duplicated!
        self.assertEqual(self.mock_terminal_client.create_refund.call_count, 1)


if __name__ == "__main__":
    unittest.main()
