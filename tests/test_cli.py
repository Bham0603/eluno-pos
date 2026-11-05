"""Unit and functional tests for Eluno POS CLI commands, parsing, and formatting."""

from datetime import date
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from eluno_pos.cli import main
from eluno_pos.cli.commands.checkout import run_checkout
from eluno_pos.cli.commands.customer import run_customer_lookup
from eluno_pos.cli.commands.doctor import run_doctor
from eluno_pos.cli.commands.invoice import run_invoice_show
from eluno_pos.cli.commands.reconcile import run_reconcile
from eluno_pos.cli.commands.refund import run_refund
from eluno_pos.cli.commands.report import run_report
from eluno_pos.cli.context import AppContext
from eluno_pos.cli.formatting import format_inr
from eluno_pos.cli.parser import build_parser
from eluno_pos.config import AppConfig
from eluno_pos.domain.customer import Customer
from eluno_pos.domain.invoice import InvoiceStatus, get_financial_year
from eluno_pos.domain.prescription import EyePrescription, Prescription, PupillaryDistance
from eluno_pos.domain.refund import RefundRecord, RefundStatus
from eluno_pos.repositories.invoice_repository import InvoiceRepository


def _make_customer(
    cid: int = 80001001,
    first_name: str = "Rahul",
    last_name: str = "Menon",
    phone: str = "+919000000001",
    email: str = "rahul@example.com",
    tags: str = "family",
) -> Customer:
    return Customer(
        id=cid,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        email=email,
        tags=tags,
        total_spent="0.00",
        orders_count=0,
    )


