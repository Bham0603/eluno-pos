"""GST Calculation Service for Eluno POS."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from eluno_pos.domain.money import round_half_up
from eluno_pos.domain.tax import LineTaxCalculation, OrderTaxSummary, TaxRateEntry


class UnknownHsnError(ValueError):
    """Raised when an unrecognized HSN is submitted for tax calculation."""
    pass


class TaxRateNotFoundError(ValueError):
    """Raised when no active tax rate exists for an HSN on a given transaction date."""
    pass


class GstService:
    """Service to evaluate and back-calculate Indian Goods & Services Tax (GST) by HSN."""

    DEFAULT_TAX_RATES_FILE = Path("starter-kit/data/tax_rates.json")

    def __init__(
        self,
        rates_data: Optional[List[Dict[str, Any]]] = None,
        file_path: Optional[Path] = None,
    ) -> None:
        self.entries: List[TaxRateEntry] = []
        if rates_data is not None:
            self._load_from_data(rates_data)
        else:
            path = file_path or self.DEFAULT_TAX_RATES_FILE
            self._load_from_file(path)

    def _load_from_data(self, rates_list: List[Dict[str, Any]]) -> None:
        self.entries = [TaxRateEntry.from_dict(r) for r in rates_list]

    def _load_from_file(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"Tax rates configuration not found at {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rates_list = data.get("rates", [])
        self._load_from_data(rates_list)

    def get_rate_entry(self, hsn: str, transaction_date: date) -> TaxRateEntry:
        """Find the applicable GST tax rate for an HSN on a given date.

        Raises:
            UnknownHsnError: If the HSN is not present in the rate registry.
            TaxRateNotFoundError: If the HSN is known but no rate is active on the date.
        """
        clean_hsn = str(hsn).strip()
        hsn_entries = [e for e in self.entries if e.hsn == clean_hsn]

        if not hsn_entries:
            raise UnknownHsnError(f"HSN '{clean_hsn}' is not recognized in tax rate registry")

        for entry in hsn_entries:
            if entry.is_effective_on(transaction_date):
                return entry

        raise TaxRateNotFoundError(
            f"No active tax rate found for HSN '{clean_hsn}' on transaction date {transaction_date.isoformat()}"
        )

    def calculate_line_tax(
        self,
        hsn: str,
        net_inclusive_paise: int,
        transaction_date: Optional[date] = None,
    ) -> LineTaxCalculation:
        """Back-calculate GST from a net GST-inclusive line price in integer paise.

        In Indian retail, floor prices (MRP) are inclusive of GST.
        Under Section 15(3) of CGST Act, 2017, tax is calculated on the net transaction
        value after deducting all trade and promotional discounts.

        Accounting Formulations:
            Taxable Value = round_half_up(net_inclusive_paise * 10000 / (10000 + rate_bps))
            Total GST     = net_inclusive_paise - Taxable Value
            CGST          = round_half_up(Total GST / 2)
            SGST          = Total GST - CGST

        Guarantees:
            Taxable Value + CGST + SGST == net_inclusive_paise (exact to the paisa).
        """
        tx_date = transaction_date or date.today()
        rate_entry = self.get_rate_entry(hsn, tx_date)

        if net_inclusive_paise <= 0:
            return LineTaxCalculation(
                hsn=rate_entry.hsn,
                description=rate_entry.description,
                rate_bps=rate_entry.rate_bps,
                inclusive_price_paise=0,
                taxable_value_paise=0,
                cgst_paise=0,
                sgst_paise=0,
                total_gst_paise=0,
            )

        rate_bps = rate_entry.rate_bps
        # Taxable Value = A / (1 + rate) = A * 10000 / (10000 + rate_bps)
        taxable_dec = (Decimal(net_inclusive_paise) * Decimal(10000)) / Decimal(10000 + rate_bps)
        taxable_value = round_half_up(taxable_dec)

        # Back out GST as exact difference
        total_gst = net_inclusive_paise - taxable_value

        # Equal 50/50 split for intra-state supply (CGST and SGST)
        # Any odd paisa goes to CGST via half-up rounding, remainder to SGST
        cgst = round_half_up(Decimal(total_gst) / Decimal(2))
        sgst = total_gst - cgst

        calc = LineTaxCalculation(
            hsn=rate_entry.hsn,
            description=rate_entry.description,
            rate_bps=rate_bps,
            inclusive_price_paise=net_inclusive_paise,
            taxable_value_paise=taxable_value,
            cgst_paise=cgst,
            sgst_paise=sgst,
            total_gst_paise=total_gst,
        )

        assert calc.is_reconciled, f"Line tax failed reconciliation: {calc}"
        return calc

    def calculate_order_tax(
        self,
        lines: List[Tuple[str, int]],  # List of (hsn, net_inclusive_paise)
        transaction_date: Optional[date] = None,
    ) -> OrderTaxSummary:
        """Calculate and consolidate tax across all lines in an order."""
        tx_date = transaction_date or date.today()
        line_taxes: List[LineTaxCalculation] = []

        total_inclusive = 0
        total_taxable = 0
        total_cgst = 0
        total_sgst = 0
        total_gst = 0

        for hsn, net_paise in lines:
            line_calc = self.calculate_line_tax(hsn, net_paise, tx_date)
            line_taxes.append(line_calc)
            total_inclusive += line_calc.inclusive_price_paise
            total_taxable += line_calc.taxable_value_paise
            total_cgst += line_calc.cgst_paise
            total_sgst += line_calc.sgst_paise
            total_gst += line_calc.total_gst_paise

        summary = OrderTaxSummary(
            total_inclusive_paise=total_inclusive,
            total_taxable_paise=total_taxable,
            total_cgst_paise=total_cgst,
            total_sgst_paise=total_sgst,
            total_gst_paise=total_gst,
            line_taxes=line_taxes,
        )
        assert summary.is_reconciled, f"Order tax summary failed reconciliation: {summary}"
        return summary

