"""Unit tests for the Modular Offer Engine."""

import unittest
from datetime import date

from eluno_pos.domain.cart import Cart, CartLine, ManagerOverride
from eluno_pos.domain.customer import Customer
from eluno_pos.services.offer_service import OfferConflictStrategy, OfferService


class TestOfferService(unittest.TestCase):
    """Test suite for promotional offer matching, stackability, priority, and line allocations."""

    def setUp(self) -> None:
        self.service = OfferService()
        self.current_date = date(2026, 10, 20)  # During active festive & progressive campaign

        # Standard Customer with priority and progressive campaign tags (Karthik Iyer 80001005)
        self.priority_customer = Customer(
            id=80001005,
            first_name="Karthik",
            last_name="Iyer",
            phone="+919000000005",
            email="karthik.iyer@example.test",
            tags="campaign:progressive_oct26,priority",
            total_spent="52485.00",
            orders_count=4,
            home_store="HSR",
        )

        # Non-priority customer without tags
        self.standard_customer = Customer(
            id=80001000,
            first_name="Priya",
            last_name="Nair",
            phone="+919000000000",
            email=None,
            tags="",
            total_spent="0.00",
            orders_count=0,
        )

        # Frame Line: Atelier Cat-eye 01 (FR-0001), ₹12,998.00 = 1,299,800 paise
        self.frame_line = CartLine(
            line_id="line_frame_1",
            line_type="frame",
            variant_id=91000001,
            sku="FR-0001",
            title="Olive",
            unit_price_paise=1299800,
            quantity=1,
            hsn="90031900",
            pair_ref="PAIR-01",
        )

        # Lens Line: Progressive 1.6 Blue Cut (LN-0043), ₹9,499.00 = 949,900 paise
        self.lens_line = CartLine(
            line_id="line_lens_1",
            line_type="lens",
            variant_id=91000083,
            sku="LN-0043",
            title="blue_cut",
            unit_price_paise=949900,
            quantity=1,
            hsn="90015000",
            pair_ref="PAIR-01",
            lens_type="progressive",
            index=1.6,
            coating="blue_cut",
            rx_id="RX5007",
        )

    def test_active_festive_offer_on_frame(self) -> None:
        # FESTIVE10: 10% on frames (max ₹2,000 = 200,000 paise)
        # Frame price = 1,299,800 paise -> 10% = 129,980 paise (₹1,299.80)
        cart = Cart(lines=[self.frame_line], customer=self.standard_customer)
        res = self.service.evaluate_cart(cart, transaction_date=self.current_date)

        self.assertTrue(res.has_discounts)
        self.assertEqual(len(res.applied_offers), 1)
        applied = res.applied_offers[0]
        self.assertEqual(applied.offer_id, "FESTIVE10")
        self.assertEqual(applied.discount_paise, 129980)
        self.assertEqual(self.frame_line.total_discount_paise, 129980)
        self.assertEqual(self.frame_line.net_price_paise, 1299800 - 129980)

    def test_festive_offer_capped_at_max_discount(self) -> None:
        # Expensive frames exceeding ₹20,000 total: 10% would be > ₹2,000
        expensive_frame = CartLine(
            line_id="line_frame_exp",
            line_type="frame",
            variant_id=91000099,
            sku="FR-EXP",
            title="Platinum",
            unit_price_paise=2500000,  # ₹25,000.00
            quantity=1,
            hsn="90031900",
        )
        cart = Cart(lines=[expensive_frame], customer=self.standard_customer)
        res = self.service.evaluate_cart(cart, transaction_date=self.current_date)

        self.assertEqual(len(res.applied_offers), 1)
        applied = res.applied_offers[0]
        # Must be capped at max_discount_paise = 200000 (₹2,000.00)
        self.assertEqual(applied.discount_paise, 200000)
        self.assertEqual(expensive_frame.net_price_paise, 2500000 - 200000)

    def test_expired_offer_is_never_applied(self) -> None:
        # MONSOON20: valid_to was 2026-08-31, active=False
        cart = Cart(lines=[self.frame_line], customer=self.standard_customer)
        # Evaluate on date where only MONSOON20 was running, but it has active=False
        res = self.service.evaluate_cart(cart, transaction_date=date(2026, 7, 1))
        # MONSOON20 is inactive, so no discount applies
        self.assertFalse(res.has_discounts)

    def test_future_offer_is_not_applied_before_start_date(self) -> None:
        # FESTIVE10 starts 2026-10-01
        # On 2026-09-15, FESTIVE10 is not yet valid
        cart = Cart(lines=[self.frame_line], customer=self.standard_customer)
        res = self.service.evaluate_cart(cart, transaction_date=date(2026, 9, 15))
        applied_ids = [a.offer_id for a in res.applied_offers]
        self.assertNotIn("FESTIVE10", applied_ids)

    def test_customer_tag_eligibility_priority_500(self) -> None:
        # PRIORITY500: ₹500 off cart for customer_tag 'priority'
        # 1. Customer WITH tag
        cart_priority = Cart(lines=[self.frame_line], customer=self.priority_customer)
        # Force strict priority or isolate to PRIORITY500
        service_isolated = OfferService(offers_data=[
            {
                "offer_id": "PRIORITY500",
                "name": "Priority customer ₹500 off",
                "active": True,
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
                "stackable": True,
                "priority": 30,
                "rule": {
                    "applies_to": "cart",
                    "condition": {"customer_tag": "priority"},
                    "discount": {"type": "flat", "value_paise": 50000},
                },
            }
        ])
        res = service_isolated.evaluate_cart(cart_priority, transaction_date=self.current_date)
        self.assertEqual(res.total_discount_paise, 50000)

        # 2. Customer WITHOUT tag
        cart_std = Cart(lines=[self.frame_line], customer=self.standard_customer)
        res_std = service_isolated.evaluate_cart(cart_std, transaction_date=self.current_date)
        self.assertEqual(res_std.total_discount_paise, 0)

    def test_second_pair_50_percent_lenses(self) -> None:
        # SECONDPAIR50: requires min_pairs_in_cart: 2
        # Pair 1: Frame 1 + Progressive Lens ₹9,499.00 (949,900 paise)
        # Pair 2: Frame 2 + Single Vision Lens ₹1,499.00 (149,900 paise)
        frame_2 = CartLine(
            line_id="line_frame_2",
            line_type="frame",
            variant_id=91000002,
            sku="FR-0002",
            title="Tortoise",
            unit_price_paise=1199800,
            pair_ref="PAIR-02",
        )
        lens_2 = CartLine(
            line_id="line_lens_2",
            line_type="lens",
            variant_id=91000042,
            sku="LN-0002",
            title="anti_reflective",
            unit_price_paise=149900,
            pair_ref="PAIR-02",
            lens_type="single_vision",
        )

        cart = Cart(
            lines=[self.frame_line, self.lens_line, frame_2, lens_2],
            customer=self.standard_customer,
        )

        service_second_pair = OfferService(offers_data=[
            {
                "offer_id": "SECONDPAIR50",
                "name": "50% off second pair lenses",
                "active": True,
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
                "stackable": True,
                "priority": 20,
                "rule": {
                    "applies_to": "lens",
                    "condition": {"min_pairs_in_cart": 2},
                    "discount": {
                        "type": "percent",
                        "value_bps": 5000,
                        "on": "lowest_priced_lens_line",
                    },
                },
            }
        ])

        res = service_second_pair.evaluate_cart(cart, transaction_date=self.current_date)
        self.assertEqual(len(res.applied_offers), 1)
        applied = res.applied_offers[0]
        # 50% of lowest lens (₹1,499.00 / 2 = ₹749.50 = 74,950 paise)
        self.assertEqual(applied.discount_paise, 74950)
        self.assertEqual(lens_2.total_discount_paise, 74950)
        self.assertEqual(self.lens_line.total_discount_paise, 0)  # Higher lens untouched

    def test_complete_pair_bundle_300(self) -> None:
        # BUNDLE300: ₹300 off when frame and lens billed together (complete pair)
        # 1. Complete pair in cart
        cart_complete = Cart(lines=[self.frame_line, self.lens_line], customer=self.standard_customer)
        service_bundle = OfferService(offers_data=[
            {
                "offer_id": "BUNDLE300",
                "name": "₹300 off complete pair",
                "active": True,
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
                "stackable": True,
                "priority": 40,
                "rule": {
                    "applies_to": "pair",
                    "condition": {"has_frame_and_lens": True},
                    "discount": {"type": "flat", "value_paise": 30000},
                },
            }
        ])
        res = service_bundle.evaluate_cart(cart_complete, transaction_date=self.current_date)
        self.assertEqual(res.total_discount_paise, 30000)

        # 2. Frame-only cart (incomplete pair)
        cart_frame_only = Cart(lines=[self.frame_line], customer=self.standard_customer)
        res_incomplete = service_bundle.evaluate_cart(cart_frame_only, transaction_date=self.current_date)
        self.assertEqual(res_incomplete.total_discount_paise, 0)

    def test_stackable_offers_combination(self) -> None:
        # Combine BUNDLE300 (₹300) + PRIORITY500 (₹500) + FREE_BLUECUT_PGX (₹1,000)
        # Total stacked savings = ₹1,800 = 180,000 paise
        cart = Cart(lines=[self.frame_line, self.lens_line], customer=self.priority_customer)

        service_stackable = OfferService(offers_data=[
            {
                "offer_id": "BUNDLE300",
                "name": "₹300 pair discount",
                "active": True,
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
                "stackable": True,
                "priority": 40,
                "rule": {
                    "applies_to": "pair",
                    "condition": {"has_frame_and_lens": True},
                    "discount": {"type": "flat", "value_paise": 30000},
                },
            },
            {
                "offer_id": "PRIORITY500",
                "name": "Priority customer ₹500 off",
                "active": True,
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
                "stackable": True,
                "priority": 30,
                "rule": {
                    "applies_to": "cart",
                    "condition": {"customer_tag": "priority"},
                    "discount": {"type": "flat", "value_paise": 50000},
                },
            },
            {
                "offer_id": "FREE_BLUECUT_PGX",
                "name": "Free bluecut upgrade",
                "active": True,
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
                "stackable": True,
                "priority": 25,
                "rule": {
                    "applies_to": "lens",
                    "condition": {"lens_type": "progressive", "index_min": 1.6, "coating": "blue_cut"},
                    "discount": {"type": "flat", "value_paise": 100000},
                },
            },
        ])

        res = service_stackable.evaluate_cart(cart, transaction_date=self.current_date)
        self.assertEqual(len(res.applied_offers), 3)
        self.assertEqual(res.total_discount_paise, 180000)  # 30,000 + 50,000 + 100,000
        # Line prices are reduced without exceeding gross totals
        self.assertEqual(cart.net_total_paise, (1299800 + 949900) - 180000)

    def test_conflict_resolution_max_savings_vs_strict_priority(self) -> None:
        # Scenario:
        # Non-stackable offer FESTIVE10 gives ₹1,299.80 (priority 10)
        # Stackable combination BUNDLE300 (₹300) + PRIORITY500 (₹500) gives ₹800.00
        # Under MAX_SAVINGS: FESTIVE10 wins (₹1,299.80 > ₹800.00)
        # Under STRICT_PRIORITY: FESTIVE10 wins (priority 10 is evaluated first)

        # But suppose FESTIVE10 frame is small (₹5,000 frame -> 10% = ₹500 discount)
        # Stackable bundle BUNDLE300 (₹300) + PRIORITY500 (₹500) + FREE_BLUECUT (₹1,000) = ₹1,800
        cheap_frame = CartLine(
            line_id="line_frame_cheap",
            line_type="frame",
            variant_id=91000010,
            sku="FR-CHEAP",
            title="Crystal",
            unit_price_paise=500000,
            pair_ref="PAIR-01",
        )
        cart = Cart(lines=[cheap_frame, self.lens_line], customer=self.priority_customer)

        # 1. Under MAX_SAVINGS strategy:
        # FESTIVE10 gives ₹500. Stackable bundle gives ₹1,800.
        # Winner must be the stackable bundle!
        res_max = self.service.evaluate_cart(
            cart,
            transaction_date=self.current_date,
            strategy=OfferConflictStrategy.MAX_SAVINGS,
        )
        self.assertEqual(res_max.total_discount_paise, 180000)
        applied_max_ids = {a.offer_id for a in res_max.applied_offers}
        self.assertIn("FREE_BLUECUT_PGX", applied_max_ids)
        self.assertIn("PRIORITY500", applied_max_ids)
        self.assertIn("BUNDLE300", applied_max_ids)
        self.assertNotIn("FESTIVE10", applied_max_ids)

        # 2. Under STRICT_PRIORITY strategy:
        # FESTIVE10 has priority 10 (top priority), is non-stackable, so it takes precedence alone!
        res_strict = self.service.evaluate_cart(
            cart,
            transaction_date=self.current_date,
            strategy=OfferConflictStrategy.STRICT_PRIORITY,
        )
        self.assertEqual(len(res_strict.applied_offers), 1)
        self.assertEqual(res_strict.applied_offers[0].offer_id, "FESTIVE10")
        self.assertEqual(res_strict.total_discount_paise, 50000)

    def test_manager_override_with_reason_code(self) -> None:
        # MANAGER_OVERRIDE: 10% on cart with store_manager and valid reason code
        override = ManagerOverride(
            staff_id="S103",
            role="store_manager",
            reason_code="WALK_OUT_PREVENTION",
            discount_percent_bps=800,  # 8%
        )
        cart = Cart(
            lines=[self.frame_line, self.lens_line],
            customer=self.standard_customer,
            manager_override=override,
        )

        service_manager = OfferService(offers_data=[
            {
                "offer_id": "MANAGER_OVERRIDE",
                "name": "Manager discretionary discount",
                "active": True,
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
                "stackable": False,
                "priority": 99,
                "rule": {
                    "applies_to": "cart",
                    "requires": {"role": "store_manager", "reason_code": True},
                    "discount": {"type": "percent", "value_bps_max": 1000},
                },
            }
        ])

        res = service_manager.evaluate_cart(cart, transaction_date=self.current_date)
        self.assertEqual(len(res.applied_offers), 1)
        applied = res.applied_offers[0]
        self.assertEqual(applied.offer_id, "MANAGER_OVERRIDE")
        # Total cart = 1,299,800 + 949,900 = 2,249,700 paise (₹22,497.00)
        # 8% = round_half_up(2249700 * 800 / 10000) = 179,976 paise (₹1,799.76)
        self.assertEqual(applied.discount_paise, 179976)

    def test_discount_cannot_exceed_line_gross_price(self) -> None:
        # Lens priced at ₹500 (50,000 paise), offer provides ₹1,000 flat discount
        cheap_lens = CartLine(
            line_id="line_cheap",
            line_type="lens",
            variant_id=91000041,
            sku="LN-CHEAP",
            title="clear",
            unit_price_paise=50000,  # ₹500
            quantity=1,
            hsn="90015000",
            lens_type="progressive",
            index=1.6,
            coating="blue_cut",
        )
        cart = Cart(lines=[cheap_lens], customer=self.standard_customer)

        service_flat = OfferService(offers_data=[
            {
                "offer_id": "FREE_BLUECUT_PGX",
                "name": "Free bluecut upgrade",
                "active": True,
                "valid_from": "2026-01-01",
                "valid_to": "2026-12-31",
                "stackable": True,
                "priority": 25,
                "rule": {
                    "applies_to": "lens",
                    "condition": {"lens_type": "progressive", "index_min": 1.6, "coating": "blue_cut"},
                    "discount": {"type": "flat", "value_paise": 100000},  # ₹1,000
                },
            }
        ])

        res = service_flat.evaluate_cart(cart, transaction_date=self.current_date)
        # Discount must be capped at 50,000 paise (₹500.00), net price cannot be negative
        self.assertEqual(res.total_discount_paise, 50000)
        self.assertEqual(cheap_lens.net_price_paise, 0)

    def test_karthik_iyer_full_audit_allocations_and_gst(self) -> None:
        """Comprehensive audit test: Karthik Iyer cart with FR-CHEAP (₹5,000) and LN-0043 (₹9,499).

        Verifies:
        1. Compares Candidate 1 (FESTIVE10: ₹500), Candidate 2 (PGX_CAMPAIGN15: ₹1,424.85),
           and Candidate 3 (Stackable Bundle: ₹1,800.00).
        2. Candidate 3 wins under MAX_SAVINGS.
        3. Preserves exact line-item allocations:
           - Frame: BUNDLE300 (₹103.46) + PRIORITY500 (₹172.43) = ₹275.89 discount -> Net ₹4,724.11
           - Lens: FREE_BLUECUT_PGX (₹1,000.00) + BUNDLE300 (₹196.54) + PRIORITY500 (₹327.57) = ₹1,524.11 discount -> Net ₹7,974.89
        4. Independent GST calculations by HSN:
           - Frame HSN 90031900 (18%): Taxable ₹4,003.48, CGST ₹360.32, SGST ₹360.31
           - Lens HSN 90015000 (5%): Taxable ₹7,595.13, CGST ₹189.88, SGST ₹189.88
           - Order Net Total: ₹12,699.00 exactly reconciled.
        """
        from eluno_pos.services.gst_service import GstService

        cheap_frame = CartLine(
            line_id="line_frame_cheap",
            line_type="frame",
            variant_id=91000010,
            sku="FR-CHEAP",
            title="Crystal",
            unit_price_paise=500000,  # ₹5,000.00
            pair_ref="PAIR-01",
            hsn="90031900",
        )

        pgx_lens = CartLine(
            line_id="line_lens_pgx",
            line_type="lens",
            variant_id=91000083,
            sku="LN-0043",
            title="blue_cut",
            unit_price_paise=949900,  # ₹9,499.00
            pair_ref="PAIR-01",
            lens_type="progressive",
            index=1.6,
            coating="blue_cut",
            rx_id="RX5007",
            hsn="90015000",
        )

        cart = Cart(lines=[cheap_frame, pgx_lens], customer=self.priority_customer)
        tx_date = date(2026, 10, 20)

        # Evaluate offers under MAX_SAVINGS
        res = self.service.evaluate_cart(cart, transaction_date=tx_date, strategy=OfferConflictStrategy.MAX_SAVINGS)
        self.assertEqual(res.total_discount_paise, 180000)

        applied_ids = [a.offer_id for a in res.applied_offers]
        self.assertIn("FREE_BLUECUT_PGX", applied_ids)
        self.assertIn("PRIORITY500", applied_ids)
        self.assertIn("BUNDLE300", applied_ids)
        self.assertNotIn("FESTIVE10", applied_ids)
        self.assertNotIn("PGX_CAMPAIGN15", applied_ids)

        # Inspect Frame line item allocations
        frame_allocs = {d.offer_id: d.amount_paise for d in cheap_frame.discount_allocations}
        self.assertEqual(frame_allocs["BUNDLE300"], 10346)     # ₹103.46 (proportional pair share)
        self.assertEqual(frame_allocs["PRIORITY500"], 17243)  # ₹172.43 (proportional cart share)
        self.assertNotIn("FREE_BLUECUT_PGX", frame_allocs)    # FREE_BLUECUT never touches frame!
        self.assertEqual(cheap_frame.total_discount_paise, 27589)
        self.assertEqual(cheap_frame.net_price_paise, 472411)  # ₹4,724.11

        # Inspect Lens line item allocations
        lens_allocs = {d.offer_id: d.amount_paise for d in pgx_lens.discount_allocations}
        self.assertEqual(lens_allocs["FREE_BLUECUT_PGX"], 100000)  # ₹1,000.00 dedicated to lens
        self.assertEqual(lens_allocs["BUNDLE300"], 19654)          # ₹196.54 (pair share)
        self.assertEqual(lens_allocs["PRIORITY500"], 32757)        # ₹327.57 (cart share)
        self.assertEqual(pgx_lens.total_discount_paise, 152411)
        self.assertEqual(pgx_lens.net_price_paise, 797489)         # ₹7,974.89

        # Verify net total is exactly ₹12,699.00
        self.assertEqual(cart.net_total_paise, 1269900)

        # Calculate Independent GST
        gst = GstService()
        frame_tax = gst.calculate_line_tax(cheap_frame.hsn, cheap_frame.net_price_paise, tx_date)
        self.assertEqual(frame_tax.hsn, "90031900")
        self.assertEqual(frame_tax.taxable_value_paise, 400348)  # ₹4,003.48
        self.assertEqual(frame_tax.cgst_paise, 36032)           # ₹360.32
        self.assertEqual(frame_tax.sgst_paise, 36031)           # ₹360.31 (odd paisa to CGST)
        self.assertEqual(frame_tax.total_gst_paise, 72063)
        self.assertTrue(frame_tax.is_reconciled)

        lens_tax = gst.calculate_line_tax(pgx_lens.hsn, pgx_lens.net_price_paise, tx_date)
        self.assertEqual(lens_tax.hsn, "90015000")
        self.assertEqual(lens_tax.taxable_value_paise, 759513)  # ₹7,595.13
        self.assertEqual(lens_tax.cgst_paise, 18988)           # ₹189.88
        self.assertEqual(lens_tax.sgst_paise, 18988)           # ₹189.88
        self.assertEqual(lens_tax.total_gst_paise, 37976)
        self.assertTrue(lens_tax.is_reconciled)

        # Consolidated Order Tax
        order_tax = gst.calculate_order_tax([
            (cheap_frame.hsn, cheap_frame.net_price_paise),
            (pgx_lens.hsn, pgx_lens.net_price_paise),
        ], tx_date)
        self.assertEqual(order_tax.total_inclusive_paise, 1269900)
        self.assertEqual(order_tax.total_taxable_paise, 1159861)
        self.assertEqual(order_tax.total_cgst_paise, 55020)
        self.assertEqual(order_tax.total_sgst_paise, 55019)
        self.assertEqual(order_tax.total_gst_paise, 110039)
        self.assertTrue(order_tax.is_reconciled)

    def test_deterministic_repeated_evaluation_idempotency(self) -> None:
        """Repeatedly evaluating the same cart must produce identical results without cumulative drift."""
        cart = Cart(lines=[self.frame_line, self.lens_line], customer=self.priority_customer)

        res_1 = self.service.evaluate_cart(cart, transaction_date=self.current_date)
        disc_1 = cart.total_discount_paise
        net_1 = cart.net_total_paise

        res_2 = self.service.evaluate_cart(cart, transaction_date=self.current_date)
        disc_2 = cart.total_discount_paise
        net_2 = cart.net_total_paise

        res_3 = self.service.evaluate_cart(cart, transaction_date=self.current_date)
        disc_3 = cart.total_discount_paise
        net_3 = cart.net_total_paise

        self.assertEqual(disc_1, disc_2)
        self.assertEqual(disc_2, disc_3)
        self.assertEqual(net_1, net_2)
        self.assertEqual(net_2, net_3)
        self.assertEqual(len(self.frame_line.discount_allocations), 2)

    def test_pgx_campaign_15_wins_when_higher_than_stackables(self) -> None:
        """When progressive campaign 15% yields higher savings than stackable offers, it wins."""
        # Standard coating progressive lens (does not qualify for FREE_BLUECUT_PGX)
        pgx_standard = CartLine(
            line_id="line_pgx_std",
            line_type="lens",
            variant_id=91000081,
            sku="LN-0041",
            title="standard",
            unit_price_paise=949900,  # ₹9,499.00
            pair_ref="PAIR-01",
            lens_type="progressive",
            index=1.6,
            coating="standard",
        )
        cheap_frame = CartLine(
            line_id="line_frame_cheap",
            line_type="frame",
            variant_id=91000010,
            sku="FR-CHEAP",
            title="Crystal",
            unit_price_paise=200000,  # ₹2,000.00
            pair_ref="PAIR-01",
            hsn="90031900",
        )
        cart = Cart(lines=[cheap_frame, pgx_standard], customer=self.priority_customer)

        # Candidate comparison:
        # 1. FESTIVE10: 10% of ₹2,000 = ₹200.00 (20,000 paise)
        # 2. PGX_CAMPAIGN15: 15% of ₹9,499 = ₹1,424.85 (142,485 paise)
        # 3. Stackable bundle: BUNDLE300 (₹300) + PRIORITY500 (₹500) = ₹800.00 (80,000 paise)
        # Outcome: PGX_CAMPAIGN15 must win (₹1,424.85 > ₹800.00 > ₹200.00)!
        res = self.service.evaluate_cart(cart, transaction_date=self.current_date)
        self.assertEqual(len(res.applied_offers), 1)
        applied = res.applied_offers[0]
        self.assertEqual(applied.offer_id, "PGX_CAMPAIGN15")
        self.assertEqual(applied.discount_paise, 142485)

        # Discount must be allocated 100% to lens line, frame discount is 0
        self.assertEqual(pgx_standard.total_discount_paise, 142485)
        self.assertEqual(cheap_frame.total_discount_paise, 0)

    def test_free_bluecut_only_discounts_eligible_progressive_bluecut_lens(self) -> None:
        """FREE_BLUECUT_PGX must only discount progressive 1.6+ blue-cut lenses."""
        sv_bluecut = CartLine(
            line_id="line_sv_bluecut",
            line_type="lens",
            variant_id=91000043,
            sku="LN-SV-BLUE",
            title="blue_cut",
            unit_price_paise=199900,
            lens_type="single_vision",  # NOT progressive
            index=1.6,
            coating="blue_cut",
        )
        pgx_bluecut = CartLine(
            line_id="line_pgx_bluecut",
            line_type="lens",
            variant_id=91000083,
            sku="LN-PGX-BLUE",
            title="blue_cut",
            unit_price_paise=949900,
            lens_type="progressive",   # Progressive 1.6 blue-cut
            index=1.6,
            coating="blue_cut",
        )
        cart = Cart(lines=[sv_bluecut, pgx_bluecut], customer=self.standard_customer)

        service_bluecut = OfferService(offers_data=[
            {
                "offer_id": "FREE_BLUECUT_PGX",
                "name": "Free blue-cut upgrade",
                "active": True,
                "valid_from": "2026-09-01",
                "valid_to": "2026-12-31",
                "stackable": True,
                "priority": 25,
                "rule": {
                    "applies_to": "lens",
                    "condition": {"lens_type": "progressive", "index_min": 1.6, "coating": "blue_cut"},
                    "discount": {"type": "flat", "value_paise": 100000},
                },
            }
        ])

        res = service_bluecut.evaluate_cart(cart, transaction_date=self.current_date)
        self.assertEqual(res.total_discount_paise, 100000)
        self.assertEqual(pgx_bluecut.total_discount_paise, 100000)
        self.assertEqual(sv_bluecut.total_discount_paise, 0)  # Single vision untouched!

    def test_festive10_only_discounts_eligible_frames(self) -> None:
        """FESTIVE10 must strictly discount frames and never touch lenses or services."""
        cart = Cart(lines=[self.frame_line, self.lens_line], customer=self.standard_customer)
        service_festive = OfferService(offers_data=[
            {
                "offer_id": "FESTIVE10",
                "name": "Festive 10% on frames",
                "active": True,
                "valid_from": "2026-10-01",
                "valid_to": "2026-11-15",
                "stackable": False,
                "priority": 10,
                "rule": {
                    "applies_to": "frame",
                    "discount": {"type": "percent", "value_bps": 1000},
                    "max_discount_paise": 200000,
                },
            }
        ])

        res = service_festive.evaluate_cart(cart, transaction_date=self.current_date)
        self.assertEqual(len(res.applied_offers), 1)
        self.assertEqual(self.frame_line.total_discount_paise, 129980)  # 10% of ₹12,998.00
        self.assertEqual(self.lens_line.total_discount_paise, 0)        # Lens discount is strictly 0!


if __name__ == "__main__":
    unittest.main()
