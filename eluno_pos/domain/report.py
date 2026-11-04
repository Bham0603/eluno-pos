"""Day-end store reporting models for Eluno POS."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class HsnTaxBreakdown:
    """Tax breakdown for a specific HSN."""
    hsn: str
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    total_gst_paise: int


@dataclass
class DayEndReport:
    """Consolidated financial day-end report for a store and business date."""
    business_date: str  # YYYY-MM-DD
    store_code: str
    location_id: int
    timezone: str = "Asia/Kolkata"
    posted_sales_count: int = 0
    gross_sales_paise: int = 0
    total_discounts_paise: int = 0
    net_sales_paise: int = 0
    taxable_value_paise: int = 0
    cgst_paise: int = 0
    sgst_paise: int = 0
    total_gst_paise: int = 0
    refunds_count: int = 0
    refunds_amount_paise: int = 0
    net_sales_after_refunds_paise: int = 0
    payment_method_totals: Dict[str, int] = field(default_factory=dict)
    channel_totals: Dict[str, int] = field(default_factory=dict)
    hsn_totals: Dict[str, HsnTaxBreakdown] = field(default_factory=dict)
    cancelled_invoices_count: int = 0
    pending_reconciliation_count: int = 0
    is_reconciled: bool = True
    discrepancies: List[str] = field(default_factory=list)
