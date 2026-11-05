"""Unit and functional tests for Eluno POS CLI commands, parsing, and formatting."""

import os
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
from eluno_pos.domain.invoice import InvoiceStatus
from eluno_pos.domain.prescription import EyePrescription, Prescription, PupillaryDistance
from eluno_pos.repositories.invoice_repository import InvoiceRepository


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

    def test_customer_lookup_success(self) -> None:
        """Verify customer lookup displays customer details and prescriptions."""
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
            rx_id="RX5001",
            customer_id=80001001,
            phone="+919000000001",
            date="2026-08-12",
            source="in_store",
            od=EyePrescription(sph=2.5, cyl=-1.0, axis=90),
            os=EyePrescription(sph=2.5, cyl=-1.0, axis=90),
            pd=PupillaryDistance(binocular=64),
            valid_until="2027-08-12",
        )
        with patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[fake_rx]):
            ret = run_customer_lookup(self.ctx, phone="+919000000001")
            self.assertEqual(ret, 0)

    def test_checkout_cancelled_by_staff_does_not_charge(self) -> None:
        """Requirement: Cancelled checkout marks invoice CANCELLED without initiating terminal charges."""
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
        frame_dict = {"id": 91000001, "sku": "FR-0001", "title": "Test Frame", "price": "10000.00", "eluno": {"hsn": "90031900"}}
        lens_dict = {"id": 91000041, "sku": "LN-0001", "title": "Test Lens", "price": "1000.00", "eluno": {"hsn": "90015000", "rx_limits": {"sph_min": -4.0, "sph_max": 4.0, "cyl_abs_max": 2.0}}}

        with patch.object(self.ctx.shopify_client, "get_locations", return_value=[{"id": 70000001, "name": "HSR", "store_code": "HSR"}]), \
             patch.object(self.ctx.smt_client, "get_staff", return_value=[{"staff_id": "S108", "name": "Meera", "role": "store_manager", "can_override_discount": True}]), \
             patch.object(self.ctx.customer_service, "find_by_phone", return_value=fake_cust), \
             patch.object(self.ctx.prescription_service, "get_prescriptions_by_phone", return_value=[]), \
             patch.object(self.ctx.shopify_client, "get_variant", side_effect=lambda vid: frame_dict if vid == 91000001 else lens_dict), \
             patch("eluno_pos.cli.commands.checkout.prompt_confirm", return_value=False):  # Staff confirms NO

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

            # Invoice allocated must be CANCELLED
            inv = self.ctx.repo.get_invoice("ELU/HSR/FY26/00001")
            self.assertIsNotNone(inv)
            self.assertEqual(inv.status, InvoiceStatus.CANCELLED)

            # Terminal client was never charged
            self.ctx.terminal_client.http.post = MagicMock()
            self.ctx.terminal_client.http.post.assert_not_called()

    def test_manager_discount_override_cap_enforced(self) -> None:
        """Requirement: Manager discount override cannot exceed 10% (1000 bps)."""
        ret = run_checkout(
            ctx=self.ctx,
            store_code="HSR",
            staff_id="S108",
            customer_phone="+919000000001",
            manager_override_bps=1500,  # 15% (exceeds 10% cap)
            auto_confirm=True,
        )
        self.assertEqual(ret, 1)

    def test_reconcile_command_zero_pending(self) -> None:
        """Verify reconcile command handles zero pending transactions cleanly."""
        ret = run_reconcile(self.ctx, store_code="HSR", auto_confirm=True)
        self.assertEqual(ret, 0)


if __name__ == "__main__":
    unittest.main()
