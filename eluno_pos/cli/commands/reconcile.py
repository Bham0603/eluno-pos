"""Reconciliation recovery command for Eluno POS CLI."""

import json
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
    print("1. Queries payment terminal gateway for actual charge_id and status (Never creates duplicate charges).")
    print("2. Compares refunded_paise against durable baseline to verify whether refund already executed.")
    print("3. Reconciles Shopify orders using persisted invoice metadata (store_code, invoice_no, checkout_ref).")
    print("4. Allocates credit note numbers sequentially ONLY upon confirmed terminal capture.")
    print("5. Unresolved or ambiguous items remain safely PENDING for supervisor inspection.")

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
    for ref in pending_refunds:
        print(f"\n[Reconciling Refund: {ref.refund_id} for Invoice: {ref.invoice_no}]")
        
        # Determine persisted Shopify location ID from original invoice
        inv = ctx.repo.get_invoice(ref.invoice_no)
        persisted_loc_id: Optional[int] = None
        
        if inv:
            # 1. Inspect persisted shopify_order_intents
            with ctx.repo._get_connection() as conn:
                cur = conn.execute("SELECT payload_json FROM shopify_order_intents WHERE invoice_no = ?;", (inv.invoice_no,))
                row = cur.fetchone()
                if row and row["payload_json"]:
                    try:
                        intent_payload = json.loads(row["payload_json"])
                        persisted_loc_id = intent_payload.get("order", {}).get("location_id")
                    except Exception:
                        pass

            # 2. Inspect Shopify order if already synced
            if not persisted_loc_id and inv.shopify_order_id:
                try:
                    shopify_order = ctx.shopify_client.get_order(inv.shopify_order_id)
                    if shopify_order and shopify_order.get("location_id"):
                        persisted_loc_id = int(shopify_order["location_id"])
                except Exception:
                    pass

            # 3. Inspect store locations mapping for invoice's store code
            if not persisted_loc_id and inv.store_code:
                try:
                    all_locs = ctx.shopify_client.get_locations()
                    matched_loc = next((l for l in all_locs if (l.get("eluno", {}).get("store_code") or l.get("store_code")) == inv.store_code), None)
                    if matched_loc and matched_loc.get("id"):
                        persisted_loc_id = int(matched_loc["id"])
                except Exception:
                    pass

        if not persisted_loc_id:
            print(f"  ✖ Error: Persisted Shopify location ID missing for invoice {ref.invoice_no}.")
            print("    Operation kept PENDING for supervisor / manual resolution.")
            unresolved_count += 1
            continue

        try:
            ref_res = ctx.refund_service.resume_or_reconcile_refund(ref.refund_id, location_id=persisted_loc_id)
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
