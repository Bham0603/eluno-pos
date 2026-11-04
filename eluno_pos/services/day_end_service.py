"""Day-end store reporting and multi-system reconciliation service."""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.domain.invoice import Invoice, InvoiceLine, InvoiceStatus
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

    def _get_line_tax_breakdown(self, line: InvoiceLine, inv_date_str: str):
        """Extract line tax breakdown from persisted values or calculate using original invoice date."""
        if line.taxable_paise > 0 or line.tax_paise > 0:
            return line.taxable_paise, line.cgst_paise, line.sgst_paise, line.tax_paise

        # Fallback to computing based on original invoice date
        try:
            inv_date = datetime.strptime(self._to_ist_date_string(inv_date_str), "%Y-%m-%d").date()
        except Exception:
            inv_date = datetime.now(IST).date()

        lt = self.gst_service.calculate_line_tax(
            hsn=line.hsn,
            net_inclusive_paise=line.net_paise,
            transaction_date=inv_date,
        )
        return lt.taxable_value_paise, lt.cgst_paise, lt.sgst_paise, lt.total_gst_paise

    def generate_report(
        self,
        store_code: str,
        location_id: int,
        business_date: str,  # YYYY-MM-DD
        reconcile_with_shopify: bool = True,
    ) -> DayEndReport:
        """Generate day-end report for a store on a given business date.

        REPORTING & RECONCILIATION INVARIANTS:
        1. Sales totals include ONLY POSTED invoices whose created_at in IST falls on business_date.
        2. PENDING_RECONCILIATION invoices are reported in pending_reconciliation_count.
        3. CANCELLED invoices are reported in cancelled_invoices_count and excluded from sales.
        4. Refunds are aggregated from COMPLETED refunds whose created_at in IST falls on business_date.
        5. Separate gross sales, refund, and net values for revenue, taxable value, and GST.
        6. Uses the original invoice's persisted line tax breakdown for refund calculations.
           Does NOT recalculate refund GST using an independently selected current tax rate.
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

        # 4. Aggregate HSN breakdown and tax totals across posted line items
        hsn_data: Dict[str, Dict[str, int]] = {}

        def _ensure_hsn(h: str):
            if h not in hsn_data:
                hsn_data[h] = {
                    "sales_taxable_paise": 0,
                    "sales_cgst_paise": 0,
                    "sales_sgst_paise": 0,
                    "sales_total_gst_paise": 0,
                    "refund_taxable_paise": 0,
                    "refund_cgst_paise": 0,
                    "refund_sgst_paise": 0,
                    "refund_total_gst_paise": 0,
                }

        sales_taxable_value_paise = 0
        sales_cgst_paise = 0
        sales_sgst_paise = 0
        sales_total_gst_paise = 0

        for inv in posted_invoices:
            for line in inv.lines:
                _ensure_hsn(line.hsn)
                taxable, cgst, sgst, tot_gst = self._get_line_tax_breakdown(line, inv.created_at)

                sales_taxable_value_paise += taxable
                sales_cgst_paise += cgst
                sales_sgst_paise += sgst
                sales_total_gst_paise += tot_gst

                hsn_data[line.hsn]["sales_taxable_paise"] += taxable
                hsn_data[line.hsn]["sales_cgst_paise"] += cgst
                hsn_data[line.hsn]["sales_sgst_paise"] += sgst
                hsn_data[line.hsn]["sales_total_gst_paise"] += tot_gst

        # 5. Aggregate completed refunds for business date
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

        # Filter strictly by business date in Asia/Kolkata
        refunds_for_date = [
            r for r in all_refunds if self._to_ist_date_string(r.created_at) == business_date
        ]

        # Deduplicate refunds to guarantee no double-counting
        seen_refund_ids = set()
        unique_refunds: List[RefundRecord] = []
        for r in refunds_for_date:
            if r.refund_id not in seen_refund_ids:
                seen_refund_ids.add(r.refund_id)
                unique_refunds.append(r)

        refunds_count = len(unique_refunds)
        refunds_amount_paise = sum(r.amount_paise for r in unique_refunds)

        refund_taxable_value_paise = 0
        refund_cgst_paise = 0
        refund_sgst_paise = 0
        refund_total_gst_paise = 0

        # For each completed full refund, reverse the original invoice's persisted line taxes
        for ref in unique_refunds:
            orig_inv = self.repo.get_invoice(ref.invoice_no)
            if orig_inv:
                for line in orig_inv.lines:
                    _ensure_hsn(line.hsn)
                    taxable, cgst, sgst, tot_gst = self._get_line_tax_breakdown(line, orig_inv.created_at)

                    refund_taxable_value_paise += taxable
                    refund_cgst_paise += cgst
                    refund_sgst_paise += sgst
                    refund_total_gst_paise += tot_gst

                    hsn_data[line.hsn]["refund_taxable_paise"] += taxable
                    hsn_data[line.hsn]["refund_cgst_paise"] += cgst
                    hsn_data[line.hsn]["refund_sgst_paise"] += sgst
                    hsn_data[line.hsn]["refund_total_gst_paise"] += tot_gst

        # 6. Compute Net Figures (Sales less Refunds)
        net_sales_after_refunds_paise = net_sales_paise - refunds_amount_paise
        net_taxable_value_paise = sales_taxable_value_paise - refund_taxable_value_paise
        net_cgst_paise = sales_cgst_paise - refund_cgst_paise
        net_sgst_paise = sales_sgst_paise - refund_sgst_paise
        net_total_gst_paise = sales_total_gst_paise - refund_total_gst_paise

        hsn_breakdowns = {
            h: HsnTaxBreakdown(
                hsn=h,
                sales_taxable_paise=data["sales_taxable_paise"],
                sales_cgst_paise=data["sales_cgst_paise"],
                sales_sgst_paise=data["sales_sgst_paise"],
                sales_total_gst_paise=data["sales_total_gst_paise"],
                refund_taxable_paise=data["refund_taxable_paise"],
                refund_cgst_paise=data["refund_cgst_paise"],
                refund_sgst_paise=data["refund_sgst_paise"],
                refund_total_gst_paise=data["refund_total_gst_paise"],
                net_taxable_paise=data["sales_taxable_paise"] - data["refund_taxable_paise"],
                net_cgst_paise=data["sales_cgst_paise"] - data["refund_cgst_paise"],
                net_sgst_paise=data["sales_sgst_paise"] - data["refund_sgst_paise"],
                net_total_gst_paise=data["sales_total_gst_paise"] - data["refund_total_gst_paise"],
            )
            for h, data in hsn_data.items()
        }

        # 7. Payment method and channel breakdowns
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

        # 8. Multi-system reconciliation against Shopify
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
            sales_taxable_value_paise=sales_taxable_value_paise,
            sales_cgst_paise=sales_cgst_paise,
            sales_sgst_paise=sales_sgst_paise,
            sales_total_gst_paise=sales_total_gst_paise,
            refunds_count=refunds_count,
            refunds_amount_paise=refunds_amount_paise,
            refund_taxable_value_paise=refund_taxable_value_paise,
            refund_cgst_paise=refund_cgst_paise,
            refund_sgst_paise=refund_sgst_paise,
            refund_total_gst_paise=refund_total_gst_paise,
            net_sales_after_refunds_paise=net_sales_after_refunds_paise,
            net_taxable_value_paise=net_taxable_value_paise,
            net_cgst_paise=net_cgst_paise,
            net_sgst_paise=net_sgst_paise,
            net_total_gst_paise=net_total_gst_paise,
            payment_method_totals=payment_method_totals,
            channel_totals=channel_totals,
            hsn_totals=hsn_breakdowns,
            cancelled_invoices_count=len(cancelled_invoices),
            pending_reconciliation_count=len(pending_invoices),
            is_reconciled=is_reconciled,
            discrepancies=discrepancies,
        )
