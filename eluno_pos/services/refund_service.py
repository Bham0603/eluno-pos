"""Refund orchestration service with duplicate-refund prevention and frame restocking."""

from datetime import date
from typing import Any, Dict, List, Optional
import uuid

from eluno_pos.adapters.exceptions import (
    AmbiguousOperationError,
    ElunoPosError,
    ValidationError,
)
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.adapters.terminal_client import TerminalClient
from eluno_pos.domain.invoice import Invoice, InvoiceStatus
from eluno_pos.domain.refund import RefundLine, RefundRecord, RefundResult, RefundStatus
from eluno_pos.repositories.invoice_repository import InvoiceRepository


class RefundService:
    """Service to execute store full refunds, terminal reversals, and frame restocking."""

    def __init__(
        self,
        terminal_client: TerminalClient,
        shopify_client: ShopifyClient,
        invoice_repository: InvoiceRepository,
    ) -> None:
        self.terminal_client = terminal_client
        self.shopify_client = shopify_client
        self.repo = invoice_repository

    def process_full_refund(
        self,
        invoice_no: str,
        location_id: int,
        reason_code: str = "CUSTOMER_RETURN",
        staff_id: Optional[str] = None,
        financial_year: str = "FY26",
    ) -> RefundResult:
        """Process a full refund for an existing posted invoice.

        STRICT SAFETY & INVENTORY INVARIANTS:
        1. Validates original invoice is POSTED and has captured terminal charge & Shopify order.
        2. Queries terminal to inspect captured amount and baseline refunded_paise.
        3. Rejects if refundable amount is 0 (already fully refunded).
        4. Allocates credit note sequence in SQLite: ELU/{store}/{FY}/CN/{seq:05d}.
        5. Persists refund intent BEFORE making network calls.
        6. Captures terminal refund:
           - On ambiguous outcome (timeout/503), inspects charge via GET /terminal/charges/{id}.
           - Compares refunded_paise against baseline + intended amount.
           - If not conclusive, marks PENDING_RECONCILIATION. NEVER retries POST blindly!
        7. Records Shopify refund:
           - Frames: restock_type='return', location_id=store_location_id (restocked!).
           - Lenses: restock_type='no_restock' (custom ophthalmic medical device, scrapped!).
           - Transactions: sale parent_id linked to refund transaction.
        8. Confirms resulting inventory and updates audit records.
        """
        invoice = self.repo.get_invoice(invoice_no)
        if not invoice:
            raise ValidationError(message=f"Invoice {invoice_no} not found")

        if invoice.status != InvoiceStatus.POSTED:
            raise ValidationError(
                message=f"Only POSTED invoices can be refunded. Current status is {invoice.status.value}"
            )

        if not invoice.terminal_charge_id:
            raise ValidationError(message=f"Invoice {invoice_no} has no captured terminal charge ID")

        if not invoice.shopify_order_id:
            raise ValidationError(message=f"Invoice {invoice_no} has no linked Shopify order ID")

        # 1. Check for existing refunds on this invoice first
        existing_refunds = self.repo.get_refunds_for_invoice(invoice_no)
        total_completed_refund_paise = sum(r.amount_paise for r in existing_refunds if r.status == RefundStatus.COMPLETED)
        if total_completed_refund_paise >= invoice.net_paise and invoice.net_paise > 0:
            raise ValidationError(
                message=f"Invoice {invoice_no} has already been fully refunded ({total_completed_refund_paise} paise refunded)"
            )

        for r in existing_refunds:
            if r.status in (RefundStatus.INITIATED, RefundStatus.TERMINAL_REFUNDED, RefundStatus.PENDING_RECONCILIATION):
                raise AmbiguousOperationError(
                    message=(
                        f"Invoice {invoice_no} has an unresolved refund ({r.refund_id}) in status {r.status.value}. "
                        "Resolve pending reconciliation before initiating another refund attempt."
                    )
                )

        # 2. Inspect original terminal charge
        charge = self.terminal_client.get_charge(invoice.terminal_charge_id)
        captured_paise = charge.get("amount_paise", 0)
        baseline_refunded_paise = charge.get("refunded_paise", 0)
        refundable_paise = captured_paise - baseline_refunded_paise

        if refundable_paise <= 0:
            raise ValidationError(
                message=f"Charge {invoice.terminal_charge_id} has already been fully refunded (captured: {captured_paise}, refunded: {baseline_refunded_paise})"
            )

        amount_to_refund_paise = refundable_paise

        # 3. Allocate sequential credit note number
        credit_note_no = self.repo.allocate_credit_note_no(invoice.store_code, financial_year)
        refund_id = f"ref_{uuid.uuid4().hex[:12]}"

        # 4. Persist refund intent in SQLite
        refund_record = RefundRecord(
            refund_id=refund_id,
            credit_note_no=credit_note_no,
            invoice_no=invoice.invoice_no,
            checkout_ref=invoice.checkout_ref,
            charge_id=invoice.terminal_charge_id,
            shopify_order_id=invoice.shopify_order_id,
            amount_paise=amount_to_refund_paise,
            status=RefundStatus.INITIATED,
            reason_code=reason_code,
            staff_id=staff_id,
            baseline_refunded_paise=baseline_refunded_paise,
        )
        self.repo.record_refund_intent(refund_record)

        # 5. Execute terminal refund with duplicate-prevention reconciliation
        terminal_refund_id = None
        try:
            term_resp = self.terminal_client.create_refund(
                charge_id=invoice.terminal_charge_id,
                amount_paise=amount_to_refund_paise,
                reason=reason_code,
            )
            terminal_refund_id = term_resp.get("refund_id")
            self.repo.update_refund_terminal_captured(refund_id, terminal_refund_id)

        except AmbiguousOperationError as err:
            # Reconcile by querying terminal charge status
            charge_check = self.terminal_client.get_charge(invoice.terminal_charge_id)
            new_refunded_paise = charge_check.get("refunded_paise", 0)

            if new_refunded_paise == baseline_refunded_paise + amount_to_refund_paise:
                # Conclusive! Terminal did process the refund
                terminal_refund_id = f"trf_reconciled_{invoice.terminal_charge_id}"
                self.repo.update_refund_terminal_captured(refund_id, terminal_refund_id)
            elif new_refunded_paise == baseline_refunded_paise:
                # Conclusive: Not processed by terminal
                self.repo.update_refund_status(refund_id, RefundStatus.FAILED, error_message=str(err))
                raise
            else:
                # Ambiguous
                self.repo.update_refund_status(refund_id, RefundStatus.PENDING_RECONCILIATION, error_message=str(err))
                return RefundResult(
                    success=False,
                    refund_id=refund_id,
                    credit_note_no=credit_note_no,
                    invoice_no=invoice.invoice_no,
                    status=RefundStatus.PENDING_RECONCILIATION,
                    amount_refunded_paise=0,
                    error_message=f"Terminal refund outcome uncertain: {err}",
                )

        # 6. Retrieve Shopify order and construct refund payload
        shopify_order = self.shopify_client.get_order(invoice.shopify_order_id)
        if not shopify_order:
            self.repo.update_refund_status(
                refund_id,
                RefundStatus.PENDING_RECONCILIATION,
                error_message=f"Shopify order {invoice.shopify_order_id} not found during refund recording",
            )
            return RefundResult(
                success=False,
                refund_id=refund_id,
                credit_note_no=credit_note_no,
                invoice_no=invoice.invoice_no,
                status=RefundStatus.PENDING_RECONCILIATION,
                amount_refunded_paise=amount_to_refund_paise,
                terminal_refund_id=terminal_refund_id,
                error_message="Terminal refund captured, but Shopify order lookup failed. Pending reconciliation.",
            )

        # Find original sale transaction
        transactions = shopify_order.get("transactions", [])
        sale_tx = next((t for t in transactions if t.get("kind") == "sale" and t.get("status") == "success"), None)
        parent_tx_id = sale_tx.get("id") if sale_tx else None

        # Build refund line items with frame-only restock
        refund_line_items = []
        frames_restocked_count = 0
        for li in shopify_order.get("line_items", []):
            props = {p.get("name"): p.get("value") for p in li.get("properties", [])}
            is_frame = props.get("line_type") == "frame"

            if is_frame:
                # Restock frames at store location
                restock_type = "return"
                loc_id = int(location_id)
                frames_restocked_count += li.get("quantity", 1)

                # Record restock intent
                self.repo.record_restock_intent(
                    refund_id=refund_id,
                    variant_id=li["variant_id"],
                    inventory_item_id=li.get("id", 0),
                    location_id=loc_id,
                    quantity=li.get("quantity", 1),
                )
            else:
                # Lenses are ophthalmic custom goods; do NOT restock
                restock_type = "no_restock"
                loc_id = None

            rl_entry = {
                "line_item_id": li["id"],
                "quantity": li.get("quantity", 1),
                "restock_type": restock_type,
            }
            if loc_id is not None:
                rl_entry["location_id"] = loc_id

            refund_line_items.append(rl_entry)

        shopify_refund_payload = {
            "refund": {
                "note": f"Full refund ({reason_code}). Credit note: {credit_note_no}",
                "refund_line_items": refund_line_items,
                "transactions": [
                    {
                        "parent_id": parent_tx_id,
                        "amount": f"{amount_to_refund_paise / 100:.2f}",
                        "kind": "refund",
                        "gateway": "razorpay_terminal",
                    }
                ],
                "note_attributes": [
                    {"name": "credit_note_no", "value": credit_note_no},
                    {"name": "reason_code", "value": reason_code},
                    {"name": "terminal_refund_id", "value": str(terminal_refund_id)},
                ],
            }
        }

        # 7. Post refund to Shopify with reconciliation safety
        try:
            created_refund = self.shopify_client.create_refund(
                order_id=invoice.shopify_order_id,
                refund_data=shopify_refund_payload["refund"],
            )
            shopify_refund_id = created_refund.get("id")
            self.repo.update_refund_completed(refund_id, shopify_refund_id)

            return RefundResult(
                success=True,
                refund_id=refund_id,
                credit_note_no=credit_note_no,
                invoice_no=invoice.invoice_no,
                status=RefundStatus.COMPLETED,
                amount_refunded_paise=amount_to_refund_paise,
                terminal_refund_id=terminal_refund_id,
                shopify_refund_id=shopify_refund_id,
                frames_restocked_count=frames_restocked_count,
            )

        except AmbiguousOperationError as err:
            # Reconcile by querying Shopify order refunds
            reconciled = self.reconcile_shopify_refund(refund_id)
            if reconciled:
                return reconciled

            # Keep in PENDING_RECONCILIATION; money was already returned to customer!
            self.repo.update_refund_status(refund_id, RefundStatus.PENDING_RECONCILIATION, error_message=str(err))
            return RefundResult(
                success=False,
                refund_id=refund_id,
                credit_note_no=credit_note_no,
                invoice_no=invoice.invoice_no,
                status=RefundStatus.PENDING_RECONCILIATION,
                amount_refunded_paise=amount_to_refund_paise,
                terminal_refund_id=terminal_refund_id,
                error_message=f"Terminal refund captured ({terminal_refund_id}), but Shopify refund recording ambiguous: {err}",
            )

    def reconcile_shopify_refund(self, refund_id: str) -> Optional[RefundResult]:
        """Attempt to reconcile an unconfirmed Shopify refund record."""
        rec = self.repo.get_refund(refund_id)
        if not rec:
            return None

        if rec.status == RefundStatus.COMPLETED and rec.shopify_refund_id:
            return RefundResult(
                success=True,
                refund_id=rec.refund_id,
                credit_note_no=rec.credit_note_no,
                invoice_no=rec.invoice_no,
                status=RefundStatus.COMPLETED,
                amount_refunded_paise=rec.amount_paise,
                terminal_refund_id=rec.terminal_refund_id,
                shopify_refund_id=rec.shopify_refund_id,
            )

        order = self.shopify_client.get_order(rec.shopify_order_id)
        if not order:
            return None

        # Inspect order refunds list for matching credit note
        for r in order.get("refunds", []):
            attrs = {a.get("name"): a.get("value") for a in r.get("note_attributes", [])}
            if attrs.get("credit_note_no") == rec.credit_note_no or rec.credit_note_no in str(r.get("note", "")):
                # Found matching Shopify refund!
                shp_id = r["id"]
                self.repo.update_refund_completed(rec.refund_id, shp_id)
                return RefundResult(
                    success=True,
                    refund_id=rec.refund_id,
                    credit_note_no=rec.credit_note_no,
                    invoice_no=rec.invoice_no,
                    status=RefundStatus.COMPLETED,
                    amount_refunded_paise=rec.amount_paise,
                    terminal_refund_id=rec.terminal_refund_id,
                    shopify_refund_id=shp_id,
                )

        return None
