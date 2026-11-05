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
from eluno_pos.domain.invoice import InvoiceStatus, get_financial_year
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
    for_customer_id: Optional[int] = None,
    manager_override_bps: Optional[int] = None,
    override_reason: Optional[str] = None,
    override_manager_id: Optional[str] = None,
    payment_method: str = "upi",
    auto_confirm: bool = False,
    transaction_date: Optional[date] = None,
) -> int:
    """Execute guided 15-step retail checkout workflow."""
    print_header("Eluno POS — Retail Optical Checkout", "Guided 15-Step Prescription & Pairing Flow")

    # =========================================================================
    # Step 1: Select Store / Location (Retail Location Filtering)
    # =========================================================================
    print("\n--- Step 1: Store & Location Selection ---")
    locations = ctx.shopify_client.get_locations()
    if not locations:
        print("✖ Error: No store locations found in catalogue.")
        return 1

    def _extract_store_code(loc_dict: Dict[str, Any]) -> Optional[str]:
        return loc_dict.get("eluno", {}).get("store_code") or loc_dict.get("store_code")

    # In accordance with assignment schema, only locations with active=true and eluno.kind="store"
    # may be selected for retail sales checkout. HQ (kind="warehouse") and inactive stores are strictly excluded.
    retail_locations = [
        loc for loc in locations
        if loc.get("active") is True and loc.get("eluno", {}).get("kind") == "store"
    ]
    valid_store_codes = [_extract_store_code(l) for l in retail_locations if _extract_store_code(l)]

    selected_store = store_code
    if not selected_store:
        if auto_confirm:
            print("✖ Error: Missing required argument '--store' for non-interactive checkout.")
            return 1
        else:
            loc_options = [f"{loc['name']} ({_extract_store_code(loc) or loc['name']})" for loc in retail_locations]
            print(f"Available retail stores: {', '.join(loc_options)}")
            selected_store = prompt_input("Select Store Code", default=valid_store_codes[0] if valid_store_codes else "HSR").upper()

    if not selected_store:
        print("✖ Error: Store selection is required.")
        return 1

    loc = next((l for l in retail_locations if _extract_store_code(l) == selected_store or l.get("name", "").upper() == selected_store.upper() or str(l.get("id")) == str(selected_store)), None)
    if not loc:
        # Check if location exists in general catalogue but is non-retail or inactive
        any_loc = next((l for l in locations if _extract_store_code(l) == selected_store or l.get("name", "").upper() == selected_store.upper() or str(l.get("id")) == str(selected_store)), None)
        if any_loc:
            if not any_loc.get("active"):
                print(f"✖ Error: Location '{selected_store}' is inactive and cannot be selected for checkout.")
                return 1
            kind = any_loc.get("eluno", {}).get("kind", "non-store")
            print(f"✖ Error: Location '{selected_store}' is a {kind}, not an active retail store. Valid retail stores: {', '.join(valid_store_codes)}")
            return 1
        else:
            print(f"✖ Error: Invalid store code '{selected_store}'. Valid store codes: {', '.join(valid_store_codes)}")
            return 1

    location_id = int(loc["id"])
    store_code_val = _extract_store_code(loc) or loc.get("store_code") or str(loc["id"])
    print(f"✔ Active Store: {loc['name']} [Code: {store_code_val}, Location ID: {location_id}]")

    # =========================================================================
    # Step 2: Identify Staff Member (Strict Staff Validation)
    # =========================================================================
    print("\n--- Step 2: Staff Identification ---")
    staff_list = ctx.smt_client.get_staff(store_code=store_code_val)

    selected_staff_id = staff_id
    if not selected_staff_id:
        if auto_confirm:
            print("✖ Error: Missing required argument '--staff' for non-interactive checkout.")
            return 1
        else:
            if staff_list:
                print("Staff on duty:")
                for s in staff_list[:5]:
                    print(f"  • {s['staff_id']} — {s['name']} ({s['role']})")
                selected_staff_id = prompt_input("Enter Operating Staff ID")
            else:
                selected_staff_id = prompt_input("Enter Operating Staff ID")

    if not selected_staff_id:
        print("✖ Error: Operating staff ID is required.")
        return 1

    staff_member = next((s for s in staff_list if s["staff_id"] == selected_staff_id), None)
    if not staff_member:
        # Check if staff exists in general registry but belongs to a different store
        all_staff = ctx.smt_client.get_staff()
        other_store_staff = next((s for s in all_staff if s["staff_id"] == selected_staff_id), None)
        if other_store_staff:
            print(f"✖ Error: Staff '{selected_staff_id}' is registered at store '{other_store_staff.get('store_code')}', not active store '{store_code_val}'.")
        else:
            print(f"✖ Error: Staff member '{selected_staff_id}' not found in staff registry.")
        return 1

    if staff_member.get("is_active") is False:
        print(f"✖ Error: Staff member '{selected_staff_id}' is marked inactive.")
        return 1

    print(f"✔ Logged-in Staff: {staff_member['name']} ({staff_member['role']}) [ID: {staff_member['staff_id']}]")

    # =========================================================================
    # Step 3: Look Up Customer by Phone
    # =========================================================================
    print("\n--- Step 3: Customer Lookup ---")
    c_phone = customer_phone
    if not c_phone:
        if auto_confirm:
            print("✖ Error: Missing required argument '--customer' for non-interactive checkout.")
            return 1
        else:
            c_phone = prompt_input("Enter Customer Phone")

    if not c_phone:
        print("✖ Error: Customer phone number is required.")
        return 1

    customer = ctx.customer_service.find_by_phone(c_phone)
    if not customer:
        print(f"✖ Customer with phone {c_phone} not found.")
        return 1

    print(f"✔ Customer Identified: {customer.first_name} {customer.last_name or ''} [ID: {customer.id}, Tags: {customer.tags or 'None'}]")

    # =========================================================================
    # Step 4: Retrieve Relevant Prescriptions (Privacy Minimization & Strict Rx ID Validation)
    # =========================================================================
    print("\n--- Step 4: Clinical Prescription Retrieval ---")
    prescriptions = ctx.prescription_service.get_prescriptions_by_phone(customer.phone)
    selected_rx = None
    tx_date = transaction_date or date.today()

    is_explicit_plano = bool(rx_id and rx_id.strip().lower() in ("plano", "none", "0"))

    if is_explicit_plano:
        selected_rx = None
        print("✔ Explicit plano / non-corrective mode requested.")
    elif rx_id:
        # Explicit prescription requested: MUST exist for this customer
        selected_rx = next((rx for rx in prescriptions if rx.rx_id == rx_id), None)
        if not selected_rx:
            print(f"✖ Error: Prescription '{rx_id}' does not exist for customer {customer.phone}.")
            return 1
        if not selected_rx.is_valid_on(tx_date):
            print(f"✖ Error: Prescription '{selected_rx.rx_id}' is expired or not valid on transaction date (valid until {selected_rx.valid_until}).")
            return 1
        add_present = "Yes" if (selected_rx.od.add or selected_rx.os.add) else "No"
        print(f"✔ Selected Prescription: {selected_rx.rx_id} dated {selected_rx.date} (Source: {selected_rx.optometrist_id or 'SMT'}, Status: Valid, ADD: {add_present})")
    elif prescriptions:
        valid_rxs = [rx for rx in prescriptions if rx.is_valid_on(tx_date)]
        print(f"Found {len(prescriptions)} prescription(s) on record:")
        for i, rx in enumerate(prescriptions):
            add_present = "Yes" if (rx.od.add or rx.os.add) else "No"
            valid_str = "Valid" if rx.is_valid_on(tx_date) else f"Expired ({rx.valid_until})"
            print(f"  [{i + 1}] {rx.rx_id} (Date: {rx.date}, Source: {rx.optometrist_id or 'SMT'}, Status: {valid_str}, ADD: {add_present})")

        if not valid_rxs:
            # All prescriptions on file are expired
            if auto_confirm:
                print("✖ Error: All prescriptions on file are expired. In non-interactive mode, plano mode cannot be assumed silently; provide '--rx plano' to explicitly authorize plano.")
                return 1
            else:
                allow_plano = prompt_confirm("All prescriptions on file are expired. Do you want to proceed in plano / non-corrective mode?", default=False)
                if not allow_plano:
                    print("✖ Error: Cannot proceed with expired prescription. Optical consultation required.")
                    return 1
                selected_rx = None
        else:
            if auto_confirm:
                selected_rx = valid_rxs[0]
            else:
                rx_choice = prompt_input("Select Prescription [number or ID, or 'plano']", default="1")
                if rx_choice.lower() in ("plano", "none", "0"):
                    selected_rx = None
                elif rx_choice.isdigit() and 1 <= int(rx_choice) <= len(prescriptions):
                    candidate_rx = prescriptions[int(rx_choice) - 1]
                    if not candidate_rx.is_valid_on(tx_date):
                        print(f"✖ Error: Selected prescription {candidate_rx.rx_id} is expired (valid until {candidate_rx.valid_until}).")
                        return 1
                    selected_rx = candidate_rx
                else:
                    candidate_rx = next((rx for rx in prescriptions if rx.rx_id == rx_choice), None)
                    if not candidate_rx:
                        print(f"✖ Error: Prescription '{rx_choice}' not found for customer.")
                        return 1
                    if not candidate_rx.is_valid_on(tx_date):
                        print(f"✖ Error: Selected prescription {candidate_rx.rx_id} is expired (valid until {candidate_rx.valid_until}).")
                        return 1
                    selected_rx = candidate_rx

        if selected_rx:
            add_present = "Yes" if (selected_rx.od.add or selected_rx.os.add) else "No"
            print(f"✔ Active Prescription: {selected_rx.rx_id} dated {selected_rx.date} (Source: {selected_rx.optometrist_id or 'SMT'}, Status: Valid, ADD: {add_present})")
        else:
            print("✔ Plano / Non-corrective mode active.")
    else:
        print("  • No clinical prescription on file. (Plano / Non-corrective mode)")

    # =========================================================================
    # Step 5: Family Billing Identification & Recipient Association
    # =========================================================================
    print("\n--- Step 5: Family Billing & Recipient Identification ---")
    member_name = for_member
    wearer_cust_id: Optional[int] = for_customer_id

    if not member_name and wearer_cust_id is None:
        if auto_confirm:
            member_name = "Self"
            wearer_cust_id = customer.id
        else:
            member_input = prompt_input("Individual recipient name or Customer ID for this pair", default="Self")
            if member_input.isdigit():
                wearer_cust_id = int(member_input)
                member_name = f"Customer #{wearer_cust_id}"
            elif member_input.lower() in ("self", "primary", customer.first_name.lower()):
                member_name = "Self"
                wearer_cust_id = customer.id
            else:
                member_name = member_input
                wearer_cust_id = None
    else:
        if wearer_cust_id is not None:
            if not member_name:
                if wearer_cust_id == customer.id:
                    member_name = "Self"
                else:
                    member_name = f"Customer #{wearer_cust_id}"
        elif member_name:
            if member_name.isdigit():
                wearer_cust_id = int(member_name)
                member_name = f"Customer #{wearer_cust_id}"
            elif member_name.lower() in ("self", "primary", customer.first_name.lower()):
                wearer_cust_id = customer.id
                member_name = "Self"
            else:
                # Unenrolled family member identified by name:
                # Do NOT map to purchasing customer's ID!
                wearer_cust_id = None

    print(f"✔ Pair Recipient: {member_name} (Persisted for_customer_id: {wearer_cust_id})")

    # =========================================================================
    # Step 6: Select Frame and Lens Variants (Strict HSN Metadata Validation)
    # =========================================================================
    print("\n--- Step 6: Catalogue Item Selection ---")
    # Frame selection
    f_vid = frame_variant_id
    if not f_vid:
        if auto_confirm:
            print("✖ Error: Missing required argument '--frame' for non-interactive checkout.")
            return 1
        else:
            print("Popular Frames:")
            print("  • 91000001 — Eluno Atelier Cat-eye 01 (₹12,998.00)")
            print("  • 91000014 — Eluno Core Aviator 14 (₹7,999.00)")
            print("  • 91000027 — Eluno Lite Rectangle 27 (₹4,999.00)")
            f_choice = prompt_input("Enter Frame Variant ID", default="91000001")
            try:
                f_vid = int(f_choice)
            except ValueError:
                print(f"✖ Error: Invalid frame variant ID '{f_choice}'.")
                return 1

    frame_var = ctx.shopify_client.get_variant(f_vid)
    if not frame_var:
        print(f"✖ Frame variant {f_vid} not found in catalogue.")
        return 1

    f_meta = frame_var.get("eluno") or {}
    f_hsn = f_meta.get("hsn")
    if not f_hsn:
        print(f"✖ Error: Frame variant {f_vid} is missing mandatory HSN code in 'variant.eluno.hsn'.")
        return 1

    f_price_paise = parse_to_paise(frame_var["price"])
    print(f"✔ Selected Frame: {frame_var['title']} (SKU: {frame_var['sku']}) — {format_inr(f_price_paise)} [HSN: {f_hsn}]")

    # Lens selection
    l_vid = lens_variant_id
    if not l_vid:
        if auto_confirm:
            print("✖ Error: Missing required argument '--lens' for non-interactive checkout.")
            return 1
        else:
            print("\nAvailable Lenses:")
            print("  • 91000041 — Single Vision 1.50 Standard (₹999.00)")
            print("  • 91000043 — Single Vision 1.60 Blue-cut (₹3,499.00)")
            print("  • 91000049 — Progressive 1.50 Standard (₹6,499.00)")
            print("  • 91000050 — Progressive 1.56 Blue-cut (₹9,499.00)")
            l_choice = prompt_input("Enter Lens Variant ID", default="91000050" if "campaign" in (customer.tags or "") else "91000041")
            try:
                l_vid = int(l_choice)
            except ValueError:
                print(f"✖ Error: Invalid lens variant ID '{l_choice}'.")
                return 1

    lens_var = ctx.shopify_client.get_variant(l_vid)
    if not lens_var:
        print(f"✖ Lens variant {l_vid} not found in catalogue.")
        return 1

    l_meta = lens_var.get("eluno") or {}
    l_hsn = l_meta.get("hsn")
    if not l_hsn:
        print(f"✖ Error: Lens variant {l_vid} is missing mandatory HSN code in 'variant.eluno.hsn'.")
        return 1

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
    # Step 8: Build Cart & Associate Pairs (pair_ref + for_customer_id)
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
        for_customer_id=wearer_cust_id,
        wearer_name=member_name,
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
        for_customer_id=wearer_cust_id,
        wearer_name=member_name,
    )
    cart = Cart(lines=[frame_line, lens_line], customer=customer)
    print(f"✔ Frame and Lens successfully linked under pair reference: '{pair_id}' for recipient '{member_name}'.")

    # =========================================================================
    # Step 9: Staff Manager Discount Override (Strict Authorization Check)
    # =========================================================================
    override = None
    if manager_override_bps is not None and manager_override_bps > 0:
        print("\n--- Step 9: Manager Discretionary Discount Override ---")
        if manager_override_bps > 1000:
            print(f"✖ Error: Discretionary manager discount cannot exceed 10% (got {manager_override_bps / 100:.2f}%).")
            return 1

        approver_id = override_manager_id or staff_member["staff_id"]
        reason = override_reason or "CUSTOMER_RETENTION"

        # Check permissions: operating staff or authorizing manager associated with this store
        approver = next((s for s in staff_list if s["staff_id"] == approver_id and s.get("can_override_discount")), None)
        if not approver:
            all_staff = ctx.smt_client.get_staff()
            match_anywhere = next((s for s in all_staff if s["staff_id"] == approver_id), None)
            if not match_anywhere:
                print(f"✖ Error: Authorizing staff ID '{approver_id}' not found.")
            elif not match_anywhere.get("can_override_discount"):
                print(f"✖ Error: Staff member '{approver_id}' ({match_anywhere.get('role')}) lacks 'store_manager' override privilege.")
            else:
                print(f"✖ Error: Authorizing manager '{approver_id}' belongs to store '{match_anywhere.get('store_code')}', not active store '{store_code_val}'.")
            return 1

        override = ManagerOverride(
            staff_id=approver["staff_id"],
            role=approver["role"],
            reason_code=reason,
            discount_percent_bps=manager_override_bps,
        )
        cart.manager_override = override
        print(f"✔ Manager Override Approved: {manager_override_bps / 100:.2f}% authorized by {approver['staff_id']} [Reason: {reason}].")

    # =========================================================================
    # Step 10 & 11: Evaluate Offers, Compute GST & Prepare Session (Dynamic Financial Year)
    # =========================================================================
    print("\n--- Steps 10 & 11: Promotional Offers & GST Calculation ---")
    fin_year = get_financial_year(tx_date)

    session = ctx.checkout_service.prepare_checkout(
        cart=cart,
        store_code=store_code_val,
        location_id=location_id,
        financial_year=fin_year,
        channel="walk_in",
        staff_id=staff_member["staff_id"],
        transaction_date=tx_date,
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
        confirmed = prompt_confirm(f"Capture payment of {format_inr(session.cart.net_total_paise)} on UPI/Card terminal?", default=False)
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
            ("Recipient", member_name or "Self"),
            ("Persisted for_customer_id", str(wearer_cust_id) if wearer_cust_id is not None else "None (Unenrolled)"),
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
