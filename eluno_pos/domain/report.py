"""Day-end store reporting models for Eluno POS."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class HsnTaxBreakdown:
    """Tax breakdown for a specific HSN separating sales, refunds, and net totals."""
    hsn: str
    sales_taxable_paise: int = 0
    sales_cgst_paise: int = 0
    sales_sgst_paise: int = 0
    sales_total_gst_paise: int = 0
    refund_taxable_paise: int = 0
    refund_cgst_paise: int = 0
    refund_sgst_paise: int = 0
    refund_total_gst_paise: int = 0
    net_taxable_paise: int = 0
    net_cgst_paise: int = 0
    net_sgst_paise: int = 0
    net_total_gst_paise: int = 0

    @property
    def taxable_paise(self) -> int:
        """Compatibility property for sales taxable value."""
        return self.sales_taxable_paise

    @property
    def cgst_paise(self) -> int:
        """Compatibility property for sales CGST value."""
        return self.sales_cgst_paise

    @property
    def sgst_paise(self) -> int:
        """Compatibility property for sales SGST value."""
        return self.sales_sgst_paise

    @property
    def total_gst_paise(self) -> int:
        """Compatibility property for sales total GST value."""
        return self.sales_total_gst_paise


@dataclass
class DayEndReport:
    """Consolidated financial day-end report for a store and business date."""
    business_date: str  # YYYY-MM-DD
    store_code: str
    location_id: int
    timezone: str = "Asia/Kolkata"

    # Sales
    posted_sales_count: int = 0
    gross_sales_paise: int = 0
    total_discounts_paise: int = 0
    net_sales_paise: int = 0
    sales_taxable_value_paise: int = 0
    sales_cgst_paise: int = 0
    sales_sgst_paise: int = 0
    sales_total_gst_paise: int = 0

    # Refunds
    refunds_count: int = 0
    refunds_amount_paise: int = 0
    refund_taxable_value_paise: int = 0
    refund_cgst_paise: int = 0
    refund_sgst_paise: int = 0
    refund_total_gst_paise: int = 0

    # Net figures (Gross Sales less Refunds)
    net_sales_after_refunds_paise: int = 0
    net_taxable_value_paise: int = 0
    net_cgst_paise: int = 0
    net_sgst_paise: int = 0
    net_total_gst_paise: int = 0

    # Breakdowns
    payment_method_totals: Dict[str, int] = field(default_factory=dict)
    channel_totals: Dict[str, int] = field(default_factory=dict)
    hsn_totals: Dict[str, HsnTaxBreakdown] = field(default_factory=dict)

    # Operational metrics
    cancelled_invoices_count: int = 0
    pending_reconciliation_count: int = 0
    is_reconciled: bool = True
    discrepancies: List[str] = field(default_factory=list)

    @property
    def taxable_value_paise(self) -> int:
        """Compatibility property for sales taxable value."""
        return self.sales_taxable_value_paise

    @property
    def cgst_paise(self) -> int:
        """Compatibility property for sales CGST value."""
        return self.sales_cgst_paise

    @property
    def sgst_paise(self) -> int:
        """Compatibility property for sales SGST value."""
        return self.sales_sgst_paise

    @property
    def total_gst_paise(self) -> int:
        """Compatibility property for sales total GST value."""
        return self.sales_total_gst_paise
