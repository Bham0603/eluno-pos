"""Tests for InvoiceRepository SQLite persistence and sequence allocation."""

from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
import unittest

from eluno_pos.domain.invoice import Invoice, InvoiceLine, InvoiceStatus
from eluno_pos.domain.payment import PaymentRecord, PaymentStatus
from eluno_pos.repositories.invoice_repository import InvoiceRepository


class TestInvoiceRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_pos.db")
        self.repo = InvoiceRepository(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _sample_line(self, line_id: str = "L1") -> InvoiceLine:
        return InvoiceLine(
            line_id=line_id,
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

    def test_sequential_invoice_numbering_single_store(self) -> None:
        inv1 = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_001",
            customer_id=80001001,
            gross_paise=1099800,
            discount_paise=0,
            net_paise=1099800,
            taxable_paise=932034,
            tax_paise=167766,
            lines=[self._sample_line("L1")],
        )
        self.assertEqual(inv1.invoice_no, "ELU/HSR/FY26/00001")
        self.assertEqual(inv1.seq, 1)
        self.assertEqual(inv1.status, InvoiceStatus.PENDING_RECONCILIATION)

        inv2 = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_002",
            customer_id=80001002,
            gross_paise=1099800,
            discount_paise=0,
            net_paise=1099800,
            taxable_paise=932034,
            tax_paise=167766,
            lines=[self._sample_line("L2")],
        )
        self.assertEqual(inv2.invoice_no, "ELU/HSR/FY26/00002")
        self.assertEqual(inv2.seq, 2)

    def test_independent_sequence_across_stores(self) -> None:
        hsr_inv = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_hsr_1",
            customer_id=80001001,
            gross_paise=500000,
            discount_paise=0,
            net_paise=500000,
            taxable_paise=423729,
            tax_paise=76271,
            lines=[self._sample_line()],
        )
        kor_inv = self.repo.allocate_invoice(
            store_code="KOR",
            financial_year="FY26",
            checkout_ref="chk_kor_1",
            customer_id=80001002,
            gross_paise=500000,
            discount_paise=0,
            net_paise=500000,
            taxable_paise=423729,
            tax_paise=76271,
            lines=[self._sample_line()],
        )

        self.assertEqual(hsr_inv.invoice_no, "ELU/HSR/FY26/00001")
        self.assertEqual(kor_inv.invoice_no, "ELU/KOR/FY26/00001")
        self.assertEqual(hsr_inv.seq, 1)
        self.assertEqual(kor_inv.seq, 1)

    def test_concurrency_safe_allocation(self) -> None:
        num_threads = 10
        allocated_invoices = []

        def worker(idx: int) -> str:
            repo = InvoiceRepository(self.db_path)
            inv = repo.allocate_invoice(
                store_code="IND",
                financial_year="FY26",
                checkout_ref=f"chk_concurrent_{idx}",
                customer_id=80001000 + idx,
                gross_paise=100000,
                discount_paise=0,
                net_paise=100000,
                taxable_paise=90000,
                tax_paise=10000,
                lines=[self._sample_line()],
            )
            return inv.invoice_no

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker, i) for i in range(num_threads)]
            for f in futures:
                allocated_invoices.append(f.result())

        self.assertEqual(len(allocated_invoices), num_threads)
        # All invoice numbers must be distinct
        self.assertEqual(len(set(allocated_invoices)), num_threads)
        # Verify sequences 00001 through 00010 exist
        expected = {f"ELU/IND/FY26/{i:05d}" for i in range(1, num_threads + 1)}
        self.assertEqual(set(allocated_invoices), expected)

    def test_no_invoice_number_reuse_after_cancellation(self) -> None:
        inv1 = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_cancel_1",
            customer_id=80001001,
            gross_paise=500000,
            discount_paise=0,
            net_paise=500000,
            taxable_paise=423729,
            tax_paise=76271,
            lines=[self._sample_line()],
        )
        self.assertEqual(inv1.invoice_no, "ELU/HSR/FY26/00001")

        # Cancel the unpaid invoice
        self.repo.update_invoice_status(inv1.invoice_no, InvoiceStatus.CANCELLED)
        cancelled_inv = self.repo.get_invoice(inv1.invoice_no)
        self.assertEqual(cancelled_inv.status, InvoiceStatus.CANCELLED)

        # Allocate next invoice - must be 00002, NOT 00001!
        inv2 = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_cancel_2",
            customer_id=80001002,
            gross_paise=500000,
            discount_paise=0,
            net_paise=500000,
            taxable_paise=423729,
            tax_paise=76271,
            lines=[self._sample_line()],
        )
        self.assertEqual(inv2.invoice_no, "ELU/HSR/FY26/00002")

    def test_paid_invoice_cannot_be_cancelled(self) -> None:
        inv = self.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY26",
            checkout_ref="chk_paid_1",
            customer_id=80001001,
            gross_paise=500000,
            discount_paise=0,
            net_paise=500000,
            taxable_paise=423729,
            tax_paise=76271,
            lines=[self._sample_line()],
        )
        # Record payment
        self.repo.update_invoice_payment(inv.invoice_no, "chg_99999999")

        # Attempt to mark CANCELLED must fail
        with self.assertRaises(ValueError):
            self.repo.update_invoice_status(inv.invoice_no, InvoiceStatus.CANCELLED)

    def test_payment_attempt_audit_lifecycle(self) -> None:
        attempt = PaymentRecord(
            checkout_ref="chk_pay_test",
            invoice_no=self._sample_invoice().invoice_no,
            order_ref="ord_ref_test_001",
            amount_paise=1299900,
            method="upi",
            status=PaymentStatus.INITIATED,
        )
        rec = self.repo.record_payment_attempt(attempt)
        self.assertIsNotNone(rec.id)
        self.assertEqual(rec.status, PaymentStatus.INITIATED)

        # Update upon capture
        self.repo.update_payment_attempt(
            order_ref="ord_ref_test_001",
            status=PaymentStatus.CAPTURED,
            charge_id="chg_test_12345",
        )
        updated = self.repo.get_payment_attempt("ord_ref_test_001")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.status, PaymentStatus.CAPTURED)
        self.assertEqual(updated.charge_id, "chg_test_12345")

    def _sample_invoice(self, store: str = "HSR", ref: str = "chk_sample") -> Invoice:
        return self.repo.allocate_invoice(
            store_code=store,
            financial_year="FY26",
            checkout_ref=ref,
            customer_id=80001001,
            gross_paise=500000,
            discount_paise=0,
            net_paise=500000,
            taxable_paise=423729,
            tax_paise=76271,
            lines=[self._sample_line()],
        )

    def test_database_pragmas_and_durability_configuration(self) -> None:
        """Verify that foreign keys, busy timeout, WAL mode, and synchronous FULL are enforced."""
        with self.repo._get_connection() as conn:
            fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
            self.assertEqual(fk, 1, "Foreign keys must be enabled on every connection")

            sync = conn.execute("PRAGMA synchronous;").fetchone()[0]
            self.assertEqual(sync, 2, "PRAGMA synchronous must be 2 (FULL) to guarantee power-loss durability")

            timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
            self.assertGreaterEqual(timeout, 30000, "Busy timeout must be at least 30,000ms")

            journal = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            self.assertEqual(journal.lower(), "wal", "File DB must operate in WAL journal mode")

    def test_invoice_table_check_constraint_status(self) -> None:
        """Verify CHECK constraint rejects invalid invoice status."""
        import sqlite3
        with self.repo._get_connection() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO invoices (
                        invoice_no, store_code, financial_year, seq, status,
                        customer_id, gross_paise, discount_paise, net_paise,
                        taxable_paise, tax_paise, checkout_ref, terminal_charge_id,
                        shopify_order_id, created_at, updated_at
                    ) VALUES ('ELU/HSR/FY26/99999', 'HSR', 'FY26', 99999, 'INVALID_STATUS',
                             80001001, 1000, 0, 1000, 900, 100, 'chk_invalid_status',
                             NULL, NULL, '2026-10-20', '2026-10-20');
                    """
                )

    def test_invoice_table_unique_sequence_constraint(self) -> None:
        """Verify UNIQUE (store_code, financial_year, seq) constraint."""
        import sqlite3
        inv = self._sample_invoice(store="HSR", ref="chk_uniq_seq")
        with self.repo._get_connection() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO invoices (
                        invoice_no, store_code, financial_year, seq, status,
                        customer_id, gross_paise, discount_paise, net_paise,
                        taxable_paise, tax_paise, checkout_ref, terminal_charge_id,
                        shopify_order_id, created_at, updated_at
                    ) VALUES ('ELU/HSR/FY26/00001_DUP', 'HSR', 'FY26', ?, 'POSTED',
                             80001001, 1000, 0, 1000, 900, 100, 'chk_dup_seq',
                             NULL, NULL, '2026-10-20', '2026-10-20');
                    """,
                    (inv.seq,),
                )

    def test_payment_attempt_foreign_key_constraint(self) -> None:
        """Verify foreign key constraint on payment_attempts referencing invoices."""
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            attempt = PaymentRecord(
                checkout_ref="chk_non_existent",
                invoice_no="ELU/NONEXISTENT/FY26/00001",
                order_ref="ord_fk_violation",
                amount_paise=1000,
                method="upi",
                status=PaymentStatus.INITIATED,
            )
            self.repo.record_payment_attempt(attempt)

    def test_payment_attempt_charge_id_uniqueness(self) -> None:
        """Verify that a charge_id cannot be linked to multiple payment attempts."""
        import sqlite3
        inv1 = self._sample_invoice(store="HSR", ref="chk_chg_uniq_1")
        inv2 = self._sample_invoice(store="HSR", ref="chk_chg_uniq_2")

        att1 = PaymentRecord(
            checkout_ref=inv1.checkout_ref,
            invoice_no=inv1.invoice_no,
            order_ref="ord_chg_1",
            amount_paise=1000,
            method="upi",
            status=PaymentStatus.CAPTURED,
            charge_id="chg_shared_duplicate",
        )
        self.repo.record_payment_attempt(att1)

        att2 = PaymentRecord(
            checkout_ref=inv2.checkout_ref,
            invoice_no=inv2.invoice_no,
            order_ref="ord_chg_2",
            amount_paise=1000,
            method="upi",
            status=PaymentStatus.CAPTURED,
            charge_id="chg_shared_duplicate",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.record_payment_attempt(att2)

    def test_invoice_terminal_charge_id_uniqueness(self) -> None:
        """Verify that no two invoices can claim the same terminal_charge_id."""
        import sqlite3
        inv1 = self._sample_invoice(store="HSR", ref="chk_inv_chg_1")
        inv2 = self._sample_invoice(store="HSR", ref="chk_inv_chg_2")

        self.repo.update_invoice_payment(inv1.invoice_no, "chg_unique_term_001")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.update_invoice_payment(inv2.invoice_no, "chg_unique_term_001")

    def test_shopify_intent_foreign_key_constraint(self) -> None:
        """Verify foreign key constraint on shopify_order_intents referencing invoices."""
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.record_shopify_intent(
                invoice_no="ELU/NONEXISTENT/FY26/00099",
                checkout_ref="chk_non_existent_shopify",
                payload={"order": {}},
            )


if __name__ == "__main__":
    unittest.main()

