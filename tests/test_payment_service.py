"""Tests for PaymentService terminal charge lifecycle and retry safety."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from eluno_pos.adapters.exceptions import (
    AmbiguousOperationError,
    ServerError,
    TimeoutError,
    ValidationError,
)
from eluno_pos.adapters.terminal_client import TerminalClient
from eluno_pos.domain.invoice import Invoice, InvoiceLine, InvoiceStatus
from eluno_pos.domain.payment import PaymentStatus
from eluno_pos.repositories.invoice_repository import InvoiceRepository
from eluno_pos.services.payment_service import PaymentService


class TestPaymentService(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_pay.db")
        self.repo = InvoiceRepository(self.db_path)
        self.mock_terminal_client = MagicMock(spec=TerminalClient)
        self.service = PaymentService(
            terminal_client=self.mock_terminal_client,
            invoice_repository=self.repo,
        )

        self.invoice = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_pay_unit_1",
            customer_id=80001001,
            gross_paise=1299900,
            discount_paise=100000,
            net_paise=1199900,
            taxable_paise=1016864,
            tax_paise=183036,
            lines=[],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_successful_payment_lifecycle(self) -> None:
        self.mock_terminal_client.create_charge.return_value = {
            "charge_id": "chg_unit_7777",
            "status": "captured",
            "amount_paise": 1199900,
            "method": "upi",
            "order_ref": "pay_chk_pay_unit_1",
        }

        rec = self.service.process_payment(
            invoice=self.invoice,
            amount_paise=1199900,
            method="upi",
        )

        self.assertEqual(rec.status, PaymentStatus.CAPTURED)
        self.assertEqual(rec.charge_id, "chg_unit_7777")
        self.assertEqual(rec.amount_paise, 1199900)

        # Check DB state
        inv_db = self.repo.get_invoice(self.invoice.invoice_no)
        self.assertEqual(inv_db.terminal_charge_id, "chg_unit_7777")
        self.assertTrue(inv_db.is_paid)

    def test_amount_zero_or_negative_raises_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.process_payment(invoice=self.invoice, amount_paise=0)

        with self.assertRaises(ValidationError):
            self.service.process_payment(invoice=self.invoice, amount_paise=-500)

    def test_ambiguous_timeout_marks_unknown_and_blocks_auto_retry(self) -> None:
        self.mock_terminal_client.create_charge.side_effect = AmbiguousOperationError(
            "Terminal timeout"
        )

        with self.assertRaises(AmbiguousOperationError):
            self.service.process_payment(
                invoice=self.invoice,
                amount_paise=1199900,
                method="upi",
            )

        # Verify DB attempt is UNKNOWN
        attempt = self.repo.get_payment_attempt("pay_chk_pay_unit_1")
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, PaymentStatus.UNKNOWN)

        # Second attempt with same order_ref must be blocked to prevent double-charging
        with self.assertRaises(AmbiguousOperationError) as ctx:
            self.service.process_payment(
                invoice=self.invoice,
                amount_paise=1199900,
                method="upi",
            )
        self.assertIn("UNKNOWN state", str(ctx.exception))

    def test_already_captured_payment_is_idempotent(self) -> None:
        self.mock_terminal_client.create_charge.return_value = {
            "charge_id": "chg_unit_already_done",
            "status": "captured",
            "amount_paise": 1199900,
            "method": "card",
            "order_ref": "pay_chk_pay_unit_1",
        }

        rec1 = self.service.process_payment(
            invoice=self.invoice,
            amount_paise=1199900,
            method="card",
        )
        self.assertEqual(rec1.status, PaymentStatus.CAPTURED)

        # Second call returns the existing record without calling client again
        self.mock_terminal_client.create_charge.reset_mock()
        rec2 = self.service.process_payment(
            invoice=self.invoice,
            amount_paise=1199900,
            method="card",
        )
        self.assertEqual(rec2.charge_id, "chg_unit_already_done")
        self.mock_terminal_client.create_charge.assert_not_called()

    def test_definitive_terminal_failure_marks_failed(self) -> None:
        self.mock_terminal_client.create_charge.side_effect = ValueError("Card declined")

        with self.assertRaises(ValueError):
            self.service.process_payment(
                invoice=self.invoice,
                amount_paise=1199900,
                method="card",
            )

        attempt = self.repo.get_payment_attempt("pay_chk_pay_unit_1")
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.status, PaymentStatus.FAILED)


if __name__ == "__main__":
    unittest.main()

