"""Day-end financial and tax report command for Eluno POS CLI."""

from datetime import date
from typing import Optional

from eluno_pos.cli.context import AppContext
from eluno_pos.cli.formatting import (
    format_inr,
    print_card,
    print_header,
    print_table,
    prompt_input,
)


def run_report(
    ctx: AppContext,
    store_code: Optional[str] = None,
    business_date: Optional[str] = None,
    location_id: Optional[int] = None,
    reconcile_shopify: bool = True,
) -> int:
    """Generate consolidated day-end sales, refund, and GST reconciliation report."""
    print_header("Eluno POS — Day-End Store Financial Report", "Multi-System Sales, Tax & Refund Aggregation")

    target_store = store_code
    if not target_store:
        target_store = prompt_input("Enter Store Code (e.g. HSR, MM, BR)", default="HSR").upper()

    target_date = business_date
    if not target_date:
        today_iso = date.today().isoformat()
        target_date = prompt_input("Enter Business Date (YYYY-MM-DD)", default=today_iso)

    target_loc_id = location_id
    if not target_loc_id:
        locations = ctx.shopify_client.get_locations()
        matching_loc = next((l for l in locations if (l.get("eluno", {}).get("store_code") or l.get("store_code")) == target_store), None)
        if matching_loc:
            target_loc_id = int(matching_loc["id"])
        else:
            print(f"✖ Error: Store code '{target_store}' not found in locations registry.")
            return 1

    print(f"\nGenerating Day-End Report for Store '{target_store}' on {target_date} (Asia/Kolkata)...")
    try:
        report = ctx.day_end_service.generate_report(
            store_code=target_store,
            location_id=target_loc_id,
            business_date=target_date,
            reconcile_with_shopify=reconcile_shopify,
        )
    except Exception as err:
        print(f"✖ Error generating day-end report: {err}")
        return 1

    # 1. Financial Revenue & Net Figures Card
    fin_summary = [
        ("Store Code / Loc ID", f"{report.store_code} / {report.location_id}"),
        ("Business Date", f"{report.business_date} ({report.timezone})"),
        ("Gross Sales Bills", f"{report.posted_sales_count} bills"),
        ("Gross Sales Revenue", format_inr(report.gross_sales_paise)),
        ("Total Discounts Given", f"-{format_inr(report.total_discounts_paise)}"),
        ("Net Sales (Gross - Disc)", format_inr(report.net_sales_paise)),
        ("Completed Refunds", f"{report.refunds_count} credit note(s)"),
        ("Total Refund Amount", f"-{format_inr(report.refunds_amount_paise)}"),
        ("NET STORE REVENUE", format_inr(report.net_sales_after_refunds_paise)),
        ("Sales Taxable Value", format_inr(report.sales_taxable_value_paise)),
        ("Refund Taxable Reversal", format_inr(report.refund_taxable_value_paise)),
        ("NET TAXABLE VALUE", format_inr(report.net_taxable_value_paise)),
        ("Net CGST (Central)", format_inr(report.net_cgst_paise)),
        ("Net SGST (State)", format_inr(report.net_sgst_paise)),
        ("NET TOTAL GST PAYABLE", format_inr(report.net_total_gst_paise)),
    ]
    print_card("Day-End Financial & Tax Summary", fin_summary)

    # 2. Payment Method Split
    print("\n--- Payment Method Breakdown ---")
    pm_headers = ["Payment Gateway / Method", "Amount Collected"]
    pm_rows = []
    for method, amt in sorted(report.payment_method_totals.items()):
        pm_rows.append([method.upper(), format_inr(amt)])
    if not pm_rows:
        pm_rows.append(["None", format_inr(0)])
    print_table(pm_headers, pm_rows)

    # 3. Channel Breakdown
    print("\n--- Sales Channel Breakdown ---")
    ch_headers = ["Sales Channel", "Amount"]
    ch_rows = [[ch, format_inr(amt)] for ch, amt in sorted(report.channel_totals.items())]
    if not ch_rows:
        ch_rows.append(["None", format_inr(0)])
    print_table(ch_headers, ch_rows)

    # 4. HSN GST Breakdown Table
    print("\n--- GST Tax Breakdown by HSN Code ---")
    hsn_headers = [
        "HSN Code",
        "Gross Taxable",
        "Gross GST",
        "Refund Taxable",
        "Refund GST",
        "Net Taxable",
        "Net GST",
    ]
    hsn_rows = []
    for hsn_code, stat in sorted(report.hsn_totals.items()):
        hsn_rows.append([
            hsn_code,
            format_inr(stat.sales_taxable_paise),
            format_inr(stat.sales_total_gst_paise),
            format_inr(stat.refund_taxable_paise),
            format_inr(stat.refund_total_gst_paise),
            format_inr(stat.net_taxable_paise),
            format_inr(stat.net_total_gst_paise),
        ])
    if not hsn_rows:
        hsn_rows.append(["(No data)", format_inr(0), format_inr(0), format_inr(0), format_inr(0), format_inr(0), format_inr(0)])
    print_table(hsn_headers, hsn_rows)

    # 5. Operational Audit Metrics
    if not report.is_reconciled:
        if any("reconciliation skipped" in d.lower() for d in report.discrepancies):
            sync_label = "SKIPPED (Local SQLite ledger only)"
        else:
            sync_label = "DISCREPANCIES FLAGGED"
    else:
        sync_label = "RECONCILED (100% matched)"

    op_items = [
        ("Cancelled Invoices", f"{report.cancelled_invoices_count} (Pre-payment abandoned)"),
        ("Pending Reconciliations", f"{report.pending_reconciliation_count} items"),
        ("Shopify Cloud Sync", sync_label),
    ]
    print_card("Operational Audit & Cloud Synchronization", op_items)

    if not report.is_reconciled and report.discrepancies:
        if any("reconciliation skipped" in d.lower() for d in report.discrepancies):
            print("\nℹ Reconciliation Notice:")
            for disc in report.discrepancies:
                print(f"  • {disc}")
            print()
        else:
            print("\n⚠ Cloud Discrepancy Alerts:")
            for disc in report.discrepancies:
                print(f"  • {disc}")
            print()

    print("✔ Day-end report successfully generated.\n")
    return 0

