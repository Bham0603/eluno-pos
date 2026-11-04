"""Payment attempt domain entities and status enums for Eluno POS."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PaymentStatus(str, Enum):
    """Status of a payment transaction attempt."""
    INITIATED = "INITIATED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"  # Ambiguous outcome (e.g. socket timeout, network drop)


@dataclass
class PaymentRecord:
    """Audit record of a physical/UPI terminal payment attempt."""
    checkout_ref: str
    invoice_no: str
    order_ref: str
    amount_paise: int
    method: str
    status: PaymentStatus
    charge_id: Optional[str] = None
    error_message: Optional[str] = None
    id: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_successful(self) -> bool:
        """Whether payment was successfully captured."""
        return self.status == PaymentStatus.CAPTURED and self.charge_id is not None

