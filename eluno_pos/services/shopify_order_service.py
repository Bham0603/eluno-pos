"""Shopify order generation and non-idempotent reconciliation service."""

from datetime import date
import json
from typing import Any, Dict, List, Optional

from eluno_pos.adapters.exceptions import (
    AmbiguousOperationError,
    ElunoPosError,
    ValidationError,
)
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.domain.cart import Cart, CartLine
from eluno_pos.domain.invoice import Invoice, InvoiceStatus
from eluno_pos.domain.offer import AppliedOffer
from eluno_pos.domain.payment import PaymentRecord, PaymentStatus
from eluno_pos.repositories.invoice_repository import InvoiceRepository
from eluno_pos.services.gst_service import GstService


class ShopifyOrderService:
    """Service to create and reconcile Shopify orders with durable invoice linking."""

    def __init__(
        self,
        shopify_client: ShopifyClient,
        invoice_repository: InvoiceRepository,
        gst_service: Optional[GstService] = None,
    ) -> None:
        self.shopify_client = shopify_client
        self.repo = invoice_repository
        self.gst_service = gst_service or GstService()

    def build_order_payload(
        self,
        cart: Cart,
        invoice: Invoice,
        payment: PaymentRecord,
        applied_offers: List[AppliedOffer],
        location_id: int,
        store_code: str,
        channel: str = "walk_in",
        staff_id: Optional[str] = None,
        transaction_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Construct a valid Shopify Admin Order payload from cart, invoice, and payment.

        DATA MINIMIZATION RULE:
        Only rx_id is sent to Shopify as an external reference. Full clinical prescription
        data (OD, OS, SPH, CYL, AXIS, ADD, PD) is NEVER sent to Shopify.
        """
        tx_date = transaction_date or date.today()

        # 1. Discount applications
        discount_applications = []
        offer_to_idx: Dict[str, int] = {}
        for idx, app in enumerate(applied_offers):
            offer_to_idx[app.offer_id] = idx
            discount_applications.append({
                "type": "manual",
                "title": app.offer_id,
                "value": f"{app.discount_paise / 100:.2f}",
                "value_type": "fixed_amount",
                "allocation_method": "each",
                "target_selection": "explicit",
                "target_type": "line_item",
            })

        # 2. Line items with tax lines, properties, and discount allocations
        line_items = []
        for line in cart.lines:
            # Calculate line tax from Phase 3 GstService
            line_tax = self.gst_service.calculate_line_tax(
                hsn=line.hsn,
                net_inclusive_paise=line.net_price_paise,
                transaction_date=tx_date,
            )

            # Reconcile exact paisa: taxable + CGST + SGST == net_price_paise
            if not line_tax.is_reconciled:
                raise ValueError(
                    f"GST reconciliation failed for line {line.line_id}: "
                    f"taxable({line_tax.taxable_value_paise}) + "
                    f"cgst({line_tax.cgst_paise}) + "
                    f"sgst({line_tax.sgst_paise}) != "
                    f"net({line.net_price_paise})"
                )

            # Build tax lines
            half_rate = (line_tax.rate_bps / 2) / 10000.0
            tax_lines = [
                {"title": "CGST", "rate": half_rate, "price": f"{line_tax.cgst_paise / 100:.2f}"},
                {"title": "SGST", "rate": half_rate, "price": f"{line_tax.sgst_paise / 100:.2f}"},
            ]

            # Build line properties (data-minimized)
            props: List[Dict[str, str]] = [
                {"name": "line_type", "value": line.line_type},
                {"name": "hsn", "value": line.hsn},
            ]
            if line.pair_ref:
                props.append({"name": "pair_ref", "value": str(line.pair_ref)})

            if line.line_type == "lens":
                config_str = f"{line.lens_type or 'single_vision'}/{line.index or 1.5}/{line.coating or 'standard'}"
                props.append({"name": "lens_config", "value": config_str})
                if line.rx_id:
                    # Data minimization: send only rx_id, never clinical optical values
                    props.append({"name": "rx_id", "value": line.rx_id})

            if line.for_customer_id:
                props.append({"name": "for_customer_id", "value": str(line.for_customer_id)})

            # Build discount allocations
            discount_allocations = []
            for d in line.discount_allocations:
                if d.offer_id in offer_to_idx:
                    app_idx = offer_to_idx[d.offer_id]
                    discount_allocations.append({
                        "amount": f"{d.amount_paise / 100:.2f}",
                        "discount_application_index": app_idx,
                    })

            line_items.append({
                "variant_id": line.variant_id,
                "quantity": line.quantity,
                "price": f"{line.unit_price_paise / 100:.2f}",
                "total_discount": f"{line.total_discount_paise / 100:.2f}",
                "tax_lines": tax_lines,
                "properties": props,
                "discount_allocations": discount_allocations,
            })

        # 3. Note attributes
        note_attributes = [
            {"name": "invoice_no", "value": invoice.invoice_no},
            {"name": "checkout_ref", "value": invoice.checkout_ref},
            {"name": "terminal_charge_id", "value": str(payment.charge_id or "")},
            {"name": "store_code", "value": store_code},
            {"name": "channel", "value": channel},
            {"name": "payment_method", "value": payment.method},
        ]
        if staff_id:
            note_attributes.append({"name": "staff_id", "value": staff_id})

        # 4. Transactions
        transactions = [
            {
                "kind": "sale",
                "status": "success",
                "gateway": "razorpay_terminal",
                "amount": f"{payment.amount_paise / 100:.2f}",
                "authorization": payment.charge_id or "term_auth",
            }
        ]

        order_payload: Dict[str, Any] = {
            "location_id": int(location_id),
            "taxes_included": True,
            "currency": "INR",
            "inventory_behaviour": "bypass",
            "line_items": line_items,
            "note_attributes": note_attributes,
            "transactions": transactions,
            "discount_applications": discount_applications,
        }

        if cart.customer:
            order_payload["customer"] = {"id": cart.customer.id}

        return order_payload

    def create_order(
        self,
        payload: Dict[str, Any],
        invoice: Invoice,
        location_id: int,
    ) -> Dict[str, Any]:
        """Create a Shopify order with pre-persistence and reconciliation safety.

        SAFETY INVARIANT:
        Shopify order creation is non-idempotent.
        1. Persist the intent in SQLite before sending the HTTP POST.
        2. If Shopify responds with 201 Created, record order ID and transition invoice to POSTED.
        3. If call fails ambiguously (TimeoutError / ConnectionError / 503):
           - DO NOT BLINDLY RETRY!
           - Keep invoice in PENDING_RECONCILIATION.
           - Attempt reconciliation by searching recent orders in Shopify for matching note_attributes.
           - If found, complete linking to POSTED.
           - If not found immediately, leave in PENDING_RECONCILIATION and raise AmbiguousOperationError.
        """
        # 1. Idempotency check: if invoice already has shopify_order_id, retrieve and return
        if invoice.shopify_order_id:
            existing = self.shopify_client.get_order(invoice.shopify_order_id)
            if existing:
                return existing

        # 2. Persist intent before network POST
        self.repo.record_shopify_intent(
            invoice_no=invoice.invoice_no,
            checkout_ref=invoice.checkout_ref,
            payload=payload,
            status="PENDING",
        )

        # 3. Call Shopify create order
        try:
            created_order = self.shopify_client.create_order(payload)
            order_id = created_order.get("id")
            if not order_id:
                raise ElunoPosError(f"Shopify returned order without ID: {created_order}")

            # 4. Success: persist order id and mark POSTED
            self.repo.update_shopify_intent(
                invoice_no=invoice.invoice_no,
                status="COMPLETED",
                shopify_order_id=order_id,
            )
            self.repo.update_invoice_shopify_order(
                invoice_no=invoice.invoice_no,
                shopify_order_id=order_id,
                status=InvoiceStatus.POSTED,
            )
            return created_order

        except AmbiguousOperationError as err:
            # 5. Ambiguous outcome: mark intent AMBIGUOUS
            self.repo.update_shopify_intent(
                invoice_no=invoice.invoice_no,
                status="AMBIGUOUS",
                error_message=str(err),
            )
            # Attempt immediate reconciliation
            reconciled = self.reconcile_order(invoice.invoice_no, location_id=location_id)
            if reconciled:
                return reconciled

            # Still unresolved; leave invoice in PENDING_RECONCILIATION
            raise

    def reconcile_order(
        self,
        invoice_no: str,
        location_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Reconcile an in-flight or ambiguous order by querying Shopify recent orders.

        Inspects Shopify orders for matching invoice_no or checkout_ref in note_attributes.
        If a match is found, links the shopify_order_id and marks invoice POSTED.
        """
        invoice = self.repo.get_invoice(invoice_no)
        if not invoice:
            raise ValueError(f"Invoice {invoice_no} not found")

        if invoice.status == InvoiceStatus.POSTED and invoice.shopify_order_id:
            return self.shopify_client.get_order(invoice.shopify_order_id)

        # Query Shopify recent orders
        orders = self.shopify_client.get_orders(
            location_id=location_id,
            status="any",
            limit=50,
        )

        for o in orders:
            attrs = {a.get("name"): a.get("value") for a in o.get("note_attributes", [])}
            if attrs.get("invoice_no") == invoice.invoice_no or attrs.get("checkout_ref") == invoice.checkout_ref:
                # Match found! The order exists in Shopify
                order_id = o["id"]
                self.repo.update_shopify_intent(
                    invoice_no=invoice.invoice_no,
                    status="RECONCILED",
                    shopify_order_id=order_id,
                )
                self.repo.update_invoice_shopify_order(
                    invoice_no=invoice.invoice_no,
                    shopify_order_id=order_id,
                    status=InvoiceStatus.POSTED,
                )
                return o

        return None
