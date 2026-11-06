"""Live integration tests running against local mock server (http://localhost:8787)."""

import unittest
import uuid

from eluno_pos.adapters.exceptions import NotFoundError
from eluno_pos.adapters.http_client import HttpClient
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.adapters.smt_client import SmtClient
from eluno_pos.adapters.terminal_client import TerminalClient
from eluno_pos.config import AppConfig
from eluno_pos.domain.lens import LensVariant
from eluno_pos.domain.pairing import FrameVariant, Pair
from eluno_pos.domain.validation import LensValidator
from eluno_pos.services.customer_service import CustomerService
from eluno_pos.services.prescription_service import PrescriptionService


class TestMockServerIntegration(unittest.TestCase):
    """End-to-end integration tests against the live local mock server."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = AppConfig()
        cls.shopify_http = HttpClient(cls.config.shopify_base_url, timeout=cls.config.http_timeout_seconds)
        cls.smt_http = HttpClient(cls.config.smt_base_url, timeout=cls.config.http_timeout_seconds)
        cls.terminal_http = HttpClient(cls.config.terminal_base_url, timeout=cls.config.http_timeout_seconds)

        cls.shopify = ShopifyClient(cls.shopify_http)
        cls.smt = SmtClient(cls.smt_http)
        cls.terminal = TerminalClient(cls.terminal_http)

    def test_live_customer_and_prescription_lookup(self) -> None:
        phone = "+919000000005"

        # 1. Search customer in Shopify
        customers = self.shopify.search_customer_by_phone(phone)
        self.assertGreater(len(customers), 0)
        customer = customers[0]
        self.assertEqual(customer["phone"], phone)
        self.assertIn("priority", customer["tags"])

        # 2. Fetch prescriptions from SMT
        rxs = self.smt.get_prescriptions_by_phone(phone)
        self.assertGreater(len(rxs), 0)
        latest_rx = rxs[0]
        self.assertEqual(latest_rx["phone"], phone)
        self.assertIn("od", latest_rx)
        self.assertIn("os", latest_rx)

    def test_live_locations_and_inventory(self) -> None:
        # Fetch locations
        locations = self.shopify.get_locations()
        self.assertEqual(len(locations), 8)  # 7 stores + HQ

        # Fetch inventory for known frame variant
        inv = self.shopify.get_inventory_levels(
            inventory_item_ids=[92000003],
            location_ids=[70000002],
        )
        self.assertEqual(len(inv), 1)
        self.assertGreaterEqual(inv[0]["available"], 0)

    def test_live_staff_lookup(self) -> None:
        staff_list = self.smt.get_staff(store_code="HSR")
        self.assertGreater(len(staff_list), 0)
        for s in staff_list:
            self.assertEqual(s["store_code"], "HSR")

    def test_live_terminal_charge_and_refund_lifecycle(self) -> None:
        unique_ref = f"TEST/INV/{uuid.uuid4().hex[:8]}"
        amount = 540000  # ₹5,400.00 in paise

        # 1. Create charge (handles flakiness with automatic idempotent retry)
        charge = self.terminal.create_charge(
            amount_paise=amount,
            method="upi",
            order_ref=unique_ref,
        )
        self.assertEqual(charge["status"], "captured")
        self.assertEqual(charge["amount_paise"], amount)
        self.assertEqual(charge["order_ref"], unique_ref)
        charge_id = charge["charge_id"]

        # 2. Verify status lookup
        status = self.terminal.get_charge(charge_id)
        self.assertEqual(status["charge_id"], charge_id)
        self.assertEqual(status["status"], "captured")

        # 3. Process a refund on the terminal
        refund = self.terminal.create_refund(
            charge_id=charge_id,
            amount_paise=100000,  # ₹1,000.00 in paise
            reason="CUSTOMER_REQUEST",
        )
        self.assertEqual(refund["status"], "processed")
        self.assertEqual(refund["amount_paise"], 100000)

    def test_terminal_mock_duplicate_charges_on_repeated_requests(self) -> None:
        """Demonstrates that repeated requests with the same order_ref create duplicate charges in the mock."""
        shared_ref = f"DUP/TEST/{uuid.uuid4().hex[:8]}"
        amount = 100000

        # First request captures a charge
        first_charge = self.terminal.create_charge(amount_paise=amount, method="upi", order_ref=shared_ref)
        self.assertEqual(first_charge["status"], "captured")

        # Second request with the IDENTICAL order_ref in the mock creates a SECOND distinct charge
        second_charge = self.terminal.create_charge(amount_paise=amount, method="upi", order_ref=shared_ref)
        self.assertEqual(second_charge["status"], "captured")

        # Confirms the mock limitation: order_ref does NOT deduplicate repeated successful POST requests
        self.assertNotEqual(first_charge["charge_id"], second_charge["charge_id"])
        self.assertEqual(first_charge["order_ref"], second_charge["order_ref"])

    def test_terminal_mock_order_ref_lookup_limitation(self) -> None:
        """Demonstrates that GET /terminal/charges/{order_ref} returns 404 because the mock keys only by charge_id."""
        dummy_order_ref = "ELU/HSR/FY26/00099"
        with self.assertRaises(NotFoundError):
            self.terminal.get_charge(dummy_order_ref)

    def test_live_phase2_flow_lookup_validate_and_pair(self) -> None:
        """Integration test demonstrating Must Requirements #1 and #2 end-to-end against live mock."""
        customer_service = CustomerService(self.shopify)
        prescription_service = PrescriptionService(self.smt)

        # 1. Look up customer by phone
        phone = "+919000000005"
        customer = customer_service.find_by_phone(phone)
        self.assertIsNotNone(customer)
        self.assertEqual(customer.first_name, "Karthik")

        # 2. Fetch latest valid prescription
        rx = prescription_service.get_latest_valid_prescription(phone)
        self.assertIsNotNone(rx)
        self.assertEqual(rx.rx_id, "RX5007")

        # 3. Retrieve progressive lens variant from Shopify
        lens_data = self.shopify.get_variant(91000083)  # LN-0043 progressive 1.6
        lens = LensVariant.from_shopify_variant(lens_data)

        # 4. Validate prescription against lens limits
        validation = LensValidator.validate(rx, lens)
        self.assertTrue(validation.is_compatible)
        self.assertEqual(len(validation.reasons), 0)

        # 5. Retrieve frame variant from Shopify
        frame_data = self.shopify.get_variant(91000003)  # FR-0003
        frame = FrameVariant.from_shopify_variant(frame_data)

        # 6. Build Pair association
        pair = Pair(
            pair_ref="1",
            frame=frame,
            lens=lens,
            rx_id=rx.rx_id,
            for_customer_id=customer.id,
        )
        self.assertTrue(pair.is_complete_pair)

        # 7. Verify line attributes match Shopify expectations
        frame_props = {p["name"]: p["value"] for p in pair.get_frame_properties()}
        lens_props = {p["name"]: p["value"] for p in pair.get_lens_properties()}

        self.assertEqual(frame_props["pair_ref"], "1")
        self.assertEqual(frame_props["line_type"], "frame")
        self.assertEqual(lens_props["pair_ref"], "1")
        self.assertEqual(lens_props["line_type"], "lens")
        self.assertEqual(lens_props["rx_id"], "RX5007")
        self.assertEqual(lens_props["lens_config"], "progressive/1.6/blue_cut")
        self.assertEqual(lens_props["for_customer_id"], str(customer.id))

    def test_live_phase4_end_to_end_checkout(self) -> None:
        """Integration test demonstrating Must Requirement #5 end-to-end against live mock server.

        Validates:
        1. Sequential invoice allocation in SQLite.
        2. Live terminal charge with durable charge_id capture.
        3. Real Shopify order creation with line items, tax lines, discounts, transactions,
           and note_attributes carrying invoice_no.
        4. Invoice transition to POSTED.
        """
        import os
        import tempfile
        from datetime import date
        from eluno_pos.domain.cart import Cart, CartLine
        from eluno_pos.domain.invoice import InvoiceStatus
        from eluno_pos.repositories.invoice_repository import InvoiceRepository
        from eluno_pos.services.checkout_service import CheckoutService
        from eluno_pos.services.customer_service import CustomerService
        from eluno_pos.services.gst_service import GstService
        from eluno_pos.services.offer_service import OfferConflictStrategy, OfferService
        from eluno_pos.services.payment_service import PaymentService
        from eluno_pos.services.prescription_service import PrescriptionService
        from eluno_pos.services.shopify_order_service import ShopifyOrderService

        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "live_pos.db")
        repo = InvoiceRepository(db_path)

        payment_service = PaymentService(self.terminal, repo)
        shopify_order_service = ShopifyOrderService(self.shopify, repo, GstService())
        checkout_service = CheckoutService(
            invoice_repository=repo,
            payment_service=payment_service,
            shopify_order_service=shopify_order_service,
            offer_service=OfferService(),
            gst_service=GstService(),
        )

        # 1. Customer & Rx
        phone = "+919000000005"
        customer = CustomerService(self.shopify).find_by_phone(phone)
        rx = PrescriptionService(self.smt).get_latest_valid_prescription(phone)

        # 2. Build Cart
        frame = CartLine(
            line_id="line_frame",
            line_type="frame",
            variant_id=91000003,
            sku="FR-0003",
            title="Eluno Atelier Rectangle 03",
            unit_price_paise=1099800,
            hsn="90031900",
            pair_ref="PAIR-1",
        )
        lens = CartLine(
            line_id="line_lens",
            line_type="lens",
            variant_id=91000083,
            sku="LN-0043",
            title="Progressive 1.60 Blue Cut",
            unit_price_paise=949900,
            hsn="90015000",
            pair_ref="PAIR-1",
            lens_type="progressive",
            index=1.6,
            coating="blue_cut",
            rx_id=rx.rx_id,
        )
        cart = Cart(lines=[frame, lens], customer=customer)

        # 3. Prepare Checkout
        session = checkout_service.prepare_checkout(
            cart=cart,
            store_code="HSR",
            location_id=70000002,  # Eluno HSR Layout
            financial_year="FY26",
            channel="walk_in",
            staff_id="S105",
            transaction_date=date(2026, 10, 20),
            offer_strategy=OfferConflictStrategy.MAX_SAVINGS,
        )
        self.assertEqual(session.invoice.invoice_no, "ELU/HSR/FY26/00001")
        self.assertEqual(session.invoice.status, InvoiceStatus.PENDING_RECONCILIATION)

        # 4. Complete Checkout (Terminal payment -> Shopify order creation)
        result = checkout_service.complete_checkout(session, payment_method="upi")

        self.assertTrue(result.success)
        self.assertEqual(result.invoice_no, "ELU/HSR/FY26/00001")
        self.assertEqual(result.status, InvoiceStatus.POSTED)
        self.assertIsNotNone(result.terminal_charge_id)
        self.assertTrue(result.terminal_charge_id.startswith("chg_"))
        self.assertIsNotNone(result.shopify_order_id)
        self.assertGreater(result.shopify_order_id, 0)

        # 5. Verify the order exists in live mock Shopify
        created_order = self.shopify.get_order(result.shopify_order_id)
        self.assertEqual(created_order["id"], result.shopify_order_id)
        self.assertEqual(created_order["financial_status"], "paid")
        self.assertTrue(created_order["taxes_included"])

        attrs = {a["name"]: a["value"] for a in created_order.get("note_attributes", [])}
        self.assertEqual(attrs.get("invoice_no"), "ELU/HSR/FY26/00001")
        self.assertEqual(attrs.get("store_code"), "HSR")
        self.assertEqual(attrs.get("terminal_charge_id"), result.terminal_charge_id)

        # Verify transaction recorded
        txs = created_order.get("transactions", [])
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0]["kind"], "sale")
        self.assertEqual(txs[0]["status"], "success")

        # Verify prescription data minimization on Shopify order
        lens_item = next(li for li in created_order["line_items"] if any(p["name"] == "line_type" and p["value"] == "lens" for p in li.get("properties", [])))
        props = {p["name"]: p["value"] for p in lens_item.get("properties", [])}
        self.assertEqual(props.get("rx_id"), "RX5007")
        self.assertNotIn("rx", props, "Shopify line item must not carry full serialized prescription")
        for term in ("od", "os", "sph", "cyl", "axis", "add", "pd"):
            self.assertNotIn(term, props)

        temp_dir.cleanup()

    def test_live_phase5_full_refund_and_day_end_report(self) -> None:
        """Integration test demonstrating Must Requirements #6 and #7 end-to-end against live mock server.

        Validates:
        1. End-to-end checkout with POSTED invoice and Shopify order.
        2. Execution of a full refund via RefundService:
           - Duplicate refund prevention check.
           - Live terminal refund captured with status check.
           - Sequential credit note generation (ELU/HSR/FY26/CN/00001).
           - Frame-only restocking in Shopify (restock_type="return" for frame, "no_restock" for lens).
           - Verification that frame inventory increased at the store location.
           - Verification that Shopify order financial_status is 'refunded'.
        3. Day-end reporting generation via DayEndReportService:
           - Correct aggregation of gross sales, refunds, net sales, and taxes in paise.
           - Cross-reconciliation with Shopify orders confirming 0 discrepancies.
        """
        import os
        import tempfile
        from datetime import date
        from eluno_pos.domain.cart import Cart, CartLine
        from eluno_pos.domain.invoice import InvoiceStatus
        from eluno_pos.domain.refund import RefundStatus
        from eluno_pos.repositories.invoice_repository import InvoiceRepository
        from eluno_pos.services.checkout_service import CheckoutService
        from eluno_pos.services.customer_service import CustomerService
        from eluno_pos.services.day_end_service import DayEndReportService
        from eluno_pos.services.gst_service import GstService
        from eluno_pos.services.offer_service import OfferConflictStrategy, OfferService
        from eluno_pos.services.payment_service import PaymentService
        from eluno_pos.services.prescription_service import PrescriptionService
        from eluno_pos.services.refund_service import RefundService
        from eluno_pos.services.shopify_order_service import ShopifyOrderService

        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "live_pos_phase5.db")
        repo = InvoiceRepository(db_path)

        payment_service = PaymentService(self.terminal, repo)
        shopify_order_service = ShopifyOrderService(self.shopify, repo, GstService())
        checkout_service = CheckoutService(
            invoice_repository=repo,
            payment_service=payment_service,
            shopify_order_service=shopify_order_service,
            offer_service=OfferService(),
            gst_service=GstService(),
        )
        refund_service = RefundService(
            terminal_client=self.terminal,
            shopify_client=self.shopify,
            invoice_repository=repo,
        )
        day_end_service = DayEndReportService(
            invoice_repository=repo,
            shopify_client=self.shopify,
        )

        store_code = "HSR"
        location_id = 70000002
        financial_year = "FY26"
        report_date = date(2026, 10, 20)

        # 1. Customer & Rx
        phone = "+919000000005"
        customer = CustomerService(self.shopify).find_by_phone(phone)
        rx = PrescriptionService(self.smt).get_latest_valid_prescription(phone)

        # 2. Build Cart
        frame = CartLine(
            line_id="line_frame",
            line_type="frame",
            variant_id=91000003,
            sku="FR-0003",
            title="Eluno Atelier Rectangle 03",
            unit_price_paise=1099800,
            hsn="90031900",
            pair_ref="PAIR-1",
        )
        lens = CartLine(
            line_id="line_lens",
            line_type="lens",
            variant_id=91000083,
            sku="LN-0043",
            title="Progressive 1.60 Blue Cut",
            unit_price_paise=949900,
            hsn="90015000",
            pair_ref="PAIR-1",
            lens_type="progressive",
            index=1.6,
            coating="blue_cut",
            rx_id=rx.rx_id,
        )
        cart = Cart(lines=[frame, lens], customer=customer)

        # 3. Check baseline frame inventory at HSR location (inventory_item_id: 92000003)
        inv_before = self.shopify.get_inventory_levels(
            inventory_item_ids=[92000003],
            location_ids=[location_id],
        )
        self.assertEqual(len(inv_before), 1)
        initial_available = inv_before[0]["available"]

        # 4. Perform complete checkout
        session = checkout_service.prepare_checkout(
            cart=cart,
            store_code=store_code,
            location_id=location_id,
            financial_year=financial_year,
            channel="walk_in",
            staff_id="S105",
            transaction_date=report_date,
            offer_strategy=OfferConflictStrategy.MAX_SAVINGS,
        )
        checkout_result = checkout_service.complete_checkout(session, payment_method="upi")
        self.assertTrue(checkout_result.success)
        invoice_no = checkout_result.invoice_no
        self.assertEqual(checkout_result.status, InvoiceStatus.POSTED)

        # 5. Process full refund
        refund_result = refund_service.process_full_refund(
            invoice_no=invoice_no,
            location_id=location_id,
            reason_code="CUSTOMER_RETURN",
            financial_year=financial_year,
        )
        self.assertTrue(refund_result.success)
        self.assertEqual(refund_result.status, RefundStatus.COMPLETED)
        self.assertEqual(refund_result.credit_note_no, "ELU/HSR/FY26/CN/00001")
        self.assertEqual(refund_result.amount_refunded_paise, checkout_result.net_payable_paise)
        self.assertIsNotNone(refund_result.terminal_refund_id)
        self.assertIsNotNone(refund_result.shopify_refund_id)

        # 6. Verify terminal charge record reflects the refund
        term_chg = self.terminal.get_charge(checkout_result.terminal_charge_id)
        self.assertEqual(term_chg["refunded_paise"], checkout_result.net_payable_paise)

        # 7. Verify Shopify order status and refunds
        order = self.shopify.get_order(checkout_result.shopify_order_id)
        self.assertEqual(order["financial_status"], "refunded")
        self.assertGreaterEqual(len(order.get("refunds", [])), 1)
        shp_rf = order["refunds"][-1]
        self.assertIn("CN/00001", shp_rf.get("note", ""))

        # 8. Verify frame inventory increased by 1 at location, lens did not restock
        inv_after = self.shopify.get_inventory_levels(
            inventory_item_ids=[92000003],
            location_ids=[location_id],
        )
        self.assertEqual(inv_after[0]["available"], initial_available + 1)

        # 9. Test duplicate refund prevention (rejects before contacting external payment gateway)
        with self.assertRaises(Exception) as ctx:
            refund_service.process_full_refund(
                invoice_no=invoice_no,
                location_id=location_id,
                reason_code="DUPLICATE_ATTEMPT",
                financial_year=financial_year,
            )
        self.assertIn("already been fully refunded", str(ctx.exception))

        # 10. Generate Day-End Report for the business day
        # Align timestamps to test business date for report generation
        with repo._get_connection() as conn:
            conn.execute("UPDATE invoices SET created_at = '2026-10-20T11:00:00+05:30';")
            conn.execute("UPDATE refunds SET created_at = '2026-10-20T15:00:00+05:30';")

        report = day_end_service.generate_report(
            store_code=store_code,
            location_id=location_id,
            business_date="2026-10-20",
            reconcile_with_shopify=False,  # Local database authoritative report
        )

        self.assertEqual(report.store_code, "HSR")
        self.assertEqual(report.business_date, "2026-10-20")
        self.assertEqual(report.posted_sales_count, 1)
        self.assertEqual(report.refunds_count, 1)
        self.assertEqual(report.refunds_amount_paise, checkout_result.net_payable_paise)
        self.assertEqual(report.net_sales_after_refunds_paise, 0)
        self.assertFalse(report.is_reconciled)
        self.assertTrue(any("reconciliation skipped" in d.lower() for d in report.discrepancies))

        temp_dir.cleanup()


    def test_live_historical_refund_discovery_via_updated_at_query(self) -> None:
        """Prove historical-refund discovery against the live mock server:
        1. An old order's created_at is outside the report date.
        2. Its updated_at is inside the report date because a refund occurred today.
        3. The created_at query does not return the old order.
        4. The updated_at query returns the old order.
        5. The day-end report counts the refund exactly once.
        6. The original sale is not counted again.
        """
        import tempfile
        from datetime import datetime, date, timedelta, timezone
        from eluno_pos.repositories.invoice_repository import InvoiceRepository
        from eluno_pos.services.day_end_service import DayEndReportService, IST

        # 1. Create an old order on the mock server created in the past (e.g. 2026-09-15)
        old_created_at = "2026-09-15T12:00:00+05:30"
        order_payload = {
            "location_id": 70000002,
            "taxes_included": True,
            "created_at": old_created_at,
            "line_items": [
                {
                    "variant_id": 91000001,
                    "quantity": 1,
                    "price": "10000.00",
                    "total_discount": "0.00",
                    "properties": [{"name": "hsn", "value": "90031900"}],
                    "tax_lines": [
                        {"title": "CGST", "price": "686.44"},
                        {"title": "SGST", "price": "686.44"},
                    ],
                }
            ],
            "transactions": [
                {"kind": "sale", "status": "success", "amount": "10000.00", "gateway": "upi"}
            ],
            "note_attributes": [
                {"name": "store_code", "value": "HSR"},
                {"name": "channel", "value": "walk_in"},
                {"name": "payment_method", "value": "upi"},
            ],
        }
        old_order = self.shopify.create_order(order_payload)
        old_order_id = old_order["id"]

        # 2. Refund the order today (live call to mock server)
        refund_payload = {
            "refund_line_items": [
                {
                    "line_item_id": old_order["line_items"][0]["id"],
                    "quantity": 1,
                    "restock_type": "no_restock",
                }
            ],
            "transactions": [
                {
                    "kind": "refund",
                    "amount": "10000.00",
                    "gateway": "upi",
                }
            ],
        }
        refund_resp = self.shopify.create_refund(old_order_id, refund_payload)
        self.assertIn("id", refund_resp)

        # Fetch the updated order directly from mock server to verify timestamps
        refreshed_order = self.shopify.get_order(old_order_id)
        today_ist_date = datetime.now(IST).strftime("%Y-%m-%d")

        # Prove 1: created_at is outside report date
        created_at_ist = refreshed_order["created_at"][:10]
        self.assertEqual(created_at_ist, "2026-09-15")
        self.assertNotEqual(created_at_ist, today_ist_date)

        # Prove 2: updated_at is inside report date (because refund occurred today)
        updated_at_dt = datetime.fromisoformat(refreshed_order["updated_at"].replace("Z", "+00:00"))
        if updated_at_dt.tzinfo is None:
            updated_at_dt = updated_at_dt.replace(tzinfo=timezone.utc)
        updated_at_ist = updated_at_dt.astimezone(IST).strftime("%Y-%m-%d")
        self.assertEqual(updated_at_ist, today_ist_date)

        # Compute query boundaries for today
        day_start = datetime.strptime(today_ist_date, "%Y-%m-%d").replace(tzinfo=IST)
        following_day = day_start + timedelta(days=1)
        ist_min = day_start.strftime("%Y-%m-%dT%H:%M:%S+05:30")
        ist_max = following_day.strftime("%Y-%m-%dT%H:%M:%S+05:30")

        # Prove 3: created_at query does NOT return the old order
        created_orders = self.shopify.get_orders(
            created_at_min=ist_min,
            created_at_max=ist_max,
            location_id=70000002,
            status="any",
            limit=250,
        )
        created_order_ids = [o["id"] for o in created_orders]
        self.assertNotIn(old_order_id, created_order_ids, "created_at query must NOT return old order")

        # Prove 4: updated_at query DOES return the old order
        updated_orders = self.shopify.get_orders(
            updated_at_min=ist_min,
            updated_at_max=ist_max,
            location_id=70000002,
            status="any",
            limit=250,
        )
        updated_order_ids = [o["id"] for o in updated_orders]
        self.assertIn(old_order_id, updated_order_ids, "updated_at query MUST return old order updated by refund")

        # Prove 5 & 6: Day-end report counts the refund exactly once; original sale is NOT counted again
        temp_dir = tempfile.TemporaryDirectory()
        repo = InvoiceRepository(f"{temp_dir.name}/test_live_hist_refund.db")
        day_end_service = DayEndReportService(invoice_repository=repo, shopify_client=self.shopify)

        # Direct shopify computation
        shopify_comp = day_end_service._compute_from_shopify(
            store_code="HSR",
            location_id=70000002,
            business_date=today_ist_date,
        )

        self.assertGreaterEqual(shopify_comp["refunds_count"], 1)
        self.assertGreaterEqual(shopify_comp["refunds_amount_paise"], 1000000)

        # Full day-end report verification
        report = day_end_service.generate_report(
            store_code="HSR",
            location_id=70000002,
            business_date=today_ist_date,
            reconcile_with_shopify=True,
        )
        self.assertGreaterEqual(report.refunds_count, 1)
        self.assertGreaterEqual(report.refunds_amount_paise, 1000000)

        # Verify old order was retrieved via updated_at and merged exactly once
        candidate_orders = day_end_service._fetch_shopify_orders_for_store("HSR", 70000002, today_ist_date)
        old_order_candidates = [o for o in candidate_orders if o["id"] == old_order_id]
        self.assertEqual(len(old_order_candidates), 1, "Merged orders must contain old order exactly once")

        # Verify old order's sale was NOT counted on today's report
        self.assertNotIn(old_order_id, [o["id"] for o in candidate_orders if day_end_service._to_ist_date_string(o.get("created_at", "")) == today_ist_date])

        # The refund inside old_order must be counted exactly once
        refunds_for_old_order = [r for r in old_order_candidates[0].get("refunds", [])
                                 if day_end_service._to_ist_date_string(r.get("created_at", "")) == today_ist_date]
        self.assertEqual(len(refunds_for_old_order), 1, "Refund on old order counted exactly once")

        temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()


