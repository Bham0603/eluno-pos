"""Day-end store reporting and multi-system reconciliation service."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.domain.invoice import Invoice, InvoiceLine, InvoiceStatus
from eluno_pos.domain.money import format_paise_to_rupees, round_half_up
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
        if not iso_timestamp:
            return ""
        try:
            s = iso_timestamp.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(IST).strftime("%Y-%m-%d")
        except Exception:
            return iso_timestamp[:10]

    def _parse_money(self, val: Any) -> int:
        """Convert Shopify decimal string/number into integer paise without binary floating-point representation."""
        if val is None or val == "":
            return 0
        if isinstance(val, int):
            return val * 100
        s = str(val).strip()
        return int((Decimal(s) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def _is_valid_hsn(self, hsn: Optional[str]) -> bool:
        """Validate line-item HSN against the configured HSN master from tax_rates.json."""
        if not hsn:
            return False
        clean = str(hsn).strip()
        if not clean:
            return False
        valid_hsns = {e.hsn for e in self.gst_service.entries}
        return clean in valid_hsns

    def _parse_tax_line_price(self, tl: Any) -> Optional[int]:
        """Validate and parse a tax line monetary price into paise. Returns None if missing or invalid."""
        if not isinstance(tl, dict):
            return None
        price_val = tl.get("price")
        if price_val is None:
            return None
        price_str = str(price_val).strip()
        if not price_str:
            return None
        try:
            return self._parse_money(price_str)
        except Exception:
            return None

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

    def _fetch_shopify_orders_for_store(
        self,
        store_code: str,
        location_id: int,
        business_date: str,
    ) -> List[Dict[str, Any]]:
        """Fetch Shopify orders for the given location and IST business date.

        Two queries are performed:
        1. created_at window: orders created on the requested IST business day.
        2. updated_at window: orders updated on the requested IST business day (to discover
           old orders that received a refund today).

        Results are merged by Shopify order ID to avoid duplicates.
        The existing SQLite completed-refund reference lookup supplements as a recovery mechanism.
        """
        assert self.shopify_client is not None

        # Build IST business-day boundaries as ISO-8601 strings with +05:30 offset
        try:
            from datetime import date as _date
            parsed = _date.fromisoformat(business_date)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid business_date format '{business_date}': expected YYYY-MM-DD") from exc

        day_start = datetime(parsed.year, parsed.month, parsed.day, 0, 0, 0, tzinfo=IST)
        following_day_start = day_start + timedelta(days=1)

        # Format as ISO-8601 with explicit +05:30 offset (Shopify REST accepts this)
        def _fmt(dt: datetime) -> str:
            return dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")

        ist_min = _fmt(day_start)
        ist_max = _fmt(following_day_start)

        order_dict: Dict[int, Dict[str, Any]] = {}

        # Query 1: orders CREATED on the requested IST business day
        created_orders = self.shopify_client.get_orders(
            created_at_min=ist_min,
            created_at_max=ist_max,
            location_id=location_id,
            status="any",
            limit=250,
        )
        for o in created_orders:
            if "id" in o:
                try:
                    order_dict[int(o["id"])] = o
                except (ValueError, TypeError):
                    pass

        # Query 2: orders UPDATED on the requested IST business day
        # This discovers old orders that were refunded today.
        updated_orders = self.shopify_client.get_orders(
            updated_at_min=ist_min,
            updated_at_max=ist_max,
            location_id=location_id,
            status="any",
            limit=250,
        )
        for o in updated_orders:
            if "id" in o:
                try:
                    order_dict[int(o["id"])] = o
                except (ValueError, TypeError):
                    pass

        # SQLite recovery supplement: look for completed refunds referencing orders not yet in order_dict
        try:
            with self.repo._get_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT DISTINCT r.shopify_order_id FROM refunds r
                    JOIN invoices i ON r.invoice_no = i.invoice_no
                    WHERE i.store_code = ? AND r.status = ? AND r.shopify_order_id IS NOT NULL;
                    """,
                    (store_code, RefundStatus.COMPLETED.value),
                )
                for (order_id,) in cur.fetchall():
                    if order_id:
                        try:
                            oid = int(order_id)
                        except (ValueError, TypeError):
                            continue
                        if oid not in order_dict:
                            try:
                                fetched_order = self.shopify_client.get_order(oid)
                                if fetched_order and "id" in fetched_order:
                                    order_dict[int(fetched_order["id"])] = fetched_order
                            except Exception:
                                pass
        except Exception:
            pass

        return list(order_dict.values())

    def _compute_from_shopify(
        self,
        store_code: str,
        location_id: int,
        business_date: str,
    ) -> Dict[str, Any]:
        """Compute financial figures, HSN tax breakdowns, and payment splits directly from Shopify orders and refunds."""
        candidate_orders = self._fetch_shopify_orders_for_store(store_code, location_id, business_date)

        # 1. Filter sales orders for business_date in Asia/Kolkata
        sales_orders: List[Dict[str, Any]] = []
        for o in candidate_orders:
            # Match location ID
            if o.get("location_id") is not None and int(o["location_id"]) != location_id:
                continue
            # Match business date
            created_at_ist = self._to_ist_date_string(o.get("created_at", ""))
            if created_at_ist != business_date:
                continue
            # Must not be cancelled
            if o.get("cancelled_at"):
                continue
            # Strict paid-order eligibility positive allowlist: count only paid, partially_refunded, refunded
            fin_status = str(o.get("financial_status", "")).strip().lower()
            if fin_status not in ("paid", "partially_refunded", "refunded"):
                continue

            sales_orders.append(o)

        posted_sales_count = len(sales_orders)
        gross_sales_paise = 0
        total_discounts_paise = 0
        net_sales_paise = 0
        sales_taxable_value_paise = 0
        sales_cgst_paise = 0
        sales_sgst_paise = 0
        sales_total_gst_paise = 0

        quality_discrepancies: List[str] = []
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

        payment_method_totals: Dict[str, int] = {}
        channel_totals: Dict[str, int] = {}

        for o in sales_orders:
            order_id_str = str(o.get("id", "UNKNOWN_ORDER"))
            order_subtotal = self._parse_money(o.get("subtotal_price"))
            order_discounts = self._parse_money(o.get("total_discounts"))
            order_net = self._parse_money(o.get("total_price"))

            order_total_tax_paise: Optional[int] = None
            raw_order_tax = o.get("total_tax")
            if raw_order_tax is not None and str(raw_order_tax).strip() != "":
                try:
                    order_total_tax_paise = self._parse_money(raw_order_tax)
                except Exception:
                    order_total_tax_paise = None

            line_items = o.get("line_items", [])

            if not line_items:
                quality_discrepancies.append(
                    f"Order {order_id_str}: Missing line_items; itemized HSN and tax breakdown is incomplete."
                )
                gross_sales_paise += order_subtotal if order_subtotal > 0 else order_net
                total_discounts_paise += order_discounts
                net_sales_paise += order_net
                ord_tax = order_total_tax_paise if order_total_tax_paise is not None else 0
                sales_total_gst_paise += ord_tax
                taxable = order_net - ord_tax
                sales_taxable_value_paise += taxable
                _ensure_hsn("UNKNOWN")
                hsn_data["UNKNOWN"]["sales_taxable_paise"] += taxable
                hsn_data["UNKNOWN"]["sales_total_gst_paise"] += ord_tax
            else:
                parsed_lines = []
                for li in line_items:
                    line_id_str = str(li.get("id") or li.get("variant_id") or "UNKNOWN_LINE")
                    qty = int(li.get("quantity", 1))
                    price_paise = self._parse_money(li.get("price"))
                    li_gross = price_paise * qty
                    li_discount = self._parse_money(li.get("total_discount"))
                    li_net = li_gross - li_discount

                    raw_hsn = None
                    for prop in li.get("properties", []):
                        if isinstance(prop, dict) and prop.get("name") == "hsn" and prop.get("value") is not None:
                            val = str(prop["value"]).strip()
                            if val:
                                raw_hsn = val
                            break

                    valid_hsn = raw_hsn if (raw_hsn and self._is_valid_hsn(raw_hsn)) else None
                    if not valid_hsn:
                        if raw_hsn:
                            quality_discrepancies.append(
                                f"Order {order_id_str} line {line_id_str}: Unsupported HSN '{raw_hsn}'; recorded under 'UNKNOWN'."
                            )
                        else:
                            quality_discrepancies.append(
                                f"Order {order_id_str} line {line_id_str}: Missing or invalid HSN metadata; recorded under 'UNKNOWN'."
                            )

                    tax_lines = li.get("tax_lines", [])
                    cgst_tls = [tl for tl in tax_lines if isinstance(tl, dict) and tl.get("title") == "CGST"]
                    sgst_tls = [tl for tl in tax_lines if isinstance(tl, dict) and tl.get("title") == "SGST"]

                    cgst_prices = [self._parse_tax_line_price(tl) for tl in cgst_tls]
                    sgst_prices = [self._parse_tax_line_price(tl) for tl in sgst_tls]

                    has_valid_cgst = len(cgst_prices) > 0 and all(p is not None for p in cgst_prices)
                    has_valid_sgst = len(sgst_prices) > 0 and all(p is not None for p in sgst_prices)
                    is_tax_complete = has_valid_cgst and has_valid_sgst
                    has_partial_tax_lines = (len(cgst_tls) > 0 or len(sgst_tls) > 0) and not is_tax_complete

                    parsed_lines.append({
                        "line_id_str": line_id_str,
                        "li_gross": li_gross,
                        "li_discount": li_discount,
                        "li_net": li_net,
                        "valid_hsn": valid_hsn,
                        "has_valid_cgst": has_valid_cgst,
                        "has_valid_sgst": has_valid_sgst,
                        "is_tax_complete": is_tax_complete,
                        "has_partial_tax_lines": has_partial_tax_lines,
                        "cgst_prices": cgst_prices,
                        "sgst_prices": sgst_prices,
                        "tax_lines": tax_lines,
                    })

                order_line_gross_sum = sum(l["li_gross"] for l in parsed_lines)
                effective_order_net = (order_line_gross_sum - order_discounts) if order_net == 0 else order_net

                # Separate verified complete lines from incomplete lines
                complete_lines = []
                incomplete_lines = []
                complete_lines_taxable = 0
                complete_lines_cgst = 0
                complete_lines_sgst = 0
                complete_lines_tax = 0

                for l in parsed_lines:
                    if l["is_tax_complete"]:
                        li_cgst = sum(p for p in l["cgst_prices"] if p is not None)
                        li_sgst = sum(p for p in l["sgst_prices"] if p is not None)
                        li_tot_tax = li_cgst + li_sgst
                        li_taxable = l["li_net"] - li_tot_tax
                        hsn = l["valid_hsn"] or "UNKNOWN"
                        _ensure_hsn(hsn)
                        hsn_data[hsn]["sales_taxable_paise"] += li_taxable
                        hsn_data[hsn]["sales_cgst_paise"] += li_cgst
                        hsn_data[hsn]["sales_sgst_paise"] += li_sgst
                        hsn_data[hsn]["sales_total_gst_paise"] += li_tot_tax

                        complete_lines.append(l)
                        complete_lines_taxable += li_taxable
                        complete_lines_cgst += li_cgst
                        complete_lines_sgst += li_sgst
                        complete_lines_tax += li_tot_tax
                    else:
                        incomplete_lines.append(l)

                incomplete_lines_net = sum(l["li_net"] for l in incomplete_lines)

                gross_sales_paise += order_line_gross_sum
                total_discounts_paise += order_discounts
                net_sales_paise += effective_order_net

                # CGST and SGST splits are NOT guessed when metadata is incomplete
                sales_cgst_paise += complete_lines_cgst
                sales_sgst_paise += complete_lines_sgst

                if order_total_tax_paise is not None:
                    # Authoritative Shopify order-level total_tax is provided
                    ord_tax = order_total_tax_paise
                    ord_taxable = effective_order_net - ord_tax

                    # Consistency check: detect when verified line GST exceeds order total_tax
                    if complete_lines_tax > ord_tax:
                        incomplete_tax = 0
                        quality_discrepancies.append(
                            f"Order {order_id_str}: Verified line-level GST (₹{format_paise_to_rupees(complete_lines_tax)}) exceeds Shopify authoritative order total_tax (₹{format_paise_to_rupees(ord_tax)}). HSN tax breakdown does not reconcile with Shopify order total."
                        )
                        sales_total_gst_paise += ord_tax
                    else:
                        incomplete_tax = ord_tax - complete_lines_tax
                        sales_total_gst_paise += ord_tax

                    # Consistency check: detect when verified line taxable exceeds order taxable
                    if complete_lines_taxable > ord_taxable:
                        incomplete_taxable = 0
                        quality_discrepancies.append(
                            f"Order {order_id_str}: Verified line-level taxable value (₹{format_paise_to_rupees(complete_lines_taxable)}) exceeds Shopify order taxable amount (₹{format_paise_to_rupees(ord_taxable)}). HSN taxable breakdown does not reconcile with Shopify order total."
                        )
                        sales_taxable_value_paise += ord_taxable
                    else:
                        incomplete_taxable = ord_taxable - complete_lines_taxable
                        sales_taxable_value_paise += ord_taxable

                    if incomplete_lines:
                        _ensure_hsn("UNKNOWN")
                        hsn_data["UNKNOWN"]["sales_taxable_paise"] += incomplete_taxable
                        hsn_data["UNKNOWN"]["sales_total_gst_paise"] += incomplete_tax

                        for l in incomplete_lines:
                            quality_discrepancies.append(
                                f"Order {order_id_str} line {l['line_id_str']}: Incomplete tax data; missing CGST/SGST breakdown (missing or invalid CGST/SGST tax line price). Preserving authoritative order-level total_tax (₹{format_paise_to_rupees(order_total_tax_paise)}); line tax allocated to 'UNKNOWN'."
                            )
                else:
                    # No authoritative order-level total_tax exists
                    # Only lines with valid CGST AND valid SGST prices contribute verified line-level GST
                    # Every incomplete line contributes zero unverified GST
                    incomplete_tax = 0
                    incomplete_taxable = incomplete_lines_net

                    sales_total_gst_paise += complete_lines_tax
                    sales_taxable_value_paise += (complete_lines_taxable + incomplete_taxable)

                    if incomplete_lines:
                        _ensure_hsn("UNKNOWN")
                        hsn_data["UNKNOWN"]["sales_taxable_paise"] += incomplete_taxable
                        hsn_data["UNKNOWN"]["sales_total_gst_paise"] += 0

                        for l in incomplete_lines:
                            quality_discrepancies.append(
                                f"Order {order_id_str} line {l['line_id_str']}: Incomplete tax data; missing or invalid CGST/SGST breakdown (missing or invalid CGST/SGST tax line price) with no authoritative order-level total_tax. Partial tax lines cannot be treated as complete GST; Unverified line contributes zero GST."
                            )

            # Payment method and channel from note_attributes or transactions
            attrs = {a.get("name"): a.get("value") for a in o.get("note_attributes", []) if isinstance(a, dict)}
            pm = attrs.get("payment_method")
            if not pm:
                txs = o.get("transactions", [])
                pm = txs[0].get("gateway", "unknown") if txs else "unknown"

            effective_order_net = order_net if order_net > 0 else (order_subtotal - order_discounts)
            payment_method_totals[pm] = payment_method_totals.get(pm, 0) + effective_order_net

            channel = attrs.get("channel", "walk_in")
            channel_totals[channel] = channel_totals.get(channel, 0) + effective_order_net

        # 2. Extract refunds across all candidate orders matching business_date
        refunds_count = 0
        refunds_amount_paise = 0
        refund_taxable_value_paise = 0
        refund_cgst_paise = 0
        refund_sgst_paise = 0
        refund_total_gst_paise = 0

        seen_refund_ids = set()

        for o in candidate_orders:
            for ref in o.get("refunds", []):
                ref_id = ref.get("id")
                if ref_id in seen_refund_ids:
                    continue
                ref_date_ist = self._to_ist_date_string(ref.get("created_at", ""))
                if ref_date_ist != business_date:
                    continue

                seen_refund_ids.add(ref_id)
                refunds_count += 1

                # Establish refund amount from refund transactions or line items
                ref_txs = [t for t in ref.get("transactions", []) if t.get("kind") == "refund" and t.get("status") == "success"]
                if ref_txs:
                    ref_amt = sum(self._parse_money(t.get("amount")) for t in ref_txs)
                else:
                    ref_amt = sum(
                        self._parse_money(rl.get("subtotal")) + self._parse_money(rl.get("total_tax"))
                        for rl in ref.get("refund_line_items", [])
                    )

                refunds_amount_paise += ref_amt

                order_id_str = str(o.get("id", "UNKNOWN_ORDER"))
                order_lines_by_id = {li.get("id"): li for li in o.get("line_items", [])}
                refund_line_items = ref.get("refund_line_items", [])

                if refund_line_items:
                    for rl in refund_line_items:
                        li_id = rl.get("line_item_id")
                        line_id_str = str(li_id or "UNKNOWN_LINE")
                        orig_li = order_lines_by_id.get(li_id)

                        raw_hsn = None
                        orig_tax_lines = []
                        orig_qty = 1
                        if orig_li:
                            orig_qty = int(orig_li.get("quantity", 1))
                            orig_tax_lines = orig_li.get("tax_lines", [])
                            for prop in orig_li.get("properties", []):
                                if isinstance(prop, dict) and prop.get("name") == "hsn" and prop.get("value") is not None:
                                    val = str(prop["value"]).strip()
                                    if val:
                                        raw_hsn = val
                                    break
                        if raw_hsn and self._is_valid_hsn(raw_hsn):
                            hsn = raw_hsn
                        else:
                            hsn = "UNKNOWN"
                            if raw_hsn:
                                quality_discrepancies.append(
                                    f"Order {order_id_str} refund line {line_id_str}: Unsupported HSN '{raw_hsn}'; recorded under 'UNKNOWN'."
                                )
                            else:
                                quality_discrepancies.append(
                                    f"Order {order_id_str} refund line {line_id_str}: Missing or invalid HSN metadata; recorded under 'UNKNOWN'."
                                )
                        _ensure_hsn(hsn)

                        rl_taxable = self._parse_money(rl.get("subtotal"))
                        rl_tot_tax = self._parse_money(rl.get("total_tax"))
                        ref_qty = int(rl.get("quantity", 1))

                        orig_cgst_tls = [tl for tl in orig_tax_lines if isinstance(tl, dict) and tl.get("title") == "CGST"]
                        orig_sgst_tls = [tl for tl in orig_tax_lines if isinstance(tl, dict) and tl.get("title") == "SGST"]

                        orig_cgst_prices = [self._parse_tax_line_price(tl) for tl in orig_cgst_tls]
                        orig_sgst_prices = [self._parse_tax_line_price(tl) for tl in orig_sgst_tls]

                        has_valid_orig_cgst = len(orig_cgst_prices) > 0 and all(p is not None for p in orig_cgst_prices)
                        has_valid_orig_sgst = len(orig_sgst_prices) > 0 and all(p is not None for p in orig_sgst_prices)

                        if has_valid_orig_cgst and has_valid_orig_sgst:
                            orig_cgst = sum(p for p in orig_cgst_prices if p is not None)
                            orig_sgst = sum(p for p in orig_sgst_prices if p is not None)
                            if ref_qty == orig_qty:
                                rl_cgst = orig_cgst
                                rl_sgst = orig_sgst
                            else:
                                rl_cgst = (orig_cgst * ref_qty) // orig_qty
                                rl_sgst = rl_tot_tax - rl_cgst
                        else:
                            # Do not guess CGST/SGST splits when missing; identify incomplete tax data explicitly
                            if rl_tot_tax > 0:
                                quality_discrepancies.append(
                                    f"Order {order_id_str} refund line {line_id_str}: Incomplete tax data; missing or invalid original CGST/SGST breakdown and tax line price."
                                )
                            rl_cgst = 0
                            rl_sgst = 0
                            hsn = "UNKNOWN"
                            _ensure_hsn(hsn)

                        refund_taxable_value_paise += rl_taxable
                        refund_cgst_paise += rl_cgst
                        refund_sgst_paise += rl_sgst
                        refund_total_gst_paise += rl_tot_tax

                        hsn_data[hsn]["refund_taxable_paise"] += rl_taxable
                        hsn_data[hsn]["refund_cgst_paise"] += rl_cgst
                        hsn_data[hsn]["refund_sgst_paise"] += rl_sgst
                        hsn_data[hsn]["refund_total_gst_paise"] += rl_tot_tax
                else:
                    quality_discrepancies.append(
                        f"Order {order_id_str} refund {ref_id}: Missing refund_line_items; itemized HSN and tax reversal breakdown is incomplete."
                    )

        # 3. Compute Net Figures (Sales less Refunds)
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

        return {
            "posted_sales_count": posted_sales_count,
            "gross_sales_paise": gross_sales_paise,
            "total_discounts_paise": total_discounts_paise,
            "net_sales_paise": net_sales_paise,
            "sales_taxable_value_paise": sales_taxable_value_paise,
            "sales_cgst_paise": sales_cgst_paise,
            "sales_sgst_paise": sales_sgst_paise,
            "sales_total_gst_paise": sales_total_gst_paise,
            "refunds_count": refunds_count,
            "refunds_amount_paise": refunds_amount_paise,
            "refund_taxable_value_paise": refund_taxable_value_paise,
            "refund_cgst_paise": refund_cgst_paise,
            "refund_sgst_paise": refund_sgst_paise,
            "refund_total_gst_paise": refund_total_gst_paise,
            "net_sales_after_refunds_paise": net_sales_after_refunds_paise,
            "net_taxable_value_paise": net_taxable_value_paise,
            "net_cgst_paise": net_cgst_paise,
            "net_sgst_paise": net_sgst_paise,
            "net_total_gst_paise": net_total_gst_paise,
            "payment_method_totals": payment_method_totals,
            "channel_totals": channel_totals,
            "hsn_totals": hsn_breakdowns,
            "quality_discrepancies": quality_discrepancies,
        }

    def _compute_from_local(
        self,
        store_code: str,
        business_date: str,
    ) -> Dict[str, Any]:
        """Aggregate sales and refund figures from local SQLite tables (offline fallback / reconciliation baseline)."""
        with self.repo._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM invoices WHERE store_code = ? ORDER BY seq ASC;",
                (store_code,),
            )
            all_invoices = [self.repo._build_invoice_from_row(conn, r) for r in cur.fetchall()]

        invoices_for_date = [
            inv for inv in all_invoices if self._to_ist_date_string(inv.created_at) == business_date
        ]

        posted_invoices = [inv for inv in invoices_for_date if inv.status == InvoiceStatus.POSTED]

        posted_sales_count = len(posted_invoices)
        gross_sales_paise = sum(inv.gross_paise for inv in posted_invoices)
        total_discounts_paise = sum(inv.discount_paise for inv in posted_invoices)
        net_sales_paise = sum(inv.net_paise for inv in posted_invoices)

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

        payment_method_totals: Dict[str, int] = {}
        channel_totals: Dict[str, int] = {}

        for inv in posted_invoices:
            att = self.repo.get_payment_attempt(f"pay_{inv.checkout_ref}")
            method = att.method if att else "unknown"
            payment_method_totals[method] = payment_method_totals.get(method, 0) + inv.net_paise

            intent = self.repo.get_shopify_intent(inv.invoice_no)
            channel = "walk_in"
            if intent:
                attrs = {a.get("name"): a.get("value") for a in intent.get("payload", {}).get("note_attributes", [])}
                channel = attrs.get("channel", "walk_in")
            channel_totals[channel] = channel_totals.get(channel, 0) + inv.net_paise

        return {
            "posted_sales_count": posted_sales_count,
            "gross_sales_paise": gross_sales_paise,
            "total_discounts_paise": total_discounts_paise,
            "net_sales_paise": net_sales_paise,
            "sales_taxable_value_paise": sales_taxable_value_paise,
            "sales_cgst_paise": sales_cgst_paise,
            "sales_sgst_paise": sales_sgst_paise,
            "sales_total_gst_paise": sales_total_gst_paise,
            "refunds_count": refunds_count,
            "refunds_amount_paise": refunds_amount_paise,
            "refund_taxable_value_paise": refund_taxable_value_paise,
            "refund_cgst_paise": refund_cgst_paise,
            "refund_sgst_paise": refund_sgst_paise,
            "refund_total_gst_paise": refund_total_gst_paise,
            "net_sales_after_refunds_paise": net_sales_after_refunds_paise,
            "net_taxable_value_paise": net_taxable_value_paise,
            "net_cgst_paise": net_cgst_paise,
            "net_sgst_paise": net_sgst_paise,
            "net_total_gst_paise": net_total_gst_paise,
            "payment_method_totals": payment_method_totals,
            "channel_totals": channel_totals,
            "hsn_totals": hsn_breakdowns,
        }

    def _build_report_from_data(
        self,
        store_code: str,
        location_id: int,
        business_date: str,
        data: Dict[str, Any],
        cancelled_count: int,
        pending_count: int,
    ) -> DayEndReport:
        """Helper to instantiate DayEndReport from aggregated financial dictionary."""
        return DayEndReport(
            business_date=business_date,
            store_code=store_code,
            location_id=location_id,
            timezone="Asia/Kolkata",
            posted_sales_count=data["posted_sales_count"],
            gross_sales_paise=data["gross_sales_paise"],
            total_discounts_paise=data["total_discounts_paise"],
            net_sales_paise=data["net_sales_paise"],
            sales_taxable_value_paise=data["sales_taxable_value_paise"],
            sales_cgst_paise=data["sales_cgst_paise"],
            sales_sgst_paise=data["sales_sgst_paise"],
            sales_total_gst_paise=data["sales_total_gst_paise"],
            refunds_count=data["refunds_count"],
            refunds_amount_paise=data["refunds_amount_paise"],
            refund_taxable_value_paise=data["refund_taxable_value_paise"],
            refund_cgst_paise=data["refund_cgst_paise"],
            refund_sgst_paise=data["refund_sgst_paise"],
            refund_total_gst_paise=data["refund_total_gst_paise"],
            net_sales_after_refunds_paise=data["net_sales_after_refunds_paise"],
            net_taxable_value_paise=data["net_taxable_value_paise"],
            net_cgst_paise=data["net_cgst_paise"],
            net_sgst_paise=data["net_sgst_paise"],
            net_total_gst_paise=data["net_total_gst_paise"],
            payment_method_totals=data["payment_method_totals"],
            channel_totals=data["channel_totals"],
            hsn_totals=data["hsn_totals"],
            cancelled_invoices_count=cancelled_count,
            pending_reconciliation_count=pending_count,
            is_reconciled=data.get("is_reconciled", True),
            discrepancies=data.get("discrepancies", []),
        )

    def generate_report(
        self,
        store_code: str,
        location_id: int,
        business_date: str,  # YYYY-MM-DD
        reconcile_with_shopify: bool = True,
    ) -> DayEndReport:
        """Generate day-end report for a store on a given business date.

        ARCHITECTURAL INVARIANTS:
        1. When Shopify is available, all financial figures (sales, discounts, net, taxes,
           refunds, payment methods, channels, and HSN breakdown) are computed directly
           from Shopify orders and refunds (the system of record).
        2. Operational metrics (cancelled_invoices_count, pending_reconciliation_count)
           are extracted from the local SQLite audit ledger.
        3. A bidirectional reconciliation compares Shopify figures against local SQLite
           totals and explicitly flags any discrepancies.
        4. If Shopify is unavailable or cloud sync is disabled, the service falls back
           gracefully to local SQLite figures, marking the report as not reconciled and
           logging an offline fallback alert.
        """
        # 1. Fetch local SQLite operational counts
        with self.repo._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM invoices WHERE store_code = ? ORDER BY seq ASC;",
                (store_code,),
            )
            all_invoices = [self.repo._build_invoice_from_row(conn, r) for r in cur.fetchall()]

        invoices_for_date = [
            inv for inv in all_invoices if self._to_ist_date_string(inv.created_at) == business_date
        ]
        cancelled_invoices = [inv for inv in invoices_for_date if inv.status == InvoiceStatus.CANCELLED]
        pending_invoices = [inv for inv in invoices_for_date if inv.status == InvoiceStatus.PENDING_RECONCILIATION]

        # Compute local baseline figures
        local_data = self._compute_from_local(store_code, business_date)

        # 2. Shopify-First Flow
        if reconcile_with_shopify and self.shopify_client:
            try:
                shopify_data = self._compute_from_shopify(store_code, location_id, business_date)

                # Initialize discrepancies with data quality discrepancies (if any)
                discrepancies: List[str] = list(shopify_data.get("quality_discrepancies", []))
                is_reconciled = (len(discrepancies) == 0)

                if shopify_data["posted_sales_count"] != local_data["posted_sales_count"]:
                    is_reconciled = False
                    discrepancies.append(
                        f"Bill count mismatch: Shopify has {shopify_data['posted_sales_count']} orders, "
                        f"Local POS has {local_data['posted_sales_count']} posted bills."
                    )

                if shopify_data["gross_sales_paise"] != local_data["gross_sales_paise"]:
                    is_reconciled = False
                    discrepancies.append(
                        f"Gross sales mismatch: Shopify has ₹{shopify_data['gross_sales_paise']/100:.2f}, "
                        f"Local POS has ₹{local_data['gross_sales_paise']/100:.2f}."
                    )

                if shopify_data["total_discounts_paise"] != local_data["total_discounts_paise"]:
                    is_reconciled = False
                    discrepancies.append(
                        f"Discounts mismatch: Shopify has ₹{shopify_data['total_discounts_paise']/100:.2f}, "
                        f"Local POS has ₹{local_data['total_discounts_paise']/100:.2f}."
                    )

                if shopify_data["net_sales_paise"] != local_data["net_sales_paise"]:
                    is_reconciled = False
                    discrepancies.append(
                        f"Net sales mismatch: Shopify has ₹{shopify_data['net_sales_paise']/100:.2f}, "
                        f"Local POS has ₹{local_data['net_sales_paise']/100:.2f}."
                    )

                if shopify_data["sales_total_gst_paise"] != local_data["sales_total_gst_paise"]:
                    is_reconciled = False
                    discrepancies.append(
                        f"Sales tax mismatch: Shopify has ₹{shopify_data['sales_total_gst_paise']/100:.2f}, "
                        f"Local POS has ₹{local_data['sales_total_gst_paise']/100:.2f}."
                    )

                if shopify_data["refunds_count"] != local_data["refunds_count"]:
                    is_reconciled = False
                    discrepancies.append(
                        f"Refund count mismatch: Shopify has {shopify_data['refunds_count']} refunds, "
                        f"Local POS has {local_data['refunds_count']} refunds."
                    )

                if shopify_data["refunds_amount_paise"] != local_data["refunds_amount_paise"]:
                    is_reconciled = False
                    discrepancies.append(
                        f"Refund amount mismatch: Shopify has ₹{shopify_data['refunds_amount_paise']/100:.2f}, "
                        f"Local POS has ₹{local_data['refunds_amount_paise']/100:.2f}."
                    )

                shopify_data["is_reconciled"] = is_reconciled
                shopify_data["discrepancies"] = discrepancies

                return self._build_report_from_data(
                    store_code=store_code,
                    location_id=location_id,
                    business_date=business_date,
                    data=shopify_data,
                    cancelled_count=len(cancelled_invoices),
                    pending_count=len(pending_invoices),
                )

            except Exception as err:
                # Offline Fallback when Shopify fails/times out
                local_data["is_reconciled"] = False
                local_data["discrepancies"] = [
                    f"Shopify API unavailable ({err}); financial figures computed from local SQLite audit ledger (offline fallback)."
                ]
                return self._build_report_from_data(
                    store_code=store_code,
                    location_id=location_id,
                    business_date=business_date,
                    data=local_data,
                    cancelled_count=len(cancelled_invoices),
                    pending_count=len(pending_invoices),
                )

        # 3. Cloud Sync Disabled (reconcile_with_shopify=False)
        local_data["is_reconciled"] = False
        local_data["discrepancies"] = [
            "Shopify reconciliation skipped; figures are based on the local SQLite ledger."
        ]
        return self._build_report_from_data(
            store_code=store_code,
            location_id=location_id,
            business_date=business_date,
            data=local_data,
            cancelled_count=len(cancelled_invoices),
            pending_count=len(pending_invoices),
        )
