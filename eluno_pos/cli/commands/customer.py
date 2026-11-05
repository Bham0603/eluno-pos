"""Customer lookup command for Eluno POS."""

from typing import Optional

from eluno_pos.cli.context import AppContext
from eluno_pos.cli.formatting import print_card, print_header, print_table, prompt_input


def run_customer_lookup(ctx: AppContext, phone: Optional[str] = None) -> int:
    """Find customer details and active optical prescriptions by phone number."""
    target_phone = phone
    if not target_phone:
        target_phone = prompt_input("Enter customer phone number (e.g. +919000000001)")

    target_phone = target_phone.strip()
    if not target_phone:
        print("Error: Phone number is required.")
        return 1

    print_header("Customer & Optical Prescription Lookup", f"Querying customer records for phone: {target_phone}")

    customer = ctx.customer_service.find_by_phone(target_phone)
    if not customer:
        print(f"\n✖ No customer found registered with phone: {target_phone}\n")
        return 1

    cust_summary = [
        ("Customer ID", customer.id),
        ("Name", f"{customer.first_name} {customer.last_name or ''}".strip()),
        ("Phone", customer.phone),
        ("Email", customer.email or "N/A"),
        ("Home Store", customer.home_store or "N/A"),
        ("Customer Tags", customer.tags or "(none)"),
        ("Total Orders", customer.orders_count),
    ]
    print_card("Customer Profile", cust_summary)

    # Optical Prescriptions
    print("\nRetrieving clinical prescriptions from SMT...")
    rxs = ctx.prescription_service.get_prescriptions_by_phone(customer.phone)
    if not rxs:
        print("  • No optical prescriptions on file for this customer.")
        return 0

    print(f"  • Found {len(rxs)} prescription(s) on record:\n")
    headers = ["Rx ID", "Test Date", "Clinic / Optom", "OD (Right Eye)", "OS (Left Eye)", "PD"]
    rows = []
    for rx in rxs:
        od_str = f"SPH: {rx.od.sph:+.2f}, CYL: {rx.od.cyl:+.2f}, AX: {rx.od.axis or '-'}"
        if rx.od.add:
            od_str += f", ADD: {rx.od.add:+.2f}"

        os_str = f"SPH: {rx.os.sph:+.2f}, CYL: {rx.os.cyl:+.2f}, AX: {rx.os.axis or '-'}"
        if rx.os.add:
            os_str += f", ADD: {rx.os.add:+.2f}"

        optom_source = f"{rx.optometrist_id or 'SMT'} ({rx.source})"
        pd_val = f"{rx.pd.binocular} mm" if rx.pd.binocular else "N/A"
        rows.append([rx.rx_id, rx.date, optom_source, od_str, os_str, pd_val])

    print_table(headers, rows)
    print()
    return 0
