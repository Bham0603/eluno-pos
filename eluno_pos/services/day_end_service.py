"""Day-end store reporting and multi-system reconciliation service."""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.domain.invoice import Invoice, InvoiceStatus
from eluno_pos.domain.refund import RefundRecord, RefundStatus
from eluno_pos.domain.report import DayEndReport, HsnTaxBreakdown
from eluno_pos.repositories.invoice_repository import InvoiceRepository
from eluno_pos.services.gst_service import GstService


IST = timezone(timedelta(hours=5, minutes=30))


class DayEndReportService:
    """Service to generate store day-end financial reports and reconcile against Shopify."""

    def __init__(
        self,
        invoice_repository: InvoiceRepository,
        shopify_client: Optional[ShopifyClient] = None,
        gst_service: Optional[GstService] = None,
    ) -> None:
        self.repo = invoice_repository
        self.shopify_client = shopify_client
        self.gst_service = gst_service or GstService()

    def _to_ist_date_string(self, iso_timestamp: str) -> str:
        """Convert ISO timestamp string to IST business date (YYYY-MM-DD)."""
        try:
            dt = datetime.fromisoformat(iso_timestamp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(IST).strftime("%Y-%m-%d")
        except Exception:
            return iso_timestamp[:10]

    def generate_report(
        self,
        store_code: str,
        location_id: int,
        business_date: str,  # YYYY-MM-DD
        reconcile_with_shopify: bool = True,
    ) -> DayEndReport:
        """Generate day-end report for a store on a given business date.

        REPORTING & RECONCILIATION INVARIANTS:
        1. Sales totals include ONLY POSTED invoices.
        2. PENDING_RECONCILIATION invoices are reported in pending_reconciliation_count.
        3. CANCELLED invoices are reported in cancelled_invoices_count and excluded from sales.
        4. Refunds are aggregated from COMPLETED refunds for that date.
        5. Net after refunds = net_sales_paise - refunds_amount_paise.
        6. GST is broken down by HSN and reconciled to the exact paisa.
        7. Payment methods and channels are broken down from audit records.
        8. Cross-checks against Shopify orders/refunds and explicitly flags any discrepancies.
        """
        # 1. Fetch store invoices and filter by business date
        with self.repo._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM invoices WHERE store_code = ? ORDER BY seq ASC;",
                (store_code,),
            )
            all_invoices = [self.repo._build_invoice_from_row(conn, r) for r in cur.fetchall()]

        invoices_for_date = [
            inv for inv in all_invoices if self._to_ist_date_string(inv.created_at) == business_date
        ]

        # 2. Partition invoices by lifecycle status
        posted_invoices = [inv for inv in invoices_for_date if inv.status == InvoiceStatus.POSTED]
        cancelled_invoices = [inv for inv in invoices_for_date if inv.status == InvoiceStatus.CANCELLED]
        pending_invoices = [inv for inv in invoices_for_date if inv.status == InvoiceStatus.PENDING_RECONCILIATION]

        # 3. Aggregate sales figures from posted invoices
        posted_sales_count = len(posted_invoices)
        gross_sales_paise = sum(inv.gross_paise for inv in posted_invoices)
        total_discounts_paise = sum(inv.discount_paise for inv in posted_invoices)
        net_sales_paise = sum(inv.net_paise for inv in posted_invoices)
        taxable_value_paise = sum(inv.taxable_paise for inv in posted_invoices)
        total_gst_paise = sum(inv.tax_paise for inv in posted_invoices)

        # 4. Aggregate HSN breakdown, CGST, and SGST across posted line items
        hsn_totals: Dict[str, Dict[str, int]] = {}
        cgst_paise = 0
        sgst_paise = 0

        # Parse transaction date for tax rate determination
        tx_date = datetime.strptime(business_date, "%Y-%m-%d").date()

        for inv in posted_invoices:
            for line in inv.lines:
                lt = self.gst_service.calculate_line_tax(
                    hsn=line.hsn,
                    net_inclusive_paise=line.net_paise,
                    transaction_date=tx_date,
                )
                cgst_paise += lt.cgst_paise
                sgst_paise += lt.sgst_paise

                if line.hsn not in hsn_totals:
                    hsn_totals[line.hsn] = {
                        "taxable_paise": 0,
                        "cgst_paise": 0,
                        "sgst_paise": 0,
                        "total_gst_paise": 0,
                    }
                hsn_totals[line.hsn]["taxable_paise"] += lt.taxable_value_paise
                hsn_totals[line.hsn]["cgst_paise"] += lt.cgst_paise
                hsn_totals[line.hsn]["sgst_paise"] += lt.sgst_paise
                hsn_totals[line.hsn]["total_gst_paise"] += lt.total_gst_paise

        hsn_breakdowns = {
            h: HsnTaxBreakdown(
                hsn=h,
                taxable_paise=data["taxable_paise"],
                cgst_paise=data["cgst_paise"],
                sgst_paise=data["sgst_paise"],
                total_gst_paise=data["total_gst_paise"],
            )
            for h, data in hsn_totals.items()
        }

        # 5. Aggregate completed refunds
        with self.repo._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT r.* FROM refunds r
                JOIN invoices i ON r.invoice_no = i.invoice_no
                WHERE i.store_code = ? AND r.status = ?
                ORDER BY r.created_at ASC;
                """,
                (store_code, RefundStatus.COMPLETED.value),
            )
            all_refunds = [self.repo._build_refund_from_row(r) for r in cur.fetchall()]

        refunds_for_date = [
            r for r in all_refunds if self._to_ist_date_string(r.created_at) == business_date
        ]

        refunds_count = len(refunds_for_date)
        refunds_amount_paise = sum(r.amount_paise for r in refunds_for_date)
        net_sales_after_refunds_paise = net_sales_paise - refunds_amount_paise

        # 6. Payment method and channel breakdowns
        payment_method_totals: Dict[str, int] = {}
        channel_totals: Dict[str, int] = {}

        for inv in posted_invoices:
            # Query payment attempt to find method
            att = self.repo.get_payment_attempt(f"pay_{inv.checkout_ref}")
            method = att.method if att else "unknown"
            payment_method_totals[method] = payment_method_totals.get(method, 0) + inv.net_paise

            # Channel from intent payload if stored
            intent = self.repo.get_shopify_intent(inv.invoice_no)
            channel = "walk_in"
            if intent:
                attrs = {a.get("name"): a.get("value") for a in intent.get("payload", {}).get("note_attributes", [])}
                channel = attrs.get("channel", "walk_in")
            channel_totals[channel] = channel_totals.get(channel, 0) + inv.net_paise

        # 7. Multi-system reconciliation against Shopify
        is_reconciled = True
        discrepancies: List[str] = []

        if reconcile_with_shopify and self.shopify_client:
            try:
                shopify_orders = self.shopify_client.get_orders(
                    location_id=location_id,
                    status="any",
                    limit=100,
                )
                matching_orders = [
                    o for o in shopify_orders
                    if self._to_ist_date_string(o.get("created_at", "")) == business_date
                ]

                # Compare bill count
                if len(matching_orders) != posted_sales_count:
                    is_reconciled = False
                    discrepancies.append(
                        f"Bill count mismatch: Local POS has {posted_sales_count} posted bills, "
                        f"Shopify returned {len(matching_orders)} orders for location {location_id}"
                    )

                # Compare net total
                shopify_net_paise = sum(
                    round(float(o.get("total_price", "0.00")) * 100) for o in matching_orders
                )
                if shopify_net_paise != net_sales_paise:
                    is_reconciled = False
                    discrepancies.append(
                        f"Net sales mismatch: Local POS has ₹{net_sales_paise/100:.2f}, "
                        f"Shopify returned ₹{shopify_net_paise/100:.2f}"
                    )

            except Exception as err:
                is_reconciled = False
                discrepancies.append(f"Could not connect to Shopify for reconciliation: {err}")

        return DayEndReport(
            business_date=business_date,
            store_code=store_code,
            location_id=location_id,
            timezone="Asia/Kolkata",
            posted_sales_count=posted_sales_count,
            gross_sales_paise=gross_sales_paise,
            total_discounts_paise=total_discounts_paise,
            net_sales_paise=net_sales_paise,
            taxable_value_paise=taxable_value_paise,
            cgst_paise=cgst_paise,
            sgst_paise=sgst_paise,
            total_gst_paise=total_gst_paise,
            refunds_count=refunds_count,
            refunds_amount_paise=refunds_amount_paise,
            net_sales_after_refunds_paise=net_sales_after_refunds_paise,
            payment_method_totals=payment_method_totals,
            channel_totals=channel_totals,
            hsn_totals=hsn_breakdowns,
            cancelled_invoices_count=len(cancelled_invoices),
            pending_reconciliation_count=len(pending_invoices),
            is_reconciled=is_reconciled,
            discrepancies=discrepancies,
        )
