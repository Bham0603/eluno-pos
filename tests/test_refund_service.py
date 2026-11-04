"""Tests for RefundService concurrency safety, credit note lifecycle, and frame restocking."""

import concurrent.futures
from datetime import date
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock

from eluno_pos.adapters.exceptions import AmbiguousOperationError, ValidationError
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.adapters.terminal_client import TerminalClient
from eluno_pos.domain.invoice import Invoice, InvoiceLine, InvoiceStatus
from eluno_pos.domain.refund import RefundRecord, RefundStatus
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
            taxable_paise=932034,
            cgst_paise=83883,
            sgst_paise=83883,
            tax_paise=167766,
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
            taxable_paise=809429,
            cgst_paise=20236,
            sgst_paise=20235,
            tax_paise=40471,
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

    def test_concurrent_refund_attempts_atomic_safety(self) -> None:
        """Requirement 1: Start two refund attempts simultaneously.

        Exactly one attempt may initiate a terminal refund.
        The other attempt must be rejected safely.
        Verify only one terminal refund was created.
        """
        self.mock_terminal_client.get_charge.return_value = {
            "charge_id": "chg_unit_refund_01",
            "amount_paise": 1949700,
            "refunded_paise": 0,
            "status": "captured",
        }
        self.mock_terminal_client.create_refund.return_value = {
            "refund_id": "trf_concurrent_01",
            "charge_id": "chg_unit_refund_01",
            "amount_paise": 1949700,
            "status": "processed",
        }
        self.mock_shopify_client.create_refund.return_value = {"id": 98000099}

        results = []
        errors = []

        barrier = threading.Barrier(2)

        def attempt_refund():
            barrier.wait()
            try:
                res = self.service.process_full_refund(
                    invoice_no=self.invoice.invoice_no,
                    location_id=70000002,
                )
                results.append(res)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=attempt_refund)
        t2 = threading.Thread(target=attempt_refund)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one thread succeeds in initiating and capturing the refund
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)

        # The second thread was rejected safely via database-level concurrency protection
        self.assertEqual(len(errors), 1)
        self.assertTrue(
            isinstance(errors[0], (AmbiguousOperationError, ValidationError)),
            f"Expected concurrency safety error, got {type(errors[0])}",
        )

        # Invariant: Terminal create_refund was invoked EXACTLY ONCE
        self.assertEqual(self.mock_terminal_client.create_refund.call_count, 1)

    def test_terminal_refund_failure_does_not_issue_credit_note(self) -> None:
        """Requirement 2: Terminal refund failure without credit note issuance."""
        self.mock_terminal_client.get_charge.return_value = {
            "charge_id": "chg_unit_refund_01",
            "amount_paise": 1949700,
            "refunded_paise": 0,
            "status": "captured",
        }
        # Simulate gateway rejection
        self.mock_terminal_client.create_refund.side_effect = ValidationError("Gateway: Customer card expired or declined")

        with self.assertRaises(ValidationError):
            self.service.process_full_refund(
                invoice_no=self.invoice.invoice_no,
                location_id=70000002,
            )

        # Invariant: Zero credit note sequences consumed!
        with self.repo._get_connection() as conn:
            cur = conn.execute("SELECT count(*) as cnt FROM credit_note_sequences WHERE store_code = 'HSR';")
            self.assertEqual(cur.fetchone()["cnt"], 0)

        # Invariant: Refund record in DB is FAILED with no credit note assigned
        refunds = self.repo.get_refunds_for_invoice(self.invoice.invoice_no)
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0].status, RefundStatus.FAILED)
        self.assertIsNone(refunds[0].credit_note_no)

    def test_application_restart_between_terminal_confirmation_and_credit_note_allocation(self) -> None:
        """Requirement 2: Recovery if app crashes after terminal confirmation before credit note allocation."""
        # 1. Simulate DB record created before crash: terminal refund captured, credit note not yet allocated
        ref_id = "ref_crash_test_01"
        self.repo.create_refund_intent_atomically(
            invoice_no=self.invoice.invoice_no,
            refund_id=ref_id,
            amount_paise=1949700,
            reason_code="CUSTOMER_RETURN",
        )
        self.repo.update_refund_terminal_captured(ref_id, "trf_captured_before_crash")

        # 2. Simulate application restart with fresh instances
        restarted_repo = InvoiceRepository(self.db_path)
        restarted_service = RefundService(
            terminal_client=self.mock_terminal_client,
            shopify_client=self.mock_shopify_client,
            invoice_repository=restarted_repo,
        )
        self.mock_shopify_client.create_refund.return_value = {"id": 98000555}

        # 3. Resume / recover refund
        rec_res = restarted_service.resume_or_reconcile_refund(
            refund_id=ref_id,
            location_id=70000002,
            financial_year="FY26",
        )

        self.assertIsNotNone(rec_res)
        self.assertTrue(rec_res.success)
        self.assertEqual(rec_res.status, RefundStatus.COMPLETED)
        # Credit note was allocated upon recovery
        self.assertEqual(rec_res.credit_note_no, "ELU/HSR/FY26/CN/00001")
        self.assertEqual(rec_res.shopify_refund_id, 98000555)

    def test_no_duplicate_credit_note_after_recovery(self) -> None:
        """Requirement 2: Preserve already allocated credit note numbers without duplicate issuance."""
        # 1. Simulate refund where credit note was allocated before Shopify network timeout
        ref_id = "ref_dup_cn_check"
        self.repo.create_refund_intent_atomically(
            invoice_no=self.invoice.invoice_no,
            refund_id=ref_id,
            amount_paise=1949700,
            reason_code="CUSTOMER_RETURN",
        )
        self.repo.update_refund_terminal_captured(ref_id, "trf_cn_dup_01")
        cn_first = self.repo.allocate_and_persist_credit_note(ref_id, "HSR", "FY26")
        self.assertEqual(cn_first, "ELU/HSR/FY26/CN/00001")

        # 2. Re-running allocate_and_persist_credit_note on the same refund ID MUST return the same CN
        cn_second = self.repo.allocate_and_persist_credit_note(ref_id, "HSR", "FY26")
        self.assertEqual(cn_first, cn_second)

        # 3. Resume refund
        self.mock_shopify_client.create_refund.return_value = {"id": 98000777}
        rec_res = self.service.resume_or_reconcile_refund(ref_id, location_id=70000002)
        self.assertTrue(rec_res.success)
        self.assertEqual(rec_res.credit_note_no, "ELU/HSR/FY26/CN/00001")

        # 4. Verify sequence was only incremented once (next is 00002, not 00003)
        next_cn = self.repo.allocate_credit_note_no("HSR", "FY26")
        self.assertEqual(next_cn, "ELU/HSR/FY26/CN/00002")

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
