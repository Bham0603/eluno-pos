"""Cart and LineItem domain entities for offer evaluation and checkout."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from eluno_pos.domain.customer import Customer
from eluno_pos.domain.money import parse_to_paise


@dataclass
class DiscountAllocation:
    """Discount amount allocated to a specific line item."""
    offer_id: str
    offer_name: str
    amount_paise: int


@dataclass
class CartLine:
    """Individual line item in the shopping cart."""
    line_id: str
    line_type: str  # 'frame', 'lens', 'service', etc.
    variant_id: int
    sku: str
    title: str
    unit_price_paise: int
    quantity: int = 1
    hsn: str = "90031900"
    pair_ref: Optional[str] = None
    lens_type: Optional[str] = None  # 'single_vision', 'progressive'
    index: Optional[float] = None  # 1.5, 1.6, 1.67, 1.74
    coating: Optional[str] = None  # 'standard', 'anti_reflective', 'blue_cut', 'photochromic'
    rx_id: Optional[str] = None
    for_customer_id: Optional[int] = None
    discount_allocations: List[DiscountAllocation] = field(default_factory=list)

    @property
    def gross_price_paise(self) -> int:
        """Total gross price before discounts."""
        return self.unit_price_paise * self.quantity

    @property
    def total_discount_paise(self) -> int:
        """Total discount allocated to this line."""
        return sum(d.amount_paise for d in self.discount_allocations)

    @property
    def net_price_paise(self) -> int:
        """Final price payable for this line after discounts (cannot be negative)."""
        return max(0, self.gross_price_paise - self.total_discount_paise)


@dataclass
class ManagerOverride:
    """Staff manager discretionary discount authorization."""
    staff_id: str
    role: str  # e.g. 'store_manager'
    reason_code: str  # e.g. 'PRICE_MATCH', 'CUSTOMER_RETENTION'
    discount_percent_bps: int  # up to 1000 bps (10%)


@dataclass
class Cart:
    """Shopping cart containing line items and customer context."""
    lines: List[CartLine] = field(default_factory=list)
    customer: Optional[Customer] = None
    manager_override: Optional[ManagerOverride] = None

    @property
    def gross_total_paise(self) -> int:
        """Gross total before discounts."""
        return sum(line.gross_price_paise for line in self.lines)

    @property
    def total_discount_paise(self) -> int:
        """Sum of all line discounts."""
        return sum(line.total_discount_paise for line in self.lines)

    @property
    def net_total_paise(self) -> int:
        """Total payable amount after all discounts."""
        return sum(line.net_price_paise for line in self.lines)

    @property
    def frame_lines(self) -> List[CartLine]:
        """All frame line items in cart."""
        return [line for line in self.lines if line.line_type == "frame"]

    @property
    def lens_lines(self) -> List[CartLine]:
        """All lens line items in cart."""
        return [line for line in self.lines if line.line_type == "lens"]

    @property
    def pair_refs(self) -> List[str]:
        """Distinct pair references present in cart."""
        return sorted(list({line.pair_ref for line in self.lines if line.pair_ref is not None}))

    @property
    def complete_pairs_count(self) -> int:
        """Number of complete pairs in the cart (lines sharing a pair_ref that include both a frame and a lens)."""
        pairs: Dict[str, Dict[str, bool]] = {}
        for line in self.lines:
            if not line.pair_ref:
                continue
            if line.pair_ref not in pairs:
                pairs[line.pair_ref] = {"has_frame": False, "has_lens": False}
            if line.line_type == "frame":
                pairs[line.pair_ref]["has_frame"] = True
            elif line.line_type == "lens":
                pairs[line.pair_ref]["has_lens"] = True

        return sum(1 for p in pairs.values() if p["has_frame"] and p["has_lens"])

    def clear_discounts(self) -> None:
        """Reset all applied discounts on cart lines."""
        for line in self.lines:
            line.discount_allocations.clear()

