"""Payment service orchestrating terminal transactions and durable audit persistence."""

from typing import Any, Dict, Optional

from eluno_pos.adapters.exceptions import (
    AmbiguousOperationError,
    ElunoPosError,
    ValidationError,
)
from eluno_pos.adapters.terminal_client import TerminalClient
from eluno_pos.domain.invoice import Invoice
from eluno_pos.domain.payment import PaymentRecord, PaymentStatus
from eluno_pos.repositories.invoice_repository import InvoiceRepository


class PaymentService:
    """Service handling payment terminal charges with strict idempotency and audit persistence."""

    def __init__(
        self,
        terminal_client: TerminalClient,
        invoice_repository: InvoiceRepository,
    ) -> None:
        self.terminal_client = terminal_client
        self.repo = invoice_repository

    def process_payment(
        self,
        invoice: Invoice,
        amount_paise: int,
        method: str = "upi",
        order_ref: Optional[str] = None,
    ) -> PaymentRecord:
        """Execute a payment charge against the terminal.

        LIFECYCLE & RETRY RULES:
        1. Ensures the amount matches the net payable or specified amount (> 0).
        2. Assigns a stable order_ref derived from checkout_ref / invoice_no.
        3. Persists the attempt as INITIATED in SQLite BEFORE making the network call.
        4. Invokes TerminalClient.create_charge:
           - The documented synthetic pre-capture 503 is retried once with the same order_ref.
           - Network timeouts or socket drops raise AmbiguousOperationError (never auto-retried).
        5. On capture:
           - Persists charge_id and updates attempt to CAPTURED.
           - Updates invoice.terminal_charge_id.
        6. On ambiguous failure:
           - Updates attempt to UNKNOWN.
           - Re-raises AmbiguousOperationError; checkout MUST NOT attempt a second charge!
        7. On definitive failure:
           - Updates attempt to FAILED.
           - Re-raises exception.
        """
        if amount_paise <= 0:
            raise ValidationError(
                message=f"Payment amount must be greater than 0 paise, got {amount_paise}"
            )

        if not order_ref:
            order_ref = f"pay_{invoice.checkout_ref}"

        # Check existing attempt to prevent accidental re-runs
        existing = self.repo.get_payment_attempt(order_ref)
        if existing:
            if existing.status == PaymentStatus.CAPTURED:
                # Already captured! Return existing record safely
                return existing
            if existing.status == PaymentStatus.UNKNOWN:
                raise AmbiguousOperationError(
                    message=(
                        f"Payment attempt for order_ref '{order_ref}' is in an UNKNOWN state. "
                        "Do not retry automatically; investigate terminal transaction before re-attempting."
                    )
                )

        # 1. Persist intent before network invocation
        attempt = PaymentRecord(
            checkout_ref=invoice.checkout_ref,
            invoice_no=invoice.invoice_no,
            order_ref=order_ref,
            amount_paise=amount_paise,
            method=method,
            status=PaymentStatus.INITIATED,
        )
        self.repo.record_payment_attempt(attempt)

        # 2. Initiate charge
        try:
            charge = self.terminal_client.create_charge(
                amount_paise=amount_paise,
                method=method,
                order_ref=order_ref,
            )
            charge_id = charge.get("charge_id")
            if not charge_id or charge.get("status") != "captured":
                raise ElunoPosError(f"Unexpected terminal response: {charge}")

            # 3. Persist successful capture
            self.repo.update_payment_attempt(
                order_ref=order_ref,
                status=PaymentStatus.CAPTURED,
                charge_id=charge_id,
            )
            self.repo.update_invoice_payment(
                invoice_no=invoice.invoice_no,
                terminal_charge_id=charge_id,
            )

            updated_record = self.repo.get_payment_attempt(order_ref)
            assert updated_record is not None
            return updated_record

        except AmbiguousOperationError as err:
            # 4. Ambiguous outcome: mark UNKNOWN, do NOT retry
            self.repo.update_payment_attempt(
                order_ref=order_ref,
                status=PaymentStatus.UNKNOWN,
                error_message=str(err),
            )
            raise

        except Exception as err:
            # 5. Definite error: mark FAILED
            self.repo.update_payment_attempt(
                order_ref=order_ref,
                status=PaymentStatus.FAILED,
                error_message=str(err),
            )
            raise
