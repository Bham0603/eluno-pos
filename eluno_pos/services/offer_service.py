"""Modular Offer Evaluation Engine for Eluno POS."""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from eluno_pos.domain.cart import Cart, CartLine, DiscountAllocation
from eluno_pos.domain.customer import Customer
from eluno_pos.domain.money import allocate_discount_proportionally, round_half_up
from eluno_pos.domain.offer import AppliedOffer, Offer, OfferEvaluationResult


class OfferConflictStrategy(str, Enum):
    """Strategy for resolving conflicts between non-stackable and stackable offers.

    MAX_SAVINGS (Recommended):
        Evaluates each qualifying non-stackable offer and the complete bundle of
        compatible stackable offers, selecting whichever configuration delivers
        the greatest financial savings to the customer. Priority serves as tie-breaker.

    STRICT_PRIORITY:
        Evaluates qualifying offers strictly by priority order (lowest number first).
        If the top-ranked offer is non-stackable, it is applied exclusively.
        If stackable, all subsequent compatible stackable offers are accumulated.
    """
    MAX_SAVINGS = "max_savings"
    STRICT_PRIORITY = "strict_priority"


class OfferService:
    """Service to load, evaluate, and allocate promotional offers and discounts."""

    DEFAULT_OFFERS_FILE = Path("starter-kit/data/offers.json")

    def __init__(
        self,
        offers_data: Optional[List[Dict[str, Any]]] = None,
        file_path: Optional[Path] = None,
        strategy: OfferConflictStrategy = OfferConflictStrategy.MAX_SAVINGS,
    ) -> None:
        self.offers: List[Offer] = []
        self.strategy = strategy
        if offers_data is not None:
            self._load_from_data(offers_data)
        else:
            path = file_path or self.DEFAULT_OFFERS_FILE
            self._load_from_file(path)

    def _load_from_data(self, data: List[Dict[str, Any]]) -> None:
        self.offers = [Offer.from_dict(d) for d in data]

    def _load_from_file(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"Offers configuration file not found at {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._load_from_data(data)

    def evaluate_cart(
        self,
        cart: Cart,
        transaction_date: Optional[date] = None,
        strategy: Optional[OfferConflictStrategy] = None,
    ) -> OfferEvaluationResult:
        """Evaluate all eligible offers against the cart and apply optimal discounts.

        Mutates cart lines by populating line.discount_allocations.
        """
        tx_date = transaction_date or date.today()
        eval_strategy = strategy or self.strategy

        # Reset any previous discount allocations
        cart.clear_discounts()

        if not cart.lines or cart.gross_total_paise <= 0:
            return OfferEvaluationResult()

        # Step 1: Filter active & date-valid offers
        effective_offers = [o for o in self.offers if o.is_effective_on(tx_date)]
        ineligible_reasons: List[Dict[str, str]] = []

        # Step 2: Test eligibility and compute potential discounts per offer
        qualifying_offers: List[Tuple[Offer, AppliedOffer]] = []

        for offer in effective_offers:
            is_eligible, reason, applied = self._evaluate_single_offer(offer, cart)
            if is_eligible and applied and applied.discount_paise > 0:
                qualifying_offers.append((offer, applied))
            else:
                ineligible_reasons.append({"offer_id": offer.offer_id, "reason": reason})

        if not qualifying_offers:
            return OfferEvaluationResult(ineligible_offers=ineligible_reasons)

        # Step 3: Resolve conflicts based on stackability and selected strategy
        selected_applied: List[AppliedOffer] = []

        if eval_strategy == OfferConflictStrategy.STRICT_PRIORITY:
            selected_applied = self._resolve_by_strict_priority(qualifying_offers, cart)
        else:
            selected_applied = self._resolve_by_max_savings(qualifying_offers, cart)

        # Step 4: Apply winning allocations to cart lines
        self._apply_allocations_to_cart(selected_applied, cart)

        total_discount = sum(a.discount_paise for a in selected_applied)
        return OfferEvaluationResult(
            applied_offers=selected_applied,
            total_discount_paise=total_discount,
            ineligible_offers=ineligible_reasons,
        )

    def _evaluate_single_offer(
        self,
        offer: Offer,
        cart: Cart,
    ) -> Tuple[bool, str, Optional[AppliedOffer]]:
        """Test conditions for an offer and compute simulated line-item allocations."""
        rule = offer.rule
        applies_to = rule.get("applies_to", "")
        condition = rule.get("condition", {})
        discount_cfg = rule.get("discount", {})

        # Check customer tag conditions
        required_tag = condition.get("customer_tag")
        if required_tag:
            if not cart.customer or not cart.customer.has_tag(required_tag):
                return False, f"Customer does not possess required tag '{required_tag}'", None

        # Check cart pair count condition
        min_pairs = condition.get("min_pairs_in_cart")
        if min_pairs is not None:
            if cart.complete_pairs_count < min_pairs:
                return False, f"Cart has {cart.complete_pairs_count} complete pairs, requires at least {min_pairs}", None

        # Check manager override requirements
        requires = rule.get("requires", {})
        if requires:
            if not cart.manager_override:
                return False, "Manager override authorization not supplied", None
            if requires.get("role") and cart.manager_override.role != requires["role"]:
                return False, f"Requires role '{requires['role']}', got '{cart.manager_override.role}'", None
            if requires.get("reason_code") and not cart.manager_override.reason_code:
                return False, "Manager override requires an audit reason code", None

        # Evaluate target application
        if applies_to == "frame":
            return self._evaluate_frame_offer(offer, rule, cart)
        elif applies_to == "lens":
            return self._evaluate_lens_offer(offer, rule, cart)
        elif applies_to == "pair":
            return self._evaluate_pair_offer(offer, rule, cart)
        elif applies_to == "cart":
            return self._evaluate_cart_offer(offer, rule, cart)
        else:
            return False, f"Unsupported applies_to target '{applies_to}'", None

    def _evaluate_frame_offer(
        self,
        offer: Offer,
        rule: Dict[str, Any],
        cart: Cart,
    ) -> Tuple[bool, str, Optional[AppliedOffer]]:
        frames = cart.frame_lines
        if not frames:
            return False, "No frame line items in cart", None

        discount_cfg = rule.get("discount", {})
        disc_type = discount_cfg.get("type")
        max_cap = rule.get("max_discount_paise")

        allocations: Dict[str, int] = {}
        total_discount = 0

        if disc_type == "percent":
            bps = discount_cfg.get("value_bps", 0)
            for f in frames:
                amt = round_half_up((Decimal(f.gross_price_paise) * Decimal(bps)) / Decimal(10000))
                amt = min(amt, f.gross_price_paise)
                allocations[f.line_id] = amt
                total_discount += amt

            # Cap if max_discount_paise specified
            if max_cap is not None and total_discount > max_cap:
                # Proportionally scale down allocations to exact cap
                frame_ids = list(allocations.keys())
                frame_prices = [allocations[fid] for fid in frame_ids]
                scaled = allocate_discount_proportionally(max_cap, frame_prices)
                allocations = {frame_ids[i]: scaled[i] for i in range(len(frame_ids))}
                total_discount = max_cap

        elif disc_type == "flat":
            flat_val = discount_cfg.get("value_paise", 0)
            # Allocate flat discount across frames
            frame_ids = [f.line_id for f in frames]
            frame_prices = [f.gross_price_paise for f in frames]
            scaled = allocate_discount_proportionally(flat_val, frame_prices)
            allocations = {frame_ids[i]: scaled[i] for i in range(len(frame_ids))}
            total_discount = sum(allocations.values())

        if total_discount <= 0:
            return False, "Calculated discount is zero", None

        return True, "Eligible", AppliedOffer(
            offer_id=offer.offer_id,
            name=offer.name,
            discount_paise=total_discount,
            line_allocations=allocations,
            description=f"{offer.name}: ₹{total_discount / 100:.2f} off frames",
        )

    def _evaluate_lens_offer(
        self,
        offer: Offer,
        rule: Dict[str, Any],
        cart: Cart,
    ) -> Tuple[bool, str, Optional[AppliedOffer]]:
        lenses = cart.lens_lines
        if not lenses:
            return False, "No lens line items in cart", None

        condition = rule.get("condition", {})
        discount_cfg = rule.get("discount", {})

        # Filter lenses matching specific conditions
        matching_lenses: List[CartLine] = []
        for lens in lenses:
            if condition.get("lens_type") and lens.lens_type != condition["lens_type"]:
                continue
            if condition.get("index_min") and (lens.index is None or lens.index < condition["index_min"]):
                continue
            if condition.get("coating") and lens.coating != condition["coating"]:
                continue
            matching_lenses.append(lens)

        if not matching_lenses:
            return False, "No lenses meet technical condition criteria", None

        allocations: Dict[str, int] = {}
        total_discount = 0
        disc_type = discount_cfg.get("type")
        disc_on = discount_cfg.get("on")

        if disc_on == "lowest_priced_lens_line":
            # Target the single lowest-priced lens among matching lenses
            target_lens = min(matching_lenses, key=lambda l: l.gross_price_paise)
            bps = discount_cfg.get("value_bps", 0)
            amt = round_half_up((Decimal(target_lens.gross_price_paise) * Decimal(bps)) / Decimal(10000))
            amt = min(amt, target_lens.gross_price_paise)
            allocations[target_lens.line_id] = amt
            total_discount = amt

        elif disc_type == "percent":
            bps = discount_cfg.get("value_bps", 0)
            for l in matching_lenses:
                amt = round_half_up((Decimal(l.gross_price_paise) * Decimal(bps)) / Decimal(10000))
                amt = min(amt, l.gross_price_paise)
                allocations[l.line_id] = amt
                total_discount += amt

        elif disc_type == "flat":
            val_paise = discount_cfg.get("value_paise", 0)
            for l in matching_lenses:
                amt = min(val_paise, l.gross_price_paise)
                allocations[l.line_id] = amt
                total_discount += amt

        if total_discount <= 0:
            return False, "Calculated lens discount is zero", None

        return True, "Eligible", AppliedOffer(
            offer_id=offer.offer_id,
            name=offer.name,
            discount_paise=total_discount,
            line_allocations=allocations,
            description=f"{offer.name}: ₹{total_discount / 100:.2f} off lenses",
        )

    def _evaluate_pair_offer(
        self,
        offer: Offer,
        rule: Dict[str, Any],
        cart: Cart,
    ) -> Tuple[bool, str, Optional[AppliedOffer]]:
        condition = rule.get("condition", {})
        if condition.get("has_frame_and_lens") and cart.complete_pairs_count < 1:
            return False, "Cart does not contain any complete frame + lens pairs", None

        discount_cfg = rule.get("discount", {})
        val_paise = discount_cfg.get("value_paise", 0)
        allocations: Dict[str, int] = {}
        total_discount = 0

        # Apply discount per complete pair
        for pair_ref in cart.pair_refs:
            pair_lines = [l for l in cart.lines if l.pair_ref == pair_ref]
            has_frame = any(l.line_type == "frame" for l in pair_lines)
            has_lens = any(l.line_type == "lens" for l in pair_lines)

            if has_frame and has_lens:
                pair_line_ids = [l.line_id for l in pair_lines]
                pair_prices = [l.gross_price_paise for l in pair_lines]
                pair_allocs = allocate_discount_proportionally(val_paise, pair_prices)

                for i, lid in enumerate(pair_line_ids):
                    allocations[lid] = allocations.get(lid, 0) + pair_allocs[i]
                total_discount += sum(pair_allocs)

        if total_discount <= 0:
            return False, "Calculated pair discount is zero", None

        return True, "Eligible", AppliedOffer(
            offer_id=offer.offer_id,
            name=offer.name,
            discount_paise=total_discount,
            line_allocations=allocations,
            description=f"{offer.name}: ₹{total_discount / 100:.2f} off complete pair(s)",
        )

    def _evaluate_cart_offer(
        self,
        offer: Offer,
        rule: Dict[str, Any],
        cart: Cart,
    ) -> Tuple[bool, str, Optional[AppliedOffer]]:
        discount_cfg = rule.get("discount", {})
        disc_type = discount_cfg.get("type")
        total_discount = 0

        if disc_type == "flat":
            val_paise = discount_cfg.get("value_paise", 0)
            total_discount = min(val_paise, cart.gross_total_paise)
        elif disc_type == "percent":
            if cart.manager_override:
                max_bps = discount_cfg.get("value_bps_max", 1000)
                bps = min(cart.manager_override.discount_percent_bps, max_bps)
                total_discount = round_half_up((Decimal(cart.gross_total_paise) * Decimal(bps)) / Decimal(10000))
                total_discount = min(total_discount, cart.gross_total_paise)
            else:
                bps = discount_cfg.get("value_bps", 0)
                total_discount = round_half_up((Decimal(cart.gross_total_paise) * Decimal(bps)) / Decimal(10000))
                total_discount = min(total_discount, cart.gross_total_paise)

        if total_discount <= 0:
            return False, "Calculated cart discount is zero", None

        line_ids = [l.line_id for l in cart.lines]
        line_prices = [l.gross_price_paise for l in cart.lines]
        allocs = allocate_discount_proportionally(total_discount, line_prices)
        allocations = {line_ids[i]: allocs[i] for i in range(len(line_ids))}

        return True, "Eligible", AppliedOffer(
            offer_id=offer.offer_id,
            name=offer.name,
            discount_paise=total_discount,
            line_allocations=allocations,
            description=f"{offer.name}: ₹{total_discount / 100:.2f} off cart",
        )

    def _resolve_by_max_savings(
        self,
        qualifying: List[Tuple[Offer, AppliedOffer]],
        cart: Cart,
    ) -> List[AppliedOffer]:
        """Candidate comparison: evaluates each non-stackable offer against the combined stackables."""
        non_stackable_candidates: List[List[AppliedOffer]] = []
        stackable_candidate: List[AppliedOffer] = []

        for offer, applied in qualifying:
            if offer.stackable:
                stackable_candidate.append(applied)
            else:
                non_stackable_candidates.append([applied])

        all_candidate_sets: List[List[AppliedOffer]] = []
        if stackable_candidate:
            all_candidate_sets.append(stackable_candidate)
        all_candidate_sets.extend(non_stackable_candidates)

        # Score each candidate set by total feasible discount (respecting line price caps)
        scored_sets: List[Tuple[int, int, List[AppliedOffer]]] = []
        for cand_set in all_candidate_sets:
            # Simulate combined allocations to avoid exceeding line prices
            combined_allocations, effective_total = self._simulate_combined_allocations(cand_set, cart)
            # Priority score: best (lowest) priority among included offers
            best_priority = min(
                next(o.priority for o, a in qualifying if o.offer_id == ap.offer_id)
                for ap in cand_set
            )
            # Negative total discount for max-sort, positive priority for lowest-priority-first tie breaker
            scored_sets.append((effective_total, best_priority, cand_set))

        # Sort: Primary = effective_total descending, Secondary = best_priority ascending
        scored_sets.sort(key=lambda s: (-s[0], s[1]))

        winning_set = scored_sets[0][2]
        return winning_set

    def _resolve_by_strict_priority(
        self,
        qualifying: List[Tuple[Offer, AppliedOffer]],
        cart: Cart,
    ) -> List[AppliedOffer]:
        """Evaluates offers in strict ascending priority order (10, 15, 20...)."""
        sorted_qualifying = sorted(qualifying, key=lambda t: t[0].priority)

        selected: List[AppliedOffer] = []
        is_stackable_mode: Optional[bool] = None

        for offer, applied in sorted_qualifying:
            if is_stackable_mode is None:
                # First offer sets the mode
                selected.append(applied)
                is_stackable_mode = offer.stackable
                if not is_stackable_mode:
                    # Non-stackable offer takes precedence alone
                    break
            else:
                if is_stackable_mode and offer.stackable:
                    selected.append(applied)

        return selected

    def _simulate_combined_allocations(
        self,
        applied_list: List[AppliedOffer],
        cart: Cart,
    ) -> Tuple[Dict[str, int], int]:
        """Simulate stacking multiple offers and cap allocations per line at line gross price."""
        line_remaining = {l.line_id: l.gross_price_paise for l in cart.lines}
        total_effective = 0
        combined: Dict[str, int] = {}

        for applied in applied_list:
            for lid, amt in applied.line_allocations.items():
                if lid not in line_remaining:
                    continue
                room = line_remaining[lid]
                actual_amt = min(amt, room)
                combined[lid] = combined.get(lid, 0) + actual_amt
                line_remaining[lid] -= actual_amt
                total_effective += actual_amt

        return combined, total_effective

    def _apply_allocations_to_cart(
        self,
        applied_list: List[AppliedOffer],
        cart: Cart,
    ) -> None:
        """Apply final chosen allocations to CartLine objects, preventing negative line prices."""
        line_map = {l.line_id: l for l in cart.lines}
        line_remaining = {l.line_id: l.gross_price_paise for l in cart.lines}

        for applied in applied_list:
            for lid, amt in applied.line_allocations.items():
                if lid not in line_map:
                    continue
                line = line_map[lid]
                room = line_remaining[lid]
                actual_amt = min(amt, room)
                if actual_amt > 0:
                    line.discount_allocations.append(
                        DiscountAllocation(
                            offer_id=applied.offer_id,
                            offer_name=applied.name,
                            amount_paise=actual_amt,
                        )
                    )
                    line_remaining[lid] -= actual_amt

