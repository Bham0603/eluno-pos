"""Guided checkout command orchestration for Eluno POS CLI."""

from datetime import date
from typing import Any, Dict, List, Optional
import sys

from eluno_pos.adapters.exceptions import ValidationError
from eluno_pos.cli.context import AppContext
from eluno_pos.cli.formatting import (
    format_inr,
    print_card,
    print_header,
    print_table,
    prompt_confirm,
    prompt_input,
)
from eluno_pos.domain.cart import Cart, CartLine, ManagerOverride
from eluno_pos.domain.invoice import InvoiceStatus
from eluno_pos.domain.lens import LensVariant
from eluno_pos.domain.money import parse_to_paise
from eluno_pos.domain.validation import LensValidator


def run_checkout(
    ctx: AppContext,
    store_code: Optional[str] = None,
    staff_id: Optional[str] = None,
    customer_phone: Optional[str] = None,
    frame_variant_id: Optional[int] = None,
    lens_variant_id: Optional[int] = None,
    rx_id: Optional[str] = None,
    for_member: Optional[str] = None,
    manager_override_bps: Optional[int] = None,
    override_reason: Optional[str] = None,
    override_manager_id: Optional[str] = None,
    payment_method: str = "upi",
    auto_confirm: bool = False,
) -> int:
    """Execute guided 15-step retail checkout workflow."""
    print_header("Eluno POS — Retail Optical Checkout", "Guided 15-Step Prescription & Pairing Flow")

    # =========================================================================
    # Step 1: Select Store / Location
    # =========================================================================
    print("\n--- Step 1: Store & Location Selection ---")
    locations = ctx.shopify_client.get_locations()
    if not locations:
        print("✖ Error: No store locations found.")
        return 1

    selected_store = store_code
    if not selected_store:
        if auto_confirm:
            selected_store = "HSR"
        else:
            loc_options = [f"{loc['name']} ({loc.get('store_code', loc['name'])})" for loc in locations]
            print(f"Available stores: {', '.join(loc_options)}")
            selected_store = prompt_input("Select Store Code", default=locations[0].get("store_code", "HSR")).upper()

    loc = next((l for l in locations if l.get("store_code") == selected_store or l["name"] == selected_store), locations[0])
    location_id = int(loc["id"])
    store_code_val = loc.get("store_code", selected_store)
    print(f"✔ Active Store: {loc['name']} [Code: {store_code_val}, Location ID: {location_id}]")

    # =========================================================================
    # Step 2: Identify Staff Member
    # =========================================================================
    print("\n--- Step 2: Staff Identification ---")
    staff_list = ctx.smt_client.get_staff(store_code=store_code_val)
    if not staff_list:
        staff_list = ctx.smt_client.get_staff()

    selected_staff_id = staff_id
    if not selected_staff_id:
        if auto_confirm:
            selected_staff_id = staff_list[0]["staff_id"]
        else:
            print("Staff on duty:")
            for s in staff_list[:5]:
                print(f"  • {s['staff_id']} — {s['name']} ({s['role']})")
            selected_staff_id = prompt_input("Enter Operating Staff ID", default=staff_list[0]["staff_id"])

    staff_member = next((s for s in staff_list if s["staff_id"] == selected_staff_id), None)
    if not staff_member:
        staff_member = {"staff_id": selected_staff_id, "name": selected_staff_id, "role": "sales", "can_override_discount": False}
    print(f"✔ Logged-in Staff: {staff_member['name']} ({staff_member['role']}) [ID: {staff_member['staff_id']}]")

    # =========================================================================
    # Step 3: Look Up Customer by Phone
    # =========================================================================
    print("\n--- Step 3: Customer Lookup ---")
    c_phone = customer_phone
    if not c_phone:
        if auto_confirm:
            c_phone = "+919000000001"
        else:
            c_phone = prompt_input("Enter Customer Phone", default="+919000000001")

    customer = ctx.customer_service.find_by_phone(c_phone)
    if not customer:
        print(f"✖ Customer with phone {c_phone} not found.")
        return 1

    print(f"✔ Customer Identified: {customer.first_name} {customer.last_name or ''} [ID: {customer.id}, Tags: {customer.tags or 'None'}]")

    # =========================================================================
    # Step 4: Retrieve Relevant Prescriptions
    # =========================================================================
    print("\n--- Step 4: Clinical Prescription Retrieval ---")
    prescriptions = ctx.prescription_service.get_prescriptions_by_phone(customer.phone)
    selected_rx = None
    if prescriptions:
        print(f"Found {len(prescriptions)} prescription(s):")
        for i, rx in enumerate(prescriptions):
            print(f"  [{i + 1}] {rx.rx_id} (Date: {rx.date}) — OD: SPH {rx.od.sph:+.2f}, CYL {rx.od.cyl:+.2f} | OS: SPH {rx.os.sph:+.2f}, CYL {rx.os.cyl:+.2f}")

        if rx_id:
            selected_rx = next((rx for rx in prescriptions if rx.rx_id == rx_id), prescriptions[0])
        elif auto_confirm:
            selected_rx = prescriptions[0]
        else:
            rx_choice = prompt_input("Select Prescription [number or ID]", default="1")
            if rx_choice.isdigit() and 1 <= int(rx_choice) <= len(prescriptions):
                selected_rx = prescriptions[int(rx_choice) - 1]
            else:
                selected_rx = next((rx for rx in prescriptions if rx.rx_id == rx_choice), prescriptions[0])
        print(f"✔ Selected Prescription: {selected_rx.rx_id} dated {selected_rx.date}")
    else:
        print("  • No clinical prescription on file. (Plano / Non-corrective mode)")

    # =========================================================================
    # Step 5: Family Billing Identification
    # =========================================================================
    print("\n--- Step 5: Family Billing & Recipient Identification ---")
    member_name = for_member
    if not member_name:
        is_family = "family" in (customer.tags or "").lower()
        default_member = "Self" if not is_family else "Self"
        if auto_confirm:
            member_name = default_member
        else:
            member_name = prompt_input("Individual recipient name for this pair", default=default_member)
    print(f"✔ Pair Recipient: {member_name}")

    # =========================================================================
    # Step 6: Select Frame and Lens Variants
    # =========================================================================
    print("\n--- Step 6: Catalogue Item Selection ---")
    # Frame selection
    f_vid = frame_variant_id
    if not f_vid:
        if auto_confirm:
            f_vid = 91000001
        else:
            print("Popular Frames:")
            print("  • 91000001 — Eluno Atelier Cat-eye 01 (₹12,998.00)")
            print("  • 91000014 — Eluno Core Aviator 14 (₹7,999.00)")
            print("  • 91000027 — Eluno Lite Rectangle 27 (₹4,999.00)")
            f_choice = prompt_input("Enter Frame Variant ID", default="91000001")
            f_vid = int(f_choice)

    frame_var = ctx.shopify_client.get_variant(f_vid)
    if not frame_var:
        print(f"✖ Frame variant {f_vid} not found in catalogue.")
        return 1
    f_meta = frame_var.get("eluno", {})
    f_hsn = f_meta.get("hsn", "90031900")
    f_price_paise = parse_to_paise(frame_var["price"])
    print(f"✔ Selected Frame: {frame_var['title']} (SKU: {frame_var['sku']}) — {format_inr(f_price_paise)} [HSN: {f_hsn}]")

    # Lens selection
    l_vid = lens_variant_id
    if not l_vid:
        if auto_confirm:
            l_vid = 91000050 if "campaign" in (customer.tags or "") else 91000041
        else:
            print("\nAvailable Lenses:")
            print("  • 91000041 — Single Vision 1.50 Standard (₹999.00)")
            print("  • 91000043 — Single Vision 1.60 Blue-cut (₹3,499.00)")
            print("  • 91000049 — Progressive 1.50 Standard (₹6,499.00)")
            print("  • 91000050 — Progressive 1.56 Blue-cut (₹9,499.00)")
            l_choice = prompt_input("Enter Lens Variant ID", default="91000050" if "campaign" in (customer.tags or "") else "91000041")
            l_vid = int(l_choice)

    lens_var = ctx.shopify_client.get_variant(l_vid)
    if not lens_var:
        print(f"✖ Lens variant {l_vid} not found in catalogue.")
        return 1
    l_meta = lens_var.get("eluno", {})
    l_hsn = l_meta.get("hsn", "90015000")
    l_price_paise = parse_to_paise(lens_var["price"])
    l_type = l_meta.get("lens_type", "single_vision")
    l_index = l_meta.get("index", 1.5)
    l_coating = l_meta.get("coating", "standard")
    print(f"✔ Selected Lens: {lens_var['title']} (SKU: {lens_var['sku']}) — {format_inr(l_price_paise)} [HSN: {l_hsn}, Type: {l_type}]")

    # =========================================================================
    # Step 7: Prescription Compatibility Validation
    # =========================================================================
    print("\n--- Step 7: Prescription Compatibility Validation ---")
    if selected_rx:
        lens_obj = LensVariant.from_shopify_variant(lens_var)
        val_res = LensValidator.validate(selected_rx, lens_obj)

        if not val_res.is_compatible:
            print("\n✖ Optical Validation Failure:")
            for r in val_res.reasons:
                print(f"  • {r}")
            print("The selected prescription powers exceed the manufacturing limits of this lens index/material.")
            return 1

        if val_res.advisories:
            for adv in val_res.advisories:
                print(f"  ℹ Advisory: {adv.message}")
        print("✔ Lens Validation Passed: SPH, CYL, AXIS, and ADD powers are fully compatible with this lens geometry.")
    else:
        print("✔ Plano / Non-prescription line accepted.")

    # =========================================================================
    # Step 8: Build Cart & Associate Pairs (pair_ref)
    # =========================================================================
    print("\n--- Step 8: Pair Association ---")
    pair_id = "pair_1"
    frame_line = CartLine(
        line_id="line_frame_1",
        line_type="frame",
        variant_id=frame_var["id"],
        sku=frame_var["sku"],
        title=frame_var["title"],
        unit_price_paise=f_price_paise,
        quantity=1,
        hsn=f_hsn,
        pair_ref=pair_id,
        rx_id=selected_rx.rx_id if selected_rx else None,
    )
    lens_line = CartLine(
        line_id="line_lens_1",
        line_type="lens",
        variant_id=lens_var["id"],
        sku=lens_var["sku"],
        title=lens_var["title"],
        unit_price_paise=l_price_paise,
        quantity=1,
        hsn=l_hsn,
        pair_ref=pair_id,
        lens_type=l_type,
        index=float(l_index),
        coating=l_coating,
        rx_id=selected_rx.rx_id if selected_rx else None,
    )
    cart = Cart(lines=[frame_line, lens_line], customer=customer)
    print(f"✔ Frame and Lens successfully linked under pair reference: '{pair_id}' for recipient '{member_name}'.")

    # =========================================================================
    # Step 9: Staff Manager Discount Override (if authorized)
    # =========================================================================
    override = None
    if manager_override_bps is not None and manager_override_bps > 0:
        print("\n--- Step 9: Manager Discretionary Discount Override ---")
        if manager_override_bps > 1000:
            print(f"✖ Error: Discretionary manager discount cannot exceed 10% (got {manager_override_bps / 100:.2f}%).")
            return 1

        approver_id = override_manager_id or staff_member["staff_id"]
        reason = override_reason or "CUSTOMER_RETENTION"

        # Check permissions
        can_override = staff_member.get("can_override_discount", False)
        if not can_override:
            # Check authorizing manager
            auth_mgr = next((s for s in staff_list if s["staff_id"] == approver_id and s.get("can_override_discount")), None)
            if not auth_mgr:
                print(f"✖ Error: Staff member {approver_id} does not have 'store_manager' override permissions.")
                return 1
            approver_role = auth_mgr["role"]
        else:
            approver_role = staff_member["role"]

        override = ManagerOverride(
            staff_id=approver_id,
            role=approver_role,
            reason_code=reason,
            discount_percent_bps=manager_override_bps,
        )
        cart.manager_override = override
        print(f"✔ Manager Override Approved: {manager_override_bps / 100:.2f}% authorized by {approver_id} [Reason: {reason}].")

    # =========================================================================
    # Step 10 & 11: Evaluate Offers, Compute GST & Prepare Session
    # =========================================================================
    print("\n--- Steps 10 & 11: Promotional Offers & GST Calculation ---")
    session = ctx.checkout_service.prepare_checkout(
        cart=cart,
        store_code=store_code_val,
        location_id=location_id,
        financial_year="FY26",
        channel="walk_in",
        staff_id=staff_member["staff_id"],
        transaction_date=date.today(),
        prescription_map={selected_rx.rx_id: selected_rx} if selected_rx else {},
    )

    # Display complete checkout financial breakdown
    print_header("Invoice Checkout Summary", f"Store: {store_code_val} | Invoice No: {session.invoice.invoice_no}")
    
    table_headers = ["Line Item", "HSN", "Gross Price", "Discounts", "Net Price", "Taxable Value", "CGST", "SGST", "Total GST"]
    table_rows = []
    for i, line in enumerate(session.cart.lines):
        lt = session.tax_summary.line_taxes[i]
        applied_names = ", ".join(d.offer_id for d in line.discount_allocations) or "-"
        table_rows.append([
            f"{line.title} ({line.line_type})",
            line.hsn,
            format_inr(line.gross_price_paise),
            f"{format_inr(line.total_discount_paise)} ({applied_names})",
            format_inr(line.net_price_paise),
            format_inr(lt.taxable_value_paise),
            format_inr(lt.cgst_paise),
            format_inr(lt.sgst_paise),
            format_inr(lt.total_gst_paise),
        ])
    print_table(table_headers, table_rows)

    totals_summary = [
        ("Gross Amount", format_inr(session.cart.gross_total_paise)),
        ("Total Discounts", f"-{format_inr(session.cart.total_discount_paise)}"),
        ("Taxable Subtotal", format_inr(session.tax_summary.total_taxable_paise)),
        ("Total CGST", format_inr(session.tax_summary.total_cgst_paise)),
        ("Total SGST", format_inr(session.tax_summary.total_sgst_paise)),
        ("Combined GST", format_inr(session.tax_summary.total_gst_paise)),
        ("FINAL PAYABLE TOTAL", format_inr(session.cart.net_total_paise)),
    ]
    print_card("Financial Totals", totals_summary)

    # =========================================================================
    # Step 12: Explicit Staff Confirmation Before Payment Capture
    # =========================================================================
    print("\n--- Step 12: Staff Authorization & Confirmation Gate ---")
    if not auto_confirm:
        confirmed = prompt_confirm(f"Capture payment of {format_inr(session.cart.net_total_paise)} on UPI/Card terminal?", default=True)
        if not confirmed:
            print("\n✖ Payment cancelled by staff. Marking allocated invoice sequence as CANCELLED...")
            ctx.repo.update_invoice_status(session.invoice.invoice_no, InvoiceStatus.CANCELLED)
            print(f"✔ Invoice {session.invoice.invoice_no} marked CANCELLED without initiating terminal charges.")
            return 0

    # =========================================================================
    # Steps 13, 14 & 15: Payment Capture, Shopify Sync & Final Order Receipt
    # =========================================================================
    print("\n--- Steps 13–15: Terminal Payment & Shopify Order Creation ---")
    print(f"Initiating terminal transaction for {format_inr(session.cart.net_total_paise)} via {payment_method.upper()}...")

    result = ctx.checkout_service.complete_checkout(session, payment_method=payment_method)

    if result.success:
        print("\n✔ TRANSACTION COMPLETED SUCCESSFULLY!")
        receipt_items = [
            ("Store Invoice No", result.invoice_no),
            ("Invoice Status", result.status.value),
            ("Terminal Charge ID", result.terminal_charge_id),
            ("Shopify Order ID", result.shopify_order_id),
            ("Shopify Order #", result.shopify_order_number or result.shopify_order_id),
            ("Total Paid (INR)", format_inr(result.net_payable_paise)),
            ("Recipient", member_name),
            ("Operating Staff", f"{staff_member['name']} ({staff_member['staff_id']})"),
        ]
        print_card("Official Store Receipt", receipt_items)
        print("\nThermal receipt printed. Frame marked for store assembly and lab dispatch.\n")
        return 0
    elif result.is_pending_reconciliation:
        print("\n⚠ TRANSACTION REQUIRES RECONCILIATION")
        print(f"  • Terminal Charge: CAPTURED ({result.terminal_charge_id})")
        print("  • Shopify Order:   PENDING / TIMED OUT")
        print(f"  • Invoice Status:  {result.status.value}")
        print(f"  • Diagnosis:       {result.error_message}")
        print("\nRun 'python -m eluno_pos reconcile' to safely confirm cloud synchronization without re-charging.\n")
        return 2
    else:
        print("\n✖ CHECKOUT FAILED")
        print(f"  • Invoice No:     {result.invoice_no}")
        print(f"  • Status:         {result.status.value}")
        print(f"  • Error Details:  {result.error_message}\n")
        return 1
