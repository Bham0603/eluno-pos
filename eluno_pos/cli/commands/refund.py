"""Refund command orchestration for Eluno POS CLI."""

from typing import Optional

from eluno_pos.adapters.exceptions import ValidationError
from eluno_pos.cli.context import AppContext
from eluno_pos.cli.formatting import (
    format_inr,
    print_card,
    print_header,
    prompt_confirm,
    prompt_input,
)
from eluno_pos.domain.invoice import InvoiceStatus
from eluno_pos.domain.refund import RefundStatus


def run_refund(
    ctx: AppContext,
    invoice_no: Optional[str] = None,
    location_id: Optional[int] = None,
    reason_code: Optional[str] = None,
    staff_id: Optional[str] = None,
    auto_confirm: bool = False,
) -> int:
    """Execute full store return, terminal refund, credit note issuance, and frame restocking."""
    print_header("Eluno POS — Store Return & Full Refund Workflow", "Payment Reversal, Credit Note & Frame Restock")

    # 1. Invoice Number Input
    target_inv = invoice_no
    if not target_inv:
        target_inv = prompt_input("Enter Invoice Number to Refund (e.g. ELU/HSR/FY26/00001)")

    target_inv = target_inv.strip().upper()
    if not target_inv:
        print("✖ Error: Invoice number is required.")
        return 1

    invoice = ctx.repo.get_invoice(target_inv)
    if not invoice:
        print(f"✖ Error: Invoice {target_inv} not found in store database.")
        return 1

    # 2. Check Refund Eligibility
    if invoice.status != InvoiceStatus.POSTED:
        print(f"✖ Error: Cannot refund invoice in status '{invoice.status.value}'. Only POSTED invoices can be refunded.")
        return 1

    existing_refunds = ctx.repo.get_refunds_for_invoice(invoice.invoice_no)
    completed_paise = sum(r.amount_paise for r in existing_refunds if r.status == RefundStatus.COMPLETED)
    remaining_refundable = invoice.net_paise - completed_paise

    if remaining_refundable <= 0:
        print(f"✖ Error: Invoice {target_inv} has already been fully refunded (Total refunded: {format_inr(completed_paise)}).")
        return 1

    # Display Original Sale Information
    sale_info = [
        ("Invoice Number", invoice.invoice_no),
        ("Store Code", invoice.store_code),
        ("Original Sale Date", invoice.created_at[:10]),
        ("Original Gross", format_inr(invoice.gross_paise)),
        ("Discounts Given", format_inr(invoice.discount_paise)),
        ("Net Amount Paid", format_inr(invoice.net_paise)),
        ("Terminal Charge ID", invoice.terminal_charge_id or "N/A"),
        ("Shopify Order ID", invoice.shopify_order_id or "N/A"),
        ("Refundable Balance", format_inr(remaining_refundable)),
    ]
    print_card("Original Sale Details", sale_info)

    # 3. Store Location for Restocking
    target_loc_id = location_id
    if not target_loc_id:
        locations = ctx.shopify_client.get_locations()
        matching_loc = next((l for l in locations if (l.get("eluno", {}).get("store_code") or l.get("store_code")) == invoice.store_code), None)
        if matching_loc:
            target_loc_id = int(matching_loc["id"])
        else:
            print(f"✖ Error: Could not determine Shopify location ID for store code '{invoice.store_code}'.")
            return 1
    print(f"✔ Restocking Target: Location ID {target_loc_id} ({invoice.store_code})")

    # 4. Reason Code & Staff Member
    reason = reason_code
    if not reason:
        if auto_confirm:
            reason = "CUSTOMER_RETURN"
        else:
            print("\nCommon Return Reasons:")
            print("  • CUSTOMER_RETURN — Customer changed mind / returned unworn")
            print("  • DEFECTIVE_FRAME — Manufacturing flaw / warranty defect")
            print("  • INCORRECT_PRESCRIPTION — Power change / doctor error")
            reason = prompt_input("Enter Return Reason Code", default="CUSTOMER_RETURN").upper()

    operator_staff = staff_id
    if not operator_staff:
        if auto_confirm:
            operator_staff = "STF-01"
        else:
            operator_staff = prompt_input("Enter Authorizing Staff ID", default="STF-01")

    # 5. Explicit Staff Confirmation Gate
    print(f"\nRefund Summary: Reversing {format_inr(remaining_refundable)} to customer's original payment method.")
    if not auto_confirm:
        confirmed = prompt_confirm(f"Authorize full refund of {format_inr(remaining_refundable)} and issue Credit Note?", default=False)
        if not confirmed:
            print("\n✖ Refund aborted by staff. No money was reversed and no records were updated.\n")
            return 0

    # 6. Execute Refund via RefundService
    print("\nProcessing terminal reversal and cloud inventory reconciliation...")
    try:
        result = ctx.refund_service.process_full_refund(
            invoice_no=invoice.invoice_no,
            location_id=target_loc_id,
            reason_code=reason,
            staff_id=operator_staff,
            financial_year=invoice.financial_year,
        )
    except Exception as err:
        print(f"\n✖ REFUND FAILED: {err}\n")
        return 1

    if result.success:
        print("\n✔ FULL REFUND COMPLETED SUCCESSFULLY!")
        refund_card_items = [
            ("Credit Note Number", result.credit_note_no),
            ("Original Invoice", result.invoice_no),
            ("Refund Status", result.status.value),
            ("Amount Refunded", format_inr(result.amount_refunded_paise)),
            ("Terminal Refund ID", result.terminal_refund_id),
            ("Shopify Refund ID", result.shopify_refund_id),
            ("Frame Restock", f"RESTOCKED (+{result.frames_restocked_count} unit at location {target_loc_id})"),
            ("Lens Restock", "NO_RESTOCK (Custom ophthalmic medical device scrapped)"),
            ("Reason Code", reason),
            ("Authorizing Staff", operator_staff),
        ]
        print_card("Credit Note & Reversal Summary", refund_card_items)
        print("\nThermal credit note printed. Hand copy to customer.\n")
        return 0
    else:
        print("\n⚠ REFUND ENCOUNTERED AMBIGUOUS OUTCOME")
        print(f"  • Credit Note:    {result.credit_note_no or 'PENDING CONFIRMATION'}")
        print(f"  • Status:         {result.status.value}")
        print(f"  • Terminal ID:    {result.terminal_refund_id or 'UNCERTAIN'}")
        print(f"  • Error Details:  {result.error_message}")
        print("\nRun 'python -m eluno_pos reconcile' to resolve without double-refunding.\n")
        return 2

