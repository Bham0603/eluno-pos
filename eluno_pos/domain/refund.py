"""Refund domain models and lifecycle states for Eluno POS."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RefundStatus(str, Enum):
    """Lifecycle states for a store refund transaction.
    
    Invariants:
    - INITIATED: Intent recorded in SQLite; no external funds moved yet.
    - TERMINAL_REFUNDED: Terminal charge refunded; Shopify record pending.
    - COMPLETED: Terminal refund confirmed, Shopify refund recorded, frames restocked.
    - FAILED: Definitively rejected pre-refund (e.g. invalid amount or expired).
    - PENDING_RECONCILIATION: Terminal or Shopify call had an ambiguous outcome.
      No second terminal refund may be attempted while in this state!
    """
    INITIATED = "INITIATED"
    TERMINAL_REFUNDED = "TERMINAL_REFUNDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"


@dataclass
class RefundLine:
    """Individual line item being refunded."""
    line_item_id: int
    variant_id: int
    sku: str
    line_type: str  # 'frame', 'lens', 'service'
    quantity: int
    unit_price_paise: int
    net_price_paise: int
    tax_paise: int
    restock_type: str  # 'return' for frames, 'no_restock' for lenses
    location_id: Optional[int] = None
    inventory_item_id: Optional[int] = None


@dataclass
class RefundRecord:
    """Durable audit record of a store refund transaction."""
    refund_id: str
    credit_note_no: str
    invoice_no: str
    checkout_ref: str
    charge_id: str
    shopify_order_id: int
    amount_paise: int
    status: RefundStatus
    reason_code: str
    staff_id: Optional[str] = None
    terminal_refund_id: Optional[str] = None
    shopify_refund_id: Optional[int] = None
    baseline_refunded_paise: int = 0
    error_message: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class RefundResult:
    """Final outcome of an orchestrated refund workflow."""
    success: bool
    refund_id: str
    credit_note_no: str
    invoice_no: str
    status: RefundStatus
    amount_refunded_paise: int
    terminal_refund_id: Optional[str] = None
    shopify_refund_id: Optional[int] = None
    frames_restocked_count: int = 0
    error_message: Optional[str] = None

    @property
    def is_pending_reconciliation(self) -> bool:
        """Whether the refund requires manual or background reconciliation."""
        return self.status == RefundStatus.PENDING_RECONCILIATION
