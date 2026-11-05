"""Invoice inspection command for Eluno POS CLI."""

from typing import Optional

from eluno_pos.cli.context import AppContext
from eluno_pos.cli.formatting import (
    format_inr,
    print_card,
    print_header,
    print_table,
    prompt_input,
)


def run_invoice_show(ctx: AppContext, invoice_no: Optional[str] = None) -> int:
    """Inspect store tax invoice document, payment details, and line-item tax breakdowns."""
    target_inv = invoice_no
    if not target_inv:
        target_inv = prompt_input("Enter Invoice Number (e.g. ELU/HSR/FY26/00001)")

    target_inv = target_inv.strip().upper()
    if not target_inv:
        print("✖ Error: Invoice number is required.")
        return 1

    invoice = ctx.repo.get_invoice(target_inv)
    if not invoice:
        print(f"\n✖ Error: Invoice {target_inv} not found in database.\n")
        return 1

    print_header("Store Tax Invoice Inspection", f"Official Legal Sales Record: {invoice.invoice_no}")

    # 1. Invoice Meta Card
    inv_meta = [
        ("Invoice Number", invoice.invoice_no),
        ("Store Code", invoice.store_code),
        ("Financial Year", invoice.financial_year),
        ("Document Status", invoice.status.value),
        ("Date Issued", invoice.created_at[:19].replace("T", " ")),
        ("Customer ID", invoice.customer_id or "Guest Walk-in"),
        ("Terminal Charge ID", invoice.terminal_charge_id or "Uncaptured / Pending"),
        ("Shopify Order ID", invoice.shopify_order_id or "Unsynced / Pending"),
    ]
    print_card("Tax Invoice Metadata", inv_meta)

    # 2. Line Items Table
    print("\n--- Itemized Invoice Lines ---")
    headers = ["SKU / Title", "Type", "HSN", "Qty", "Gross", "Discount", "Net", "Taxable", "CGST", "SGST", "Total GST", "Pair / Rx"]
    rows = []
    for line in invoice.lines:
        pair_rx = f"Pair: {line.pair_ref or '-'}"
        if line.rx_id:
            pair_rx += f" (Rx: {line.rx_id})"
        wearer_tag = ""
        if getattr(line, "wearer_name", None) and getattr(line, "for_customer_id", None):
            wearer_tag = f" [Wearer: {line.wearer_name} (#{line.for_customer_id})]"
        elif getattr(line, "for_customer_id", None):
            wearer_tag = f" [Wearer #{line.for_customer_id}]"
        elif getattr(line, "wearer_name", None):
            wearer_tag = f" [Wearer: {line.wearer_name}]"
        if wearer_tag:
            pair_rx += wearer_tag

        rows.append([
            f"{line.sku}\n{line.title}",
            line.line_type,
            line.hsn,
            str(line.quantity),
            format_inr(line.gross_paise),
            format_inr(line.discount_paise),
            format_inr(line.net_paise),
            format_inr(line.taxable_paise),
            format_inr(line.cgst_paise),
            format_inr(line.sgst_paise),
            format_inr(line.tax_paise),
            pair_rx,
        ])
    print_table(headers, rows)

    # 3. Financial Totals Card
    tot_cgst = sum(l.cgst_paise for l in invoice.lines)
    tot_sgst = sum(l.sgst_paise for l in invoice.lines)
    totals_info = [
        ("Gross Revenue", format_inr(invoice.gross_paise)),
        ("Total Discounts Given", f"-{format_inr(invoice.discount_paise)}"),
        ("Taxable Base Amount", format_inr(invoice.taxable_paise)),
        ("CGST (Central Tax)", format_inr(tot_cgst)),
        ("SGST (State Tax)", format_inr(tot_sgst)),
        ("Total GST (Exact Paisa)", format_inr(invoice.tax_paise)),
        ("FINAL INVOICE TOTAL", format_inr(invoice.net_paise)),
    ]
    print_card("Invoice Financial & Tax Breakdown", totals_info)

    # 4. Check for Associated Credit Notes / Refunds
    refunds = ctx.repo.get_refunds_for_invoice(invoice.invoice_no)
    if refunds:
        print("\n--- Associated Credit Notes & Reversals ---")
        rf_headers = ["Credit Note No", "Refund ID", "Status", "Amount", "Terminal Refund ID", "Shopify Refund ID", "Date"]
        rf_rows = []
        for r in refunds:
            rf_rows.append([
                r.credit_note_no or "PENDING",
                r.refund_id,
                r.status.value,
                f"-{format_inr(r.amount_paise)}",
                r.terminal_refund_id or "-",
                str(r.shopify_refund_id or "-"),
                r.created_at[:10],
            ])
        print_table(rf_headers, rf_rows)

    print()
    return 0