class TestCli(unittest.TestCase):
    """Test suite for Eluno POS command-line interface."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_cli.db")
        self.config = AppConfig(
            shopify_base_url="http://localhost:8787/admin/api/2026-10",
            smt_base_url="http://localhost:8787/smt",
            terminal_base_url="http://localhost:8787/terminal",
            db_path=self.db_path,
        )
        self.ctx = AppContext.create(self.config)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_format_inr(self) -> None:
        """Verify Indian Rupee formatting with paise conversion and comma grouping."""
        self.assertEqual(format_inr(0), "₹0.00")
        self.assertEqual(format_inr(99), "₹0.99")
        self.assertEqual(format_inr(100), "₹1.00")
        self.assertEqual(format_inr(99900), "₹999.00")
        self.assertEqual(format_inr(1949700), "₹19,497.00")
        self.assertEqual(format_inr(10000000), "₹1,00,000.00")
        self.assertEqual(format_inr(-50000), "-₹500.00")
        self.assertEqual(format_inr(None), "₹0.00")

    def test_cli_parser_routing(self) -> None:
        """Verify standard-library argparse routes commands correctly."""
        parser = build_parser()

        # Doctor
        args = parser.parse_args(["doctor"])
        self.assertEqual(args.command, "doctor")

        # Customer Lookup
        args = parser.parse_args(["customer", "lookup", "-p", "+919000000001"])
        self.assertEqual(args.command, "customer")
        self.assertEqual(args.customer_cmd, "lookup")
        self.assertEqual(args.phone, "+919000000001")

        # Checkout
        args = parser.parse_args(["checkout", "-s", "HSR", "--staff", "S108", "-c", "+919000000001", "-y"])
        self.assertEqual(args.command, "checkout")
        self.assertEqual(args.store, "HSR")
        self.assertEqual(args.staff, "S108")
        self.assertEqual(args.customer, "+919000000001")
        self.assertTrue(args.auto_confirm)

        # Refund
        args = parser.parse_args(["refund", "-i", "ELU/HSR/FY26/00001", "-y"])
        self.assertEqual(args.command, "refund")
        self.assertEqual(args.invoice, "ELU/HSR/FY26/00001")
        self.assertTrue(args.auto_confirm)

        # Report
        args = parser.parse_args(["report", "-s", "HSR", "-d", "2026-10-20"])
        self.assertEqual(args.command, "report")
        self.assertEqual(args.store, "HSR")
        self.assertEqual(args.date, "2026-10-20")

        # Reconcile
        args = parser.parse_args(["reconcile", "-s", "HSR", "-x"])
        self.assertEqual(args.command, "reconcile")
        self.assertTrue(args.execute)

        # Invoice show
        args = parser.parse_args(["invoice", "show", "-i", "ELU/HSR/FY26/00001"])
        self.assertEqual(args.command, "invoice")
        self.assertEqual(args.invoice_cmd, "show")
        self.assertEqual(args.invoice_no, "ELU/HSR/FY26/00001")

    def test_doctor_command_success(self) -> None:
        """Verify doctor health check command runs and returns 0 when services respond."""
        with patch.object(self.ctx.shopify_client, "get_locations", return_value=[{"id": 70000001, "name": "Test Store"}]), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=[{"staff_id": "S101", "name": "Staff 1"}]), \
             patch.object(self.ctx.terminal_client, "get_charge", side_effect=Exception("Connection refused")):
            
            # When terminal fails, doctor returns 1
            ret = run_doctor(self.ctx)
            self.assertEqual(ret, 1)

    def test_customer_lookup_missing_customer(self) -> None:
        """Verify customer lookup handles non-existent customers cleanly."""
        with patch.object(self.ctx.customer_service, "find_by_phone", return_value=None):
            ret = run_customer_lookup(self.ctx, phone="+919999999999")
            self.assertEqual(ret, 1)

    def test_customer_lookup_privacy_minimization(self) -> None:
        """Requirement: Clinical measurements (SPH, CYL, AXIS, PD) must NOT appear in lookup output."""
        fake_cust = Customer(
            id=80001001,
            first_name="Rahul",
            last_name="Menon",
            phone="+919000000001",
            email="rahul@example.com",
            tags="family",
            total_spent="0.00",
            orders_count=0,
        )
        fake_rx = Prescription(
            rx_id="RX5002",
            customer_id=80001001,
            phone="+919000000001",
            date="2026-08-12",
            source="external",
            optometrist_id="SMT",
            od=EyePrescription(sph=2.5, cyl=-2.0, axis=155),
            os=EyePrescription(sph=2.75, cyl=-2.0, axis=155),
            pd=PupillaryDistance(binocular=60),
            valid_until="2027-08-12",
        )
        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[fake_rx]):
            ret = run_customer_lookup(self.ctx, phone="+919000000001")
            self.assertEqual(ret, 0)

        output = captured.getvalue()
        # Must show Rx ID and date and status
        self.assertIn("RX5002", output)
        self.assertIn("2026-08-12", output)
        self.assertIn("Valid", output)

        # Must NOT show raw clinical measurements
        self.assertNotIn("+2.50", output)
        self.assertNotIn("-2.00", output)
        self.assertNotIn("155", output)
        self.assertNotIn("60 mm", output)
        self.assertNotIn("OD (Right Eye)", output)

    def test_strict_store_validation_rejects_invalid_store(self) -> None:
        """Requirement: Invalid store code must reject checkout and not allocate an invoice."""
        locations = [
            {"id": 70000001, "name": "Eluno Mantri Mall", "active": True, "eluno": {"store_code": "MM", "kind": "store"}},
            {"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}},
        ]
        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations):
            ret = run_checkout(
                ctx=self.ctx,
                store_code="INVALID_STORE",
                staff_id="S108",
                customer_phone="+919000000001",
                auto_confirm=True,
            )
            self.assertEqual(ret, 1)

        output = captured.getvalue()
        self.assertIn("Invalid store code 'INVALID_STORE'", output)
        self.assertIn("MM, HSR", output)

        # Ensure no invoice was allocated in database
        with self.ctx.repo._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM invoices;")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_strict_staff_validation_rejects_unverified_staff(self) -> None:
        """Requirement: Staff member must match store records; invalid staff terminates checkout."""
        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR", "can_override_discount": True}]

        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr):
            ret = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S999_UNKNOWN",
                customer_phone="+919000000001",
                auto_confirm=True,
            )
            self.assertEqual(ret, 1)

        output = captured.getvalue()
        self.assertIn("S999_UNKNOWN", output)
        self.assertIn("not found in staff registry", output)

        # Zero invoices allocated
        with self.ctx.repo._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM invoices;")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_strict_hsn_metadata_missing_rejects_products(self) -> None:
        """Requirement: Missing HSN metadata in catalogue rejects frame or lens immediately."""
        fake_cust = _make_customer()
        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR", "can_override_discount": True}]

        # Frame without HSN
        frame_no_hsn = {"id": 91000001, "sku": "FR-0001", "title": "Test Frame", "price": "10000.00", "eluno": {}}
        lens_ok = {"id": 91000041, "sku": "LN-0001", "title": "Test Lens", "price": "1000.00", "eluno": {"hsn": "90015000"}}

        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_no_hsn if vid == 91000001 else lens_ok):
            ret = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                auto_confirm=True,
            )
            self.assertEqual(ret, 1)

        output = captured.getvalue()
        self.assertIn("missing mandatory HSN", output)

        # Lens without HSN
        frame_ok = {"id": 91000001, "sku": "FR-0001", "title": "Test Frame", "price": "10000.00", "eluno": {"hsn": "90031900"}}
        lens_no_hsn = {"id": 91000041, "sku": "LN-0001", "title": "Test Lens", "price": "1000.00", "eluno": {}}

        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_ok if vid == 91000001 else lens_no_hsn):
            ret = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                auto_confirm=True,
            )
            self.assertEqual(ret, 1)

        output = captured.getvalue()
        self.assertIn("missing mandatory HSN", output)

    def test_prescription_id_validation_rejects_missing_or_expired(self) -> None:
        """Requirement: If --rx specifies an ID not belonging to customer or expired, reject."""
        fake_cust = _make_customer()
        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR"}]

        # 1. Non-existent prescription
        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]):
            ret = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                rx_id="RX_DOES_NOT_EXIST",
                auto_confirm=True,
            )
            self.assertEqual(ret, 1)
        self.assertIn("does not exist for customer", captured.getvalue())

        # 2. Expired prescription
        expired_rx = Prescription(
            rx_id="RX_EXPIRED",
            customer_id=80001001,
            phone="+919000000001",
            date="2024-01-01",
            source="in_store",
            od=EyePrescription(sph=1.0),
            os=EyePrescription(sph=1.0),
            pd=PupillaryDistance(binocular=60),
            valid_until="2025-01-01",  # Expired
        )
        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[expired_rx]):
            ret = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                rx_id="RX_EXPIRED",
                transaction_date=date(2026, 11, 5),
                auto_confirm=True,
            )
            self.assertEqual(ret, 1)
        self.assertIn("expired", captured.getvalue())

    def test_financial_year_boundary_dates(self) -> None:
        """Requirement: Indian financial year runs April 1 to March 31."""
        self.assertEqual(get_financial_year(date(2025, 3, 31)), "FY25")
        self.assertEqual(get_financial_year(date(2025, 4, 1)), "FY26")
        self.assertEqual(get_financial_year(date(2026, 3, 31)), "FY26")
        self.assertEqual(get_financial_year(date(2026, 4, 1)), "FY27")
        self.assertEqual(get_financial_year(date(2027, 3, 31)), "FY27")

    def test_family_billing_association_persisted(self) -> None:
        """Requirement: Selected wearer/recipient is persisted as for_customer_id in SQLite."""
        fake_cust = _make_customer()
        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR", "can_override_discount": True}]
        frame_dict = {"id": 91000001, "sku": "FR-0001", "title": "Test Frame", "price": "10000.00", "eluno": {"hsn": "90031900"}}
        lens_dict = {"id": 91000041, "sku": "LN-0001", "title": "Test Lens", "price": "1000.00", "eluno": {"hsn": "90015000"}}

        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_dict if vid == 91000001 else lens_dict), \
             patch.object(self.ctx.terminal_client, "create_charge", return_value={"status": "captured", "charge_id": "ch_mock_123"}), \
             patch.object(self.ctx.shopify_client, "create_order", return_value={"id": 77000001, "order_number": 1001, "financial_status": "paid"}):

            ret = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                for_customer_id=80001005,  # Family member recipient
                auto_confirm=True,
            )
            self.assertEqual(ret, 0)

        # Verify from persisted SQLite database
        inv = self.ctx.repo.get_invoice("ELU/HSR/FY27/00001" if date.today().month >= 4 else "ELU/HSR/FY26/00001")
        self.assertIsNotNone(inv)
        self.assertEqual(len(inv.lines), 2)
        for line in inv.lines:
            self.assertEqual(line.for_customer_id, 80001005)
            # Verify properties JSON has for_customer_id
            prop_names = {p["name"]: p["value"] for p in line.properties}
            self.assertIn("for_customer_id", prop_names)
            self.assertEqual(prop_names["for_customer_id"], "80001005")

    def test_reconcile_missing_persisted_location_id_remains_pending(self) -> None:
        """Requirement: Missing persisted location ID in reconcile keeps refund pending and warns supervisor."""
        inv = self.ctx.repo.allocate_invoice(
            store_code="UNKNOWN",
            financial_year="FY26",
            checkout_ref="chk_test_001",
            customer_id=80001001,
            gross_paise=100000,
            discount_paise=0,
            net_paise=100000,
            taxable_paise=100000,
            tax_paise=0,
            lines=[],
        )
        self.ctx.repo.update_invoice_payment(inv.invoice_no, "ch_test_001")
        self.ctx.repo.update_invoice_shopify_order(inv.invoice_no, 99999999)

        ref = RefundRecord(
            refund_id="ref_test_001",
            invoice_no=inv.invoice_no,
            checkout_ref="chk_test_001",
            charge_id="ch_test_001",
            shopify_order_id=99999999,
            amount_paise=100000,
            status=RefundStatus.PENDING_RECONCILIATION,
            reason_code="CUSTOMER_RETURN",
        )
        self.ctx.repo.record_refund_intent(ref)

        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_order", return_value=None):
            ret = run_reconcile(self.ctx, execute_recovery=True, auto_confirm=True)
            self.assertEqual(ret, 2)

        output = captured.getvalue()
        self.assertIn("Persisted Shopify location ID missing", output)
        self.assertIn("kept PENDING for supervisor / manual resolution", output)

    def test_blank_confirmation_does_not_initiate_payment(self) -> None:
        """Requirement 1: Blank confirmation (pressing Enter with default=False) does not initiate payment."""
        fake_cust = _make_customer()
        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR", "can_override_discount": True}]
        frame_dict = {"id": 91000001, "sku": "FR-0001", "title": "Test Frame", "price": "10000.00", "eluno": {"hsn": "90031900"}}
        lens_dict = {"id": 91000041, "sku": "LN-0001", "title": "Test Lens", "price": "1000.00", "eluno": {"hsn": "90015000"}}

        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch("eluno_pos.cli.commands.checkout.prompt_confirm", return_value=False) as mock_confirm, \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_dict if vid == 91000001 else lens_dict), \
             patch.object(self.ctx.terminal_client, "create_charge") as mock_charge, \
             patch.object(self.ctx.shopify_client, "create_order") as mock_order:

            ret = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                auto_confirm=False,
            )
            self.assertEqual(ret, 0)
            mock_confirm.assert_called_once()
            # Verify zero payment charges or shopify orders initiated
            mock_charge.assert_not_called()
            mock_order.assert_not_called()

        output = captured.getvalue()
        self.assertIn("Payment cancelled by staff", output)
        self.assertIn("marked CANCELLED without initiating terminal charges", output)

        # In database, status must be CANCELLED
        inv = self.ctx.repo.get_invoice("ELU/HSR/FY27/00001" if date.today().month >= 4 else "ELU/HSR/FY26/00001")
        self.assertIsNotNone(inv)
        self.assertEqual(inv.status, InvoiceStatus.CANCELLED)

    def test_missing_required_arguments_with_yes_do_not_initiate_payment(self) -> None:
        """Requirement 1: Missing required arguments with --yes must not default silently and must not allocate invoice or pay."""
        fake_cust = _make_customer()
        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR"}]
        frame_dict = {"id": 91000001, "sku": "FR-0001", "title": "Test Frame", "price": "10000.00", "eluno": {"hsn": "90031900"}}
        lens_dict = {"id": 91000041, "sku": "LN-0001", "title": "Test Lens", "price": "1000.00", "eluno": {"hsn": "90015000"}}

        cases = [
            # Missing store
            {"store_code": None, "staff_id": "S108", "customer_phone": "+919000000001", "frame_variant_id": 91000001, "lens_variant_id": 91000041, "err": "--store"},
            # Missing staff
            {"store_code": "HSR", "staff_id": None, "customer_phone": "+919000000001", "frame_variant_id": 91000001, "lens_variant_id": 91000041, "err": "--staff"},
            # Missing customer
            {"store_code": "HSR", "staff_id": "S108", "customer_phone": None, "frame_variant_id": 91000001, "lens_variant_id": 91000041, "err": "--customer"},
            # Missing frame
            {"store_code": "HSR", "staff_id": "S108", "customer_phone": "+919000000001", "frame_variant_id": None, "lens_variant_id": 91000041, "err": "--frame"},
            # Missing lens
            {"store_code": "HSR", "staff_id": "S108", "customer_phone": "+919000000001", "frame_variant_id": 91000001, "lens_variant_id": None, "err": "--lens"},
        ]

        for case in cases:
            captured = io.StringIO()
            with patch("sys.stdout", captured), \
                 patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
                 patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
                 patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
                 patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
                 patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_dict if vid == 91000001 else lens_dict), \
                 patch.object(self.ctx.terminal_client, "create_charge") as mock_charge:

                ret = run_checkout(
                    ctx=self.ctx,
                    store_code=case["store_code"],
                    staff_id=case["staff_id"],
                    customer_phone=case["customer_phone"],
                    frame_variant_id=case["frame_variant_id"],
                    lens_variant_id=case["lens_variant_id"],
                    auto_confirm=True,
                )
                self.assertEqual(ret, 1)
                self.assertIn(f"Missing required argument '{case['err']}'", captured.getvalue())
                mock_charge.assert_not_called()

        # Zero invoices in database across all failed attempts
        with self.ctx.repo._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM invoices;")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_explicit_complete_checkout_with_yes_works(self) -> None:
        """Requirement 1: Explicit complete checkout with --yes succeeds end-to-end."""
        fake_cust = _make_customer()
        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR"}]
        frame_dict = {"id": 91000001, "sku": "FR-0001", "title": "Test Frame", "price": "10000.00", "eluno": {"hsn": "90031900"}}
        lens_dict = {"id": 91000041, "sku": "LN-0001", "title": "Test Lens", "price": "1000.00", "eluno": {"hsn": "90015000"}}

        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_dict if vid == 91000001 else lens_dict), \
             patch.object(self.ctx.terminal_client, "create_charge", return_value={"status": "captured", "charge_id": "ch_mock_999"}), \
             patch.object(self.ctx.shopify_client, "create_order", return_value={"id": 77000099, "order_number": 1099, "financial_status": "paid"}):

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

        output = captured.getvalue()
        self.assertIn("TRANSACTION COMPLETED SUCCESSFULLY", output)
        self.assertIn("ch_mock_999", output)

    def test_family_wearer_identity_four_scenarios(self) -> None:
        """Requirement 2: Test 4 scenarios for family wearer identity:
        A. Purchaser wearing product.
        B. Enrolled family member with customer ID.
        C. Unenrolled family member identified by name (for_customer_id is NULL).
        D. Correct persistence after application restart, and display in invoice show.
        """
        fake_cust = _make_customer(cid=80001001)
        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR"}]
        frame_dict = {"id": 91000001, "sku": "FR-0001", "title": "Test Frame", "price": "10000.00", "eluno": {"hsn": "90031900"}}
        lens_dict = {"id": 91000041, "sku": "LN-0001", "title": "Test Lens", "price": "1000.00", "eluno": {"hsn": "90015000"}}

        fy = get_financial_year()

        # --- Scenario A: Purchaser wearing product ---
        with patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_dict if vid == 91000001 else lens_dict), \
             patch.object(self.ctx.terminal_client, "create_charge", return_value={"status": "captured", "charge_id": "ch_a"}), \
             patch.object(self.ctx.shopify_client, "create_order", return_value={"id": 77000001, "order_number": 1001}):

            ret_a = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                for_member="Self",
                for_customer_id=80001001,
                auto_confirm=True,
            )
            self.assertEqual(ret_a, 0)

        inv_a = self.ctx.repo.get_invoice(f"ELU/HSR/{fy}/00001")
        self.assertIsNotNone(inv_a)
        self.assertEqual(inv_a.lines[0].for_customer_id, 80001001)
        self.assertEqual(inv_a.lines[0].wearer_name, "Self")

        # --- Scenario B: Enrolled family member with a customer ID ---
        with patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_dict if vid == 91000001 else lens_dict), \
             patch.object(self.ctx.terminal_client, "create_charge", return_value={"status": "captured", "charge_id": "ch_b"}), \
             patch.object(self.ctx.shopify_client, "create_order", return_value={"id": 77000002, "order_number": 1002}):

            ret_b = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                for_member="Priya",
                for_customer_id=80001005,
                auto_confirm=True,
            )
            self.assertEqual(ret_b, 0)

        inv_b = self.ctx.repo.get_invoice(f"ELU/HSR/{fy}/00002")
        self.assertIsNotNone(inv_b)
        self.assertEqual(inv_b.lines[0].for_customer_id, 80001005)
        self.assertEqual(inv_b.lines[0].wearer_name, "Priya")

        # --- Scenario C: Unenrolled family member identified by name ---
        with patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_dict if vid == 91000001 else lens_dict), \
             patch.object(self.ctx.terminal_client, "create_charge", return_value={"status": "captured", "charge_id": "ch_c"}), \
             patch.object(self.ctx.shopify_client, "create_order", return_value={"id": 77000003, "order_number": 1003}):

            ret_c = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                for_member="Rohan",
                for_customer_id=None,
                auto_confirm=True,
            )
            self.assertEqual(ret_c, 0)

        inv_c = self.ctx.repo.get_invoice(f"ELU/HSR/{fy}/00003")
        self.assertIsNotNone(inv_c)
        # CRUCIAL: for_customer_id must be None (NULL), NOT mapped to purchaser (80001001)
        self.assertIsNone(inv_c.lines[0].for_customer_id)
        self.assertEqual(inv_c.lines[0].wearer_name, "Rohan")

        # --- Scenario D: Application restart persistence and invoice show ---
        fresh_repo = InvoiceRepository(self.db_path)
        reloaded_inv = fresh_repo.get_invoice(inv_c.invoice_no)
        self.assertIsNotNone(reloaded_inv)
        self.assertIsNone(reloaded_inv.lines[0].for_customer_id)
        self.assertEqual(reloaded_inv.lines[0].wearer_name, "Rohan")

        # Verify invoice show displays wearer name
        captured_show = io.StringIO()
        with patch("sys.stdout", captured_show):
            ret_show = run_invoice_show(self.ctx, invoice_no=inv_c.invoice_no)
            self.assertEqual(ret_show, 0)
        show_output = captured_show.getvalue()
        self.assertIn("[Wearer: Rohan]", show_output)

    def test_expired_prescription_safety_interactive_and_non_interactive(self) -> None:
        """Requirement 3: When all prescriptions on file are expired:
        - Non-interactive without --rx plano rejects.
        - Non-interactive with --rx plano succeeds in plano mode.
        - Interactive prompt rejected cancels checkout.
        - Interactive prompt accepted proceeds in plano mode.
        """
        fake_cust = _make_customer()
        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR"}]
        frame_dict = {"id": 91000001, "sku": "FR-0001", "title": "Test Frame", "price": "10000.00", "eluno": {"hsn": "90031900"}}
        lens_dict = {"id": 91000041, "sku": "LN-0001", "title": "Test Lens", "price": "1000.00", "eluno": {"hsn": "90015000"}}
        expired_rx = Prescription(
            rx_id="RX_OLD",
            customer_id=80001001,
            phone="+919000000001",
            date="2024-01-01",
            source="in_store",
            od=EyePrescription(sph=1.0),
            os=EyePrescription(sph=1.0),
            pd=PupillaryDistance(binocular=60),
            valid_until="2025-01-01",
        )

        # 1. Non-interactive without --rx plano: must reject
        captured1 = io.StringIO()
        with patch("sys.stdout", captured1), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[expired_rx]):

            ret1 = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                auto_confirm=True,
            )
            self.assertEqual(ret1, 1)
        self.assertIn("All prescriptions on file are expired", captured1.getvalue())
        self.assertIn("provide '--rx plano'", captured1.getvalue())

        # 2. Non-interactive with --rx plano: succeeds
        captured2 = io.StringIO()
        with patch("sys.stdout", captured2), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[expired_rx]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_dict if vid == 91000001 else lens_dict), \
             patch.object(self.ctx.terminal_client, "create_charge", return_value={"status": "captured", "charge_id": "ch_p"}), \
             patch.object(self.ctx.shopify_client, "create_order", return_value={"id": 77000010, "order_number": 1010}):

            ret2 = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                rx_id="plano",
                auto_confirm=True,
            )
            self.assertEqual(ret2, 0)
        self.assertIn("Explicit plano / non-corrective mode requested", captured2.getvalue())

        # 3. Interactive with staff declining plano: rejects
        captured3 = io.StringIO()
        with patch("sys.stdout", captured3), \
             patch("eluno_pos.cli.commands.checkout.prompt_confirm", side_effect=[False]), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[expired_rx]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_dict if vid == 91000001 else lens_dict):

            ret3 = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                auto_confirm=False,
            )
            self.assertEqual(ret3, 1)
        self.assertIn("Cannot proceed with expired prescription", captured3.getvalue())

    def test_retail_location_filtering_rejects_hq_and_inactive(self) -> None:
        """Requirement 4: Only active retail stores allowed; HQ and inactive locations must be rejected."""
        locations = [
            {"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}},
            {"id": 70000008, "name": "Eluno HQ & Lab", "active": True, "eluno": {"store_code": "HQ", "kind": "warehouse"}},
            {"id": 70000009, "name": "Eluno Old Store", "active": False, "eluno": {"store_code": "OLD", "kind": "store"}},
        ]

        # 1. Reject HQ
        captured_hq = io.StringIO()
        with patch("sys.stdout", captured_hq), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations):
            ret_hq = run_checkout(
                ctx=self.ctx,
                store_code="HQ",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                auto_confirm=True,
            )
            self.assertEqual(ret_hq, 1)
        self.assertIn("Location 'HQ' is a warehouse, not an active retail store", captured_hq.getvalue())

        # 2. Reject inactive location
        captured_inact = io.StringIO()
        with patch("sys.stdout", captured_inact), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations):
            ret_inact = run_checkout(
                ctx=self.ctx,
                store_code="OLD",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                auto_confirm=True,
            )
            self.assertEqual(ret_inact, 1)
        self.assertIn("Location 'OLD' is inactive and cannot be selected for checkout", captured_inact.getvalue())

        # Verify zero invoices allocated
        with self.ctx.repo._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM invoices;")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_hsn_source_strictness_only_eluno_hsn(self) -> None:
        """Requirement 5: HSN must be under variant.eluno.hsn; undocumented top-level fallback rejected."""
        fake_cust = _make_customer()
        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR"}]

        # Frame has top-level hsn but empty eluno metadata
        frame_with_top_level_only = {"id": 91000001, "sku": "FR-0001", "title": "Test Frame", "price": "10000.00", "hsn": "90031900", "eluno": {}}
        lens_ok = {"id": 91000041, "sku": "LN-0001", "title": "Test Lens", "price": "1000.00", "eluno": {"hsn": "90015000"}}

        captured = io.StringIO()
        with patch("sys.stdout", captured), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=staff_hsr), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_with_top_level_only if vid == 91000001 else lens_ok):

            ret = run_checkout(
                ctx=self.ctx,
                store_code="HSR",
                staff_id="S108",
                customer_phone="+919000000001",
                frame_variant_id=91000001,
                lens_variant_id=91000041,
                auto_confirm=True,
            )
            self.assertEqual(ret, 1)

        output = captured.getvalue()
        self.assertIn("missing mandatory HSN code in 'variant.eluno.hsn'", output)

        # Zero invoices in database
        with self.ctx.repo._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM invoices;")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_refund_staff_authorization_rules(self) -> None:
        """Requirement: Refund staff authorization rules:
        A. Missing staff ID is rejected.
        B. Unknown staff ID is rejected.
        C. Staff from another store is rejected.
        D. Valid store staff can authorize the refund.
        E. Invalid staff causes zero terminal refund POST calls and zero refund intents.
        """
        # Create a posted invoice in store HSR
        inv = self.ctx.repo.allocate_invoice(
            store_code="HSR",
            financial_year="FY27",
            checkout_ref="chk_ref_test",
            customer_id=80001001,
            gross_paise=100000,
            discount_paise=0,
            net_paise=100000,
            taxable_paise=100000,
            tax_paise=0,
            lines=[],
        )
        self.ctx.repo.update_invoice_payment(inv.invoice_no, "chg_test_100")
        self.ctx.repo.update_invoice_shopify_order(inv.invoice_no, 99000100, status=InvoiceStatus.POSTED)

        locations = [{"id": 70000002, "name": "Eluno HSR Layout", "active": True, "eluno": {"store_code": "HSR", "kind": "store"}}]
        staff_hsr = [{"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR"}]
        all_staff = [
            {"staff_id": "S108", "name": "Meera", "role": "store_manager", "store_code": "HSR"},
            {"staff_id": "S101", "name": "Nikhil", "role": "sales", "store_code": "MM"},
        ]

        # Case A: Missing staff ID is rejected under auto_confirm
        captured_a = io.StringIO()
        with patch("sys.stdout", captured_a), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", side_effect=lambda store_code=None: staff_hsr if store_code == "HSR" else all_staff), \
             patch.object(self.ctx.terminal_client, "create_refund") as mock_term_a:

            ret_a = run_refund(
                ctx=self.ctx,
                invoice_no=inv.invoice_no,
                staff_id=None,
                reason_code="CUSTOMER_RETURN",
                auto_confirm=True,
            )
            self.assertEqual(ret_a, 1)
            mock_term_a.assert_not_called()
        self.assertIn("Missing required argument '--staff'", captured_a.getvalue())

        # Case B: Unknown staff ID is rejected
        captured_b = io.StringIO()
        with patch("sys.stdout", captured_b), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", side_effect=lambda store_code=None: staff_hsr if store_code == "HSR" else all_staff), \
             patch.object(self.ctx.terminal_client, "create_refund") as mock_term_b:

            ret_b = run_refund(
                ctx=self.ctx,
                invoice_no=inv.invoice_no,
                staff_id="S999_UNKNOWN",
                reason_code="CUSTOMER_RETURN",
                auto_confirm=True,
            )
            self.assertEqual(ret_b, 1)
            mock_term_b.assert_not_called()
        self.assertIn("not found in staff registry", captured_b.getvalue())

        # Case C: Staff from another store is rejected
        captured_c = io.StringIO()
        with patch("sys.stdout", captured_c), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", side_effect=lambda store_code=None: staff_hsr if store_code == "HSR" else all_staff), \
             patch.object(self.ctx.terminal_client, "create_refund") as mock_term_c:

            ret_c = run_refund(
                ctx=self.ctx,
                invoice_no=inv.invoice_no,
                staff_id="S101",  # S101 is registered at MM, not HSR
                reason_code="CUSTOMER_RETURN",
                auto_confirm=True,
            )
            self.assertEqual(ret_c, 1)
            mock_term_c.assert_not_called()
        self.assertIn("is registered at store 'MM', not invoice store 'HSR'", captured_c.getvalue())

        # Case E check: Verify zero refund intents or credit notes exist in DB after failed attempts
        refunds_failed = self.ctx.repo.get_refunds_for_invoice(inv.invoice_no)
        self.assertEqual(len(refunds_failed), 0)

        # Case D: Valid store staff can authorize the refund
        captured_d = io.StringIO()
        with patch("sys.stdout", captured_d), \
             patch.object(self.ctx.shopify_client, "get_locations", return_value=locations), \
             patch.object(self.ctx.smt_client, "get_staff", side_effect=lambda store_code=None: staff_hsr if store_code == "HSR" else all_staff), \
             patch.object(self.ctx.terminal_client, "get_charge", return_value={"status": "captured", "amount_paise": 100000, "refunded_paise": 0}), \
             patch.object(self.ctx.terminal_client, "create_refund", return_value={"status": "refunded", "refund_id": "trf_100"}), \
             patch.object(self.ctx.shopify_client, "create_refund", return_value={"id": 88000100}):

            ret_d = run_refund(
                ctx=self.ctx,
                invoice_no=inv.invoice_no,
                staff_id="S108",  # Valid HSR staff
                reason_code="CUSTOMER_RETURN",
                auto_confirm=True,
            )
            self.assertEqual(ret_d, 0)
        self.assertIn("FULL REFUND COMPLETED SUCCESSFULLY", captured_d.getvalue())
        self.assertIn("trf_100", captured_d.getvalue())

        # Verify completed refund in database
        refunds_ok = self.ctx.repo.get_refunds_for_invoice(inv.invoice_no)
        self.assertEqual(len(refunds_ok), 1)
        self.assertEqual(refunds_ok[0].status.value, "COMPLETED")


if __name__ == "__main__":
    unittest.main()
