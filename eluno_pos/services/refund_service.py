"""Refund orchestration service for Eluno POS."""

from typing import Any, Dict, List, Optional
import uuid

from eluno_pos.adapters.exceptions import AmbiguousOperationError, ValidationError
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.adapters.terminal_client import TerminalClient
from eluno_pos.domain.invoice import InvoiceStatus
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

        STRICT SAFETY, CONCURRENCY & LIFECYCLE INVARIANTS:
        1. Atomic Check & Intent Creation:
           - In an atomic SQLite transaction (BEGIN IMMEDIATE), verifies invoice is POSTED.
           - Checks that no active or pending refund exists for the invoice or charge.
           - Checks that total completed refunds do not exceed invoice net amount.
           - Inserts a new refund intent with status = INITIATED and credit_note_no = NULL.
           - Any concurrent request is immediately rejected inside the database transaction.
        2. Terminal Charge Baseline Inspection:
           - Queries terminal charge to inspect captured amount and baseline refunded_paise.
           - Rejects if refundable amount is 0 (already fully refunded on gateway).
           - Updates refund status to FAILED without consuming a credit note number.
        3. Terminal Refund Execution:
           - Initiates refund via POST /terminal/refunds.
           - On ambiguous outcome (timeout/503), inspects charge via GET /terminal/charges/{id}.
           - If conclusive, proceeds; otherwise marks PENDING_RECONCILIATION.
           - NEVER blindly retries POST requests on the stateless gateway!
        4. Credit Note Allocation & Persistence (Post-Terminal Confirmation):
           - Only AFTER terminal refund is captured is a sequential credit note number allocated.
           - Ensures failed or cancelled attempts never consume issued credit note numbers.
        5. Shopify Refund & Frame-Only Restocking:
           - Frames: restock_type='return', location_id=store_location_id (restocked!).
           - Lenses: restock_type='no_restock' (custom ophthalmic medical device, scrapped!).
           - Submits refund with credit_note_no and terminal_refund_id.
        6. Completion:
           - Marks refund as COMPLETED with shopify_refund_id.
        """
        invoice = self.repo.get_invoice(invoice_no)
        if not invoice:
            raise ValidationError(message=f"Invoice {invoice_no} not found")

        amount_to_refund_paise = invoice.net_paise
        refund_id = f"ref_{uuid.uuid4().hex[:12]}"

        # 1. ATOMIC CHECK & INTENT CREATION inside SQLite BEGIN IMMEDIATE
        # Guarantees concurrent attempts cannot pass checks simultaneously.
        self.repo.create_refund_intent_atomically(
            invoice_no=invoice_no,
            refund_id=refund_id,
            amount_paise=amount_to_refund_paise,
            reason_code=reason_code,
            staff_id=staff_id,
        )

        # 2. Inspect original terminal charge on the payment gateway
        charge = self.terminal_client.get_charge(invoice.terminal_charge_id)
        captured_paise = charge.get("amount_paise", 0)
        baseline_refunded_paise = charge.get("refunded_paise", 0)
        refundable_paise = captured_paise - baseline_refunded_paise

        if refundable_paise <= 0:
            err_msg = (
                f"Charge {invoice.terminal_charge_id} has already been fully refunded on gateway "
                f"(captured: {captured_paise}, refunded: {baseline_refunded_paise})"
            )
            self.repo.update_refund_status(refund_id, RefundStatus.FAILED, error_message=err_msg)
            raise ValidationError(message=err_msg)

        if amount_to_refund_paise > refundable_paise:
            err_msg = (
                f"Requested refund amount ({amount_to_refund_paise}) exceeds terminal refundable balance ({refundable_paise})"
            )
            self.repo.update_refund_status(refund_id, RefundStatus.FAILED, error_message=err_msg)
            raise ValidationError(message=err_msg)

        # Durably store baseline refunded_paise before attempting terminal refund POST
        self.repo.update_refund_baseline(refund_id, baseline_refunded_paise)

        # 3. Execute terminal refund with duplicate-prevention reconciliation
        terminal_refund_id = None
        try:
            term_resp = self.terminal_client.create_refund(
                charge_id=invoice.terminal_charge_id,
                amount_paise=amount_to_refund_paise,
                reason=reason_code,
            )
            terminal_refund_id = term_resp.get("refund_id")
            self.repo.update_refund_terminal_captured(refund_id, terminal_refund_id, baseline_refunded_paise)

        except AmbiguousOperationError as err:
            # Reconcile by querying terminal charge status
            charge_check = self.terminal_client.get_charge(invoice.terminal_charge_id)
            new_refunded_paise = charge_check.get("refunded_paise", 0)

            if new_refunded_paise == baseline_refunded_paise + amount_to_refund_paise:
                # Conclusive! Terminal did process the refund
                terminal_refund_id = f"trf_reconciled_{invoice.terminal_charge_id}"
                self.repo.update_refund_terminal_captured(refund_id, terminal_refund_id, baseline_refunded_paise)
            else:
                # Inconclusive / eventual consistency / read-after-write lag:
                # Do NOT mark as FAILED! Merely observing unchanged refunded_paise on an immediate
                # GET response cannot rule out that the gateway processed or is committing the refund.
                # Keep the refund in PENDING_RECONCILIATION so operator or recovery job can safely inspect later.
                self.repo.update_refund_status(refund_id, RefundStatus.PENDING_RECONCILIATION, error_message=str(err))
                return RefundResult(
                    success=False,
                    refund_id=refund_id,
                    credit_note_no=None,
                    invoice_no=invoice.invoice_no,
                    status=RefundStatus.PENDING_RECONCILIATION,
                    amount_refunded_paise=0,
                    error_message=f"Terminal refund outcome uncertain (read-after-write inconclusive): {err}",
                )
        except Exception as err:
            self.repo.update_refund_status(refund_id, RefundStatus.FAILED, error_message=str(err))
            raise

        # 4. ALLOCATE AND PERSIST CREDIT NOTE NUMBER
        # Only reached after terminal confirmation! Failed attempts never consume credit note numbers.
        credit_note_no = self.repo.allocate_and_persist_credit_note(
            refund_id=refund_id,
            store_code=invoice.store_code,
            financial_year=financial_year,
        )

        # 5. Retrieve Shopify order and construct refund payload
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

                # Record restock intent for audit
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

        # 6. Post refund to Shopify with reconciliation safety
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
            self.repo.update_refund_status(
                refund_id,
                RefundStatus.PENDING_RECONCILIATION,
                error_message=f"Terminal refund captured, but Shopify refund timed out: {err}",
            )
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
        except Exception as err:
            self.repo.update_refund_status(
                refund_id,
                RefundStatus.PENDING_RECONCILIATION,
                error_message=f"Terminal refund captured, but Shopify error: {err}",
            )
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
        if rec.credit_note_no:
            for r in order.get("refunds", []):
                attrs = {a.get("name"): a.get("value") for a in r.get("note_attributes", [])}
                if attrs.get("credit_note_no") == rec.credit_note_no or rec.credit_note_no in str(r.get("note", "")):
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

    def resume_or_reconcile_refund(
        self,
        refund_id: str,
        location_id: int,
        financial_year: str = "FY26",
    ) -> Optional[RefundResult]:
        """Recover a refund after an application crash or timeout at any lifecycle phase."""
        rec = self.repo.get_refund(refund_id)
        if not rec:
            return None

        if rec.status == RefundStatus.COMPLETED:
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

        invoice = self.repo.get_invoice(rec.invoice_no)
        if not invoice:
            return None

        # Phase A: If PENDING_RECONCILIATION, inspect terminal first
        if rec.status == RefundStatus.PENDING_RECONCILIATION and not rec.terminal_refund_id:
            charge = self.terminal_client.get_charge(rec.charge_id)
            current_refunded = charge.get("refunded_paise", 0)
            expected_refunded = rec.baseline_refunded_paise + rec.amount_paise
            if current_refunded == expected_refunded:
                self.repo.update_refund_terminal_captured(
                    refund_id,
                    f"trf_reconciled_{rec.charge_id}",
                    rec.baseline_refunded_paise,
                )
                rec = self.repo.get_refund(refund_id)
            else:
                # Still inconclusive or not yet reflected: Keep pending, DO NOT initiate second POST
                return RefundResult(
                    success=False,
                    refund_id=rec.refund_id,
                    credit_note_no=rec.credit_note_no,
                    invoice_no=rec.invoice_no,
                    status=RefundStatus.PENDING_RECONCILIATION,
                    amount_refunded_paise=0,
                    error_message=f"Terminal refund still inconclusive (current: {current_refunded}, expected: {expected_refunded})",
                )

        # Phase B: If TERMINAL_REFUNDED, allocate credit note if missing, then sync Shopify
        if rec.status in (RefundStatus.TERMINAL_REFUNDED, RefundStatus.PENDING_RECONCILIATION):
            credit_note_no = rec.credit_note_no
            if not credit_note_no:
                credit_note_no = self.repo.allocate_and_persist_credit_note(
                    refund_id=refund_id,
                    store_code=invoice.store_code,
                    financial_year=financial_year,
                )
                rec = self.repo.get_refund(refund_id)

            # Check if Shopify order already has this refund
            reconciled = self.reconcile_shopify_refund(refund_id)
            if reconciled and reconciled.success:
                return reconciled

            # Otherwise, submit the refund to Shopify using the preserved credit_note_no
            order = self.shopify_client.get_order(rec.shopify_order_id)
            if not order:
                return None

            transactions = order.get("transactions", [])
            sale_tx = next((t for t in transactions if t.get("kind") == "sale" and t.get("status") == "success"), None)
            parent_tx_id = sale_tx.get("id") if sale_tx else None

            refund_line_items = []
            frames_restocked_count = 0
            for li in order.get("line_items", []):
                props = {p.get("name"): p.get("value") for p in li.get("properties", [])}
                is_frame = props.get("line_type") == "frame"
                if is_frame:
                    restock_type = "return"
                    loc_id = int(location_id)
                    frames_restocked_count += li.get("quantity", 1)
                else:
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

            payload = {
                "refund": {
                    "note": f"Full refund ({rec.reason_code}). Credit note: {credit_note_no}",
                    "refund_line_items": refund_line_items,
                    "transactions": [
                        {
                            "parent_id": parent_tx_id,
                            "amount": f"{rec.amount_paise / 100:.2f}",
                            "kind": "refund",
                            "gateway": "razorpay_terminal",
                        }
                    ],
                    "note_attributes": [
                        {"name": "credit_note_no", "value": credit_note_no},
                        {"name": "reason_code", "value": rec.reason_code},
                        {"name": "terminal_refund_id", "value": str(rec.terminal_refund_id)},
                    ],
                }
            }

            created_refund = self.shopify_client.create_refund(
                order_id=rec.shopify_order_id,
                refund_data=payload["refund"],
            )
            shp_id = created_refund.get("id")
            self.repo.update_refund_completed(refund_id, shp_id)
            return RefundResult(
                success=True,
                refund_id=refund_id,
                credit_note_no=credit_note_no,
                invoice_no=invoice.invoice_no,
                status=RefundStatus.COMPLETED,
                amount_refunded_paise=rec.amount_paise,
                terminal_refund_id=rec.terminal_refund_id,
                shopify_refund_id=shp_id,
                frames_restocked_count=frames_restocked_count,
            )

        return None
