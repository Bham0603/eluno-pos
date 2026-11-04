"""Invoice domain models and lifecycle states for Eluno POS."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class InvoiceStatus(str, Enum):
    """Lifecycle states for a store invoice.
    
    Invariants:
    - POSTED: Successfully created and confirmed in Shopify (or conclusively reconciled).
    - PENDING_RECONCILIATION: Payment captured or in-flight, but Shopify order creation
      has not yet been conclusively confirmed.
    - CANCELLED: Pre-payment checkout abandoned or definitively failed.
      NOTE: A captured payment CAN NEVER transition to CANCELLED.
    """
    POSTED = "POSTED"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
    CANCELLED = "CANCELLED"


@dataclass
class InvoiceLine:
    """Snapshot of a line item frozen at invoice generation."""
    line_id: str
    line_type: str  # 'frame', 'lens', 'service'
    variant_id: int
    sku: str
    title: str
    quantity: int
    unit_price_paise: int
    gross_paise: int
    discount_paise: int
    net_paise: int
    hsn: str
    pair_ref: Optional[str] = None
    rx_id: Optional[str] = None
    lens_config: Optional[str] = None
    properties: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class Invoice:
    """Store tax invoice representation."""
    invoice_no: str
    store_code: str
    financial_year: str
    seq: int
    status: InvoiceStatus
    customer_id: Optional[int]
    gross_paise: int
    discount_paise: int
    net_paise: int
    taxable_paise: int
    tax_paise: int
    checkout_ref: str
    terminal_charge_id: Optional[str] = None
    shopify_order_id: Optional[int] = None
    lines: List[InvoiceLine] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_paid(self) -> bool:
        """Whether a payment charge has been captured for this invoice."""
        return self.terminal_charge_id is not None

    def can_transition_to(self, new_status: InvoiceStatus) -> bool:
        """Validate allowable state transitions according to lifecycle invariants."""
        if self.status == InvoiceStatus.POSTED:
            # POSTED is a terminal success state for sales invoices
            return False

        if self.status == InvoiceStatus.CANCELLED:
            # CANCELLED invoice numbers are retired and can never be revived
            return False

        if self.status == InvoiceStatus.PENDING_RECONCILIATION:
            if new_status == InvoiceStatus.POSTED:
                return True
            if new_status == InvoiceStatus.CANCELLED:
                # Critical rule: If payment was captured, it can NEVER be cancelled!
                return not self.is_paid

        return True

