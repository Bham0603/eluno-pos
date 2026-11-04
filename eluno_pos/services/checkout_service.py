"""End-to-end checkout orchestration service for Eluno POS."""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional
import uuid

from eluno_pos.adapters.exceptions import (
    AmbiguousOperationError,
    ElunoPosError,
    ValidationError,
)
from eluno_pos.domain.cart import Cart
from eluno_pos.domain.invoice import Invoice, InvoiceLine, InvoiceStatus
from eluno_pos.domain.offer import AppliedOffer
from eluno_pos.domain.payment import PaymentRecord, PaymentStatus
from eluno_pos.domain.tax import OrderTaxSummary
from eluno_pos.repositories.invoice_repository import InvoiceRepository
from eluno_pos.services.gst_service import GstService
from eluno_pos.services.offer_service import OfferConflictStrategy, OfferService
from eluno_pos.services.payment_service import PaymentService
from eluno_pos.services.shopify_order_service import ShopifyOrderService


@dataclass
class CheckoutSession:
    """In-progress checkout session with evaluated financial totals."""
    checkout_ref: str
    store_code: str
    financial_year: str
    location_id: int
    invoice: Invoice
    cart: Cart
    tax_summary: OrderTaxSummary
    applied_offers: List[AppliedOffer]
    channel: str = "walk_in"
    staff_id: Optional[str] = None
    transaction_date: Optional[date] = None
    prescription_map: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckoutResult:
    """Final outcome of an orchestrated checkout attempt."""
    success: bool
    invoice_no: str
    checkout_ref: str
    status: InvoiceStatus
    terminal_charge_id: Optional[str] = None
    shopify_order_id: Optional[int] = None
    shopify_order_number: Optional[int] = None
    net_payable_paise: int = 0
    error_message: Optional[str] = None

    @property
    def is_pending_reconciliation(self) -> bool:
        """Whether the transaction is captured but awaiting Shopify confirmation."""
        return self.status == InvoiceStatus.PENDING_RECONCILIATION and self.terminal_charge_id is not None


