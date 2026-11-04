"""Tax and GST domain representations for Eluno POS."""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class TaxRateEntry:
    """Historical or current GST tax rate rule for a specific HSN."""
    hsn: str
    description: str
    rate_bps: int  # e.g. 1800 for 18%, 500 for 5%
    effective_from: str  # ISO date string (YYYY-MM-DD)
    effective_to: Optional[str] = None  # Inclusive ISO date string or None if ongoing

    @property
    def rate_percent(self) -> float:
        """Rate as standard percentage (e.g. 18.0)."""
        return self.rate_bps / 100.0

    @property
    def cgst_rate_bps(self) -> int:
        """Central GST rate in basis points (half of total rate)."""
        return self.rate_bps // 2

    @property
    def sgst_rate_bps(self) -> int:
        """State GST rate in basis points (half of total rate)."""
        return self.rate_bps // 2

    def is_effective_on(self, target_date: date) -> bool:
        """Check whether this tax rate was active on the target date (inclusive)."""
        target_iso = target_date.isoformat()
        if target_iso < self.effective_from:
            return False
        if self.effective_to and target_iso > self.effective_to:
            return False
        return True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaxRateEntry":
        """Instantiate TaxRateEntry from JSON entry."""
        return cls(
            hsn=str(data["hsn"]),
            description=data.get("description", ""),
            rate_bps=int(data["rate_bps"]),
            effective_from=data["effective_from"],
            effective_to=data.get("effective_to"),
        )


@dataclass(frozen=True)
class TaxLine:
    """Shopify-compatible tax line representation."""
    title: str  # "CGST" or "SGST"
    price_paise: int
    rate_bps: int

    @property
    def rate(self) -> float:
        """Rate as decimal (e.g. 0.09 for 9%)."""
        return self.rate_bps / 10000.0


@dataclass(frozen=True)
class LineTaxCalculation:
    """Detailed GST calculation for a single invoice line item."""
    hsn: str
    description: str
    rate_bps: int
    inclusive_price_paise: int
    taxable_value_paise: int
    cgst_paise: int
    sgst_paise: int
    total_gst_paise: int

    @property
    def is_reconciled(self) -> bool:
        """Verify that taxable value + CGST + SGST exactly equals the inclusive total."""
        return (self.taxable_value_paise + self.cgst_paise + self.sgst_paise) == self.inclusive_price_paise

    def to_shopify_tax_lines(self) -> List[TaxLine]:
        """Convert calculation into Shopify tax line items."""
        half_rate = self.rate_bps // 2
        return [
            TaxLine(title="CGST", price_paise=self.cgst_paise, rate_bps=half_rate),
            TaxLine(title="SGST", price_paise=self.sgst_paise, rate_bps=half_rate),
        ]


@dataclass
class OrderTaxSummary:
    """Consolidated GST summary across an entire order."""
    total_inclusive_paise: int
    total_taxable_paise: int
    total_cgst_paise: int
    total_sgst_paise: int
    total_gst_paise: int
    line_taxes: List[LineTaxCalculation] = field(default_factory=list)

    @property
    def is_reconciled(self) -> bool:
        """Verify order-level reconciliation."""
        return (self.total_taxable_paise + self.total_cgst_paise + self.total_sgst_paise) == self.total_inclusive_paise

