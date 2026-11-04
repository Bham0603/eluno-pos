"""Offer domain entities for promotional rule matching and discount computation."""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Offer:
    """Configured promotional offer from offers.json."""
    offer_id: str
    name: str
    active: bool
    valid_from: str  # YYYY-MM-DD
    valid_to: str  # YYYY-MM-DD (inclusive)
    stackable: bool
    priority: int
    rule: Dict[str, Any]

    def is_effective_on(self, target_date: date) -> bool:
        """Check whether offer is active and valid on the target date (inclusive)."""
        if not self.active:
            return False
        target_iso = target_date.isoformat()
        return self.valid_from <= target_iso <= self.valid_to

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Offer":
        """Instantiate Offer from JSON dictionary."""
        return cls(
            offer_id=str(data["offer_id"]),
            name=str(data.get("name", data["offer_id"])),
            active=bool(data.get("active", True)),
            valid_from=str(data["valid_from"]),
            valid_to=str(data["valid_to"]),
            stackable=bool(data.get("stackable", False)),
            priority=int(data.get("priority", 100)),
            rule=dict(data.get("rule", {})),
        )


@dataclass(frozen=True)
class AppliedOffer:
    """An offer successfully qualified and applied to the cart."""
    offer_id: str
    name: str
    discount_paise: int
    line_allocations: Dict[str, int]  # line_id -> discount_paise
    description: str


@dataclass
class OfferEvaluationResult:
    """Comprehensive outcome of cart offer evaluation."""
    applied_offers: List[AppliedOffer] = field(default_factory=list)
    total_discount_paise: int = 0
    ineligible_offers: List[Dict[str, str]] = field(default_factory=list)

    @property
    def has_discounts(self) -> bool:
        """Check whether any discounts were applied."""
        return self.total_discount_paise > 0