class CheckoutService:
    """Coordinates cart evaluation, invoice sequencing, terminal payment, and Shopify order creation."""

    def __init__(
        self,
        invoice_repository: InvoiceRepository,
        payment_service: PaymentService,
        shopify_order_service: ShopifyOrderService,
        offer_service: Optional[OfferService] = None,
        gst_service: Optional[GstService] = None,
    ) -> None:
        self.repo = invoice_repository
        self.payment_service = payment_service
        self.shopify_order_service = shopify_order_service
        self.offer_service = offer_service or OfferService()
        self.gst_service = gst_service or GstService()

    def prepare_checkout(
        self,
        cart: Cart,
        store_code: str,
        location_id: int,
        financial_year: str = "FY26",
        channel: str = "walk_in",
        staff_id: Optional[str] = None,
        transaction_date: Optional[date] = None,
        offer_strategy: OfferConflictStrategy = OfferConflictStrategy.MAX_SAVINGS,
        prescription_map: Optional[Dict[str, Any]] = None,
    ) -> CheckoutSession:
        """Evaluate cart, compute GST, and allocate sequential invoice number.

        STAGES:
        1. Validates non-empty cart.
        2. Evaluates offers (Phase 3).
        3. Computes GST across lines reconciled to the paisa (Phase 3).
        4. Allocates sequential invoice number in SQLite with BEGIN IMMEDIATE.
        """
        if not cart.lines:
            raise ValidationError(message="Cannot checkout with an empty cart")

        tx_date = transaction_date or date.today()
        prescription_map = prescription_map or {}

        # 1. Evaluate offers
        offer_result = self.offer_service.evaluate_cart(
            cart=cart,
            transaction_date=tx_date,
            strategy=offer_strategy,
        )

        # 2. Compute order GST
        line_items_for_tax = [(l.hsn, l.net_price_paise) for l in cart.lines]
        tax_summary = self.gst_service.calculate_order_tax(line_items_for_tax, tx_date)

        # 3. Build snapshot invoice lines
        checkout_ref = f"chk_{uuid.uuid4().hex[:12]}"
        inv_lines: List[InvoiceLine] = []
        for i, line in enumerate(cart.lines):
            lens_cfg = None
            if line.line_type == "lens":
                lens_cfg = f"{line.lens_type or 'single_vision'}/{line.index or 1.5}/{line.coating or 'standard'}"

            lt = tax_summary.line_taxes[i] if i < len(tax_summary.line_taxes) else None
            taxable_paise = lt.taxable_value_paise if lt else 0
            cgst_paise = lt.cgst_paise if lt else 0
            sgst_paise = lt.sgst_paise if lt else 0
            tax_paise = lt.total_gst_paise if lt else 0

            inv_lines.append(
                InvoiceLine(
                    line_id=line.line_id,
                    line_type=line.line_type,
                    variant_id=line.variant_id,
                    sku=line.sku,
                    title=line.title,
                    quantity=line.quantity,
                    unit_price_paise=line.unit_price_paise,
                    gross_paise=line.gross_price_paise,
                    discount_paise=line.total_discount_paise,
                    net_paise=line.net_price_paise,
                    hsn=line.hsn,
                    taxable_paise=taxable_paise,
                    cgst_paise=cgst_paise,
                    sgst_paise=sgst_paise,
                    tax_paise=tax_paise,
                    pair_ref=line.pair_ref,
                    rx_id=line.rx_id,
                    lens_config=lens_cfg,
                    properties=[],
                )
            )

        # 4. Allocate invoice sequence in SQLite
        customer_id = cart.customer.id if cart.customer else None
        invoice = self.repo.allocate_invoice(
            store_code=store_code,
            financial_year=financial_year,
            checkout_ref=checkout_ref,
            customer_id=customer_id,
            gross_paise=cart.gross_total_paise,
            discount_paise=cart.total_discount_paise,
            net_paise=cart.net_total_paise,
            taxable_paise=tax_summary.total_taxable_paise,
            tax_paise=tax_summary.total_gst_paise,
            lines=inv_lines,
        )

        return CheckoutSession(
            checkout_ref=checkout_ref,
            store_code=store_code,
            financial_year=financial_year,
            location_id=location_id,
            invoice=invoice,
            cart=cart,
            tax_summary=tax_summary,
            applied_offers=offer_result.applied_offers,
            channel=channel,
            staff_id=staff_id,
            transaction_date=tx_date,
            prescription_map=prescription_map,
        )

    def complete_checkout(
        self,
        session: CheckoutSession,
        payment_method: str = "upi",
    ) -> CheckoutResult:
        """Execute payment on terminal and create order in Shopify.

        CRITICAL LIFECYCLE / RECOVERY INVARIANTS:
        1. Capture payment on terminal:
           - Retries documented synthetic 503 safely with identical order_ref.
           - On ambiguous failure (socket timeout/drop), leaves invoice in PENDING_RECONCILIATION.
           - Does NOT proceed to Shopify without confirmed payment.
        2. Create Shopify order:
           - Builds order payload with tax lines, discounts, prescription, and note_attributes.
           - Persists intent in SQLite before sending POST.
           - If Shopify succeeds, transitions invoice to POSTED.
           - If Shopify fails ambiguously (timeout/503), invoice remains PENDING_RECONCILIATION.
           - Under no circumstances is a paid invoice marked CANCELLED!
        """
        # Step 1: Charge terminal
        try:
            payment_record = self.payment_service.process_payment(
                invoice=session.invoice,
                amount_paise=session.cart.net_total_paise,
                method=payment_method,
            )
        except AmbiguousOperationError as err:
            # Payment outcome is ambiguous; do NOT cancel invoice, leave pending
            return CheckoutResult(
                success=False,
                invoice_no=session.invoice.invoice_no,
                checkout_ref=session.checkout_ref,
                status=InvoiceStatus.PENDING_RECONCILIATION,
                net_payable_paise=session.cart.net_total_paise,
                error_message=f"Terminal payment status ambiguous: {err}",
            )
        except Exception as err:
            # Payment definitively failed/declined pre-capture: mark invoice CANCELLED
            self.repo.update_invoice_status(session.invoice.invoice_no, InvoiceStatus.CANCELLED)
            return CheckoutResult(
                success=False,
                invoice_no=session.invoice.invoice_no,
                checkout_ref=session.checkout_ref,
                status=InvoiceStatus.CANCELLED,
                net_payable_paise=session.cart.net_total_paise,
                error_message=f"Payment declined/failed: {err}",
            )

        # Refresh invoice with captured charge reference
        refreshed_invoice = self.repo.get_invoice(session.invoice.invoice_no)
        assert refreshed_invoice is not None

        # Step 2: Build Shopify payload
        payload = self.shopify_order_service.build_order_payload(
            cart=session.cart,
            invoice=refreshed_invoice,
            payment=payment_record,
            applied_offers=session.applied_offers,
            location_id=session.location_id,
            store_code=session.store_code,
            channel=session.channel,
            staff_id=session.staff_id,
            transaction_date=session.transaction_date,
        )

        # Step 3: Create Shopify order
        try:
            created_order = self.shopify_order_service.create_order(
                payload=payload,
                invoice=refreshed_invoice,
                location_id=session.location_id,
            )
            return CheckoutResult(
                success=True,
                invoice_no=refreshed_invoice.invoice_no,
                checkout_ref=session.checkout_ref,
                status=InvoiceStatus.POSTED,
                terminal_charge_id=payment_record.charge_id,
                shopify_order_id=created_order.get("id"),
                shopify_order_number=created_order.get("order_number"),
                net_payable_paise=session.cart.net_total_paise,
            )
        except AmbiguousOperationError as err:
            # Shopify response was lost or timed out.
            # INVARIANT: Invoice stays in PENDING_RECONCILIATION; money was captured!
            return CheckoutResult(
                success=False,
                invoice_no=refreshed_invoice.invoice_no,
                checkout_ref=session.checkout_ref,
                status=InvoiceStatus.PENDING_RECONCILIATION,
                terminal_charge_id=payment_record.charge_id,
                net_payable_paise=session.cart.net_total_paise,
                error_message=f"Payment captured ({payment_record.charge_id}) but Shopify order outcome ambiguous: {err}",
            )

    def reconcile_pending_checkout(
        self,
        invoice_no: str,
        location_id: Optional[int] = None,
    ) -> CheckoutResult:
        """Attempt to reconcile an unconfirmed order against Shopify."""
        inv = self.repo.get_invoice(invoice_no)
        if not inv:
            raise ValueError(f"Invoice {invoice_no} not found")

        reconciled = self.shopify_order_service.reconcile_order(invoice_no, location_id=location_id)
        if reconciled:
            return CheckoutResult(
                success=True,
                invoice_no=inv.invoice_no,
                checkout_ref=inv.checkout_ref,
                status=InvoiceStatus.POSTED,
                terminal_charge_id=inv.terminal_charge_id,
                shopify_order_id=reconciled.get("id"),
                shopify_order_number=reconciled.get("order_number"),
                net_payable_paise=inv.net_paise,
            )

        return CheckoutResult(
            success=False,
            invoice_no=inv.invoice_no,
            checkout_ref=inv.checkout_ref,
            status=inv.status,
            terminal_charge_id=inv.terminal_charge_id,
            net_payable_paise=inv.net_paise,
            error_message="Order could not yet be confirmed in Shopify; remains pending reconciliation.",
        )

