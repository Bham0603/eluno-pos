"""Reconciliation recovery command for Eluno POS CLI."""

from typing import Optional

from eluno_pos.cli.context import AppContext
from eluno_pos.cli.formatting import (
    format_inr,
    print_card,
    print_header,
    print_table,
    prompt_confirm,
)
from eluno_pos.domain.invoice import InvoiceStatus
from eluno_pos.domain.refund import RefundStatus


def run_reconcile(
    ctx: AppContext,
    store_code: Optional[str] = None,
    execute_recovery: bool = False,
    auto_confirm: bool = False,
) -> int:
    """Inspect and safely recover pending transactions, unconfirmed charges, or incomplete refunds."""
    print_header("Eluno POS — Transaction & Reconciliation Hub", "Inspecting In-Flight and Ambiguous Operations")

    # 1. Fetch pending records from SQLite
    pending_invoices = ctx.repo.get_pending_invoices(store_code)
    pending_refunds = ctx.repo.get_pending_refunds(store_code)
    pending_intents = ctx.repo.get_pending_shopify_intents()

    total_pending = len(pending_invoices) + len(pending_refunds) + len(pending_intents)

    if total_pending == 0:
        print("\n✔ ZERO PENDING TRANSACTIONS: All invoices, payments, and refunds are 100% reconciled.\n")
        return 0

    print(f"\nFound {total_pending} pending operation(s) requiring inspection:\n")

    table_headers = ["Category", "Entity Reference", "Current Status", "Store / Loc", "Financial Impact", "Diagnosis"]
    table_rows = []

    for inv in pending_invoices:
        diag = "Payment in-flight or Shopify order unconfirmed"
        if inv.terminal_charge_id:
            diag = f"Terminal captured ({inv.terminal_charge_id}); Shopify sync pending"
        table_rows.append([
            "Invoice / Checkout",
            inv.invoice_no,
            inv.status.value,
            inv.store_code,
            format_inr(inv.net_paise),
            diag,
        ])

    for ref in pending_refunds:
        table_rows.append([
            "Refund / Reversal",
            ref.refund_id,
            ref.status.value,
            f"Inv: {ref.invoice_no}",
            f"-{format_inr(ref.amount_paise)}",
            ref.error_message or "Awaiting terminal confirmation or credit note allocation",
        ])

    for intent in pending_intents:
        table_rows.append([
            "Shopify Sync",
            intent["invoice_no"],
            intent["status"],
            f"ID: {intent['id']}",
            "-",
            intent.get("error_message") or "Cloud order creation intent pending",
        ])

    print_table(table_headers, table_rows)

    # 2. Safety Gate: Allow only proved-safe recovery operations
    print("\n--- Safe Recovery Plan ---")
    print("1. Queries payment terminal gateway for charge status (Never creates duplicate charges).")
    print("2. Reconciles existing Shopify order/refund by idempotency key (Never creates duplicate orders/refunds).")
    print("3. Allocates credit note only upon confirmed terminal capture.")
    print("4. Unresolved/uncertain items remain PENDING for manual supervisor inspection.")

    should_run = execute_recovery
    if not should_run and not auto_confirm:
        should_run = prompt_confirm("Execute safe automated reconciliation for these pending items?", default=True)

    if not should_run:
        print("\nReconciliation inspection complete. No recovery actions were executed.\n")
        return 0

    # 3. Execute Safe Recovery
    print("\nExecuting recovery routines...")
    reconciled_count = 0
    unresolved_count = 0

    # Recover pending checkouts
    for inv in pending_invoices:
        print(f"\n[Reconciling Invoice: {inv.invoice_no}]")
        try:
            res = ctx.checkout_service.resume_checkout_session(inv.invoice_no)
            if res.success:
                print(f"  ✔ Conclusively Reconciled! Status: {res.status.value}, Shopify Order ID: {res.shopify_order_id}")
                reconciled_count += 1
            elif res.is_pending_reconciliation:
                print(f"  ⚠ Still Inconclusive: {res.error_message}. Kept as PENDING_RECONCILIATION.")
                unresolved_count += 1
            else:
                print(f"  ✖ Payment Declined/Cancelled: Status {res.status.value}")
        except Exception as err:
            print(f"  ✖ Recovery attempt failed: {err}")
            unresolved_count += 1

    # Recover pending refunds
    locations = ctx.shopify_client.get_locations()
    default_loc = int(locations[0]["id"]) if locations else 70000002
    for ref in pending_refunds:
        print(f"\n[Reconciling Refund: {ref.refund_id} for Invoice: {ref.invoice_no}]")
        try:
            ref_res = ctx.refund_service.resume_or_reconcile_refund(ref.refund_id, location_id=default_loc)
            if ref_res and ref_res.success:
                print(f"  ✔ Refund Conclusively Reconciled! Credit Note: {ref_res.credit_note_no}, Shopify Refund: {ref_res.shopify_refund_id}")
                reconciled_count += 1
            else:
                status_msg = ref_res.status.value if ref_res else "INCONCLUSIVE"
                err_msg = ref_res.error_message if ref_res else "Awaiting gateway reflection"
                print(f"  ⚠ Still Pending [{status_msg}]: {err_msg}. Kept pending to prevent duplicate refund.")
                unresolved_count += 1
        except Exception as err:
            print(f"  ✖ Refund recovery attempt failed: {err}")
            unresolved_count += 1

    summary_items = [
        ("Total Operations Checked", total_pending),
        ("Successfully Reconciled", reconciled_count),
        ("Still Unresolved (Safe)", unresolved_count),
    ]
    print_card("Reconciliation Execution Result", summary_items)

    if unresolved_count > 0:
        print("\n⚠ Some operations remain unresolved. Do NOT attempt manual external payment overrides until verified.\n")
        return 2

    print("\n✔ All operations successfully reconciled!\n")
    return 0

