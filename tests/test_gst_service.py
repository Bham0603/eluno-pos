"""Unit tests for GST Calculation Engine."""

import unittest
from datetime import date
from decimal import Decimal

from eluno_pos.domain.money import parse_to_paise
from eluno_pos.domain.tax import LineTaxCalculation, TaxRateEntry
from eluno_pos.services.gst_service import GstService, TaxRateNotFoundError, UnknownHsnError


class TestGstService(unittest.TestCase):
    """Test suite for GST calculations, HSN resolution, date effectiveness and odd-paisa splits."""

    def setUp(self) -> None:
        # Service initialized with standard seed data from starter-kit/data/tax_rates.json
        self.gst = GstService()
        self.current_date = date(2026, 10, 20)

    def test_spectacle_frames_at_18_percent(self) -> None:
        # HSN 90031900, Frame price ₹12,998.00 = 1,299,800 paise
        gross_paise = 1299800
        calc = self.gst.calculate_line_tax("90031900", gross_paise, self.current_date)

        self.assertEqual(calc.hsn, "90031900")
        self.assertEqual(calc.rate_bps, 1800)
        self.assertEqual(calc.inclusive_price_paise, 1299800)
        # Taxable value: round_half_up(1299800 * 10000 / 11800) = 1,101,525 paise (₹11,015.25)
        self.assertEqual(calc.taxable_value_paise, 1101525)
        # Total GST: 1299800 - 1101525 = 198,275 paise (₹1,982.75)
        self.assertEqual(calc.total_gst_paise, 198275)
        # CGST: round_half_up(198275 / 2) = 99,138 paise (₹991.38)
        self.assertEqual(calc.cgst_paise, 99138)
        # SGST: 198275 - 99138 = 99,137 paise (₹991.37)
        self.assertEqual(calc.sgst_paise, 99137)
        # Exact reconciliation
        self.assertTrue(calc.is_reconciled)
        self.assertEqual(calc.taxable_value_paise + calc.cgst_paise + calc.sgst_paise, gross_paise)

    def test_ophthalmic_lenses_current_rate_at_5_percent(self) -> None:
        # HSN 90015000 on 2026-10-20 (post 2025-09-22) is 5% (500 bps)
        # Progressive lens ₹9,499.00 = 949,900 paise
        gross_paise = 949900
        calc = self.gst.calculate_line_tax("90015000", gross_paise, self.current_date)

        self.assertEqual(calc.hsn, "90015000")
        self.assertEqual(calc.rate_bps, 500)
        # Taxable: round_half_up(949900 * 10000 / 10500) = 904,667 paise (₹9,046.67)
        self.assertEqual(calc.taxable_value_paise, 904667)
        # GST: 949900 - 904667 = 45,233 paise (₹452.33)
        self.assertEqual(calc.total_gst_paise, 45233)
        # CGST: round_half_up(45233 / 2) = 22,617 paise
        self.assertEqual(calc.cgst_paise, 22617)
        # SGST: 45233 - 22617 = 22,616 paise
        self.assertEqual(calc.sgst_paise, 22616)
        self.assertTrue(calc.is_reconciled)

    def test_historical_ophthalmic_lenses_at_12_percent(self) -> None:
        # Before 2025-09-22, rate was 12% (1200 bps)
        historical_date = date(2025, 5, 15)
        gross_paise = 1000000  # ₹10,000.00
        calc = self.gst.calculate_line_tax("90015000", gross_paise, historical_date)

        self.assertEqual(calc.hsn, "90015000")
        self.assertEqual(calc.rate_bps, 1200)
        # Taxable: round_half_up(1000000 * 10000 / 11200) = 892,857 paise (₹8,928.57)
        self.assertEqual(calc.taxable_value_paise, 892857)
        self.assertEqual(calc.total_gst_paise, 107143)
        self.assertEqual(calc.cgst_paise, 53572)
        self.assertEqual(calc.sgst_paise, 53571)
        self.assertTrue(calc.is_reconciled)

    def test_effective_date_boundaries_for_lenses(self) -> None:
        # Boundary test:
        # 2025-09-21: Last day of 12% rate (inclusive)
        day_before = date(2025, 9, 21)
        rate_before = self.gst.get_rate_entry("90015000", day_before)
        self.assertEqual(rate_before.rate_bps, 1200)

        # 2025-09-22: First day of 5% rate (inclusive)
        switch_day = date(2025, 9, 22)
        rate_switch = self.gst.get_rate_entry("90015000", switch_day)
        self.assertEqual(rate_switch.rate_bps, 500)

    def test_non_corrective_sunglasses_at_18_percent(self) -> None:
        # HSN 90049000, e.g. SG-0001 ₹8,998.00 = 899,800 paise
        gross_paise = 899800
        calc = self.gst.calculate_line_tax("90049000", gross_paise, self.current_date)
        self.assertEqual(calc.hsn, "90049000")
        self.assertEqual(calc.rate_bps, 1800)
        self.assertEqual(calc.taxable_value_paise, 762542)
        self.assertEqual(calc.total_gst_paise, 137258)
        self.assertEqual(calc.cgst_paise, 68629)
        self.assertEqual(calc.sgst_paise, 68629)
        self.assertTrue(calc.is_reconciled)

    def test_corrective_spectacles_as_one_unit_at_5_percent(self) -> None:
        # HSN 90041000
        gross_paise = 500000  # ₹5,000.00
        calc = self.gst.calculate_line_tax("90041000", gross_paise, self.current_date)
        self.assertEqual(calc.hsn, "90041000")
        self.assertEqual(calc.rate_bps, 500)
        self.assertEqual(calc.taxable_value_paise, 476190)
        self.assertEqual(calc.total_gst_paise, 23810)
        self.assertEqual(calc.cgst_paise, 11905)
        self.assertEqual(calc.sgst_paise, 11905)
        self.assertTrue(calc.is_reconciled)

    def test_services_eye_test_and_fitting_at_18_percent(self) -> None:
        # HSN 998599, ₹500.00 = 50,000 paise
        gross_paise = 50000
        calc = self.gst.calculate_line_tax("998599", gross_paise, self.current_date)
        self.assertEqual(calc.hsn, "998599")
        self.assertEqual(calc.rate_bps, 1800)
        self.assertEqual(calc.taxable_value_paise, 42373)
        self.assertEqual(calc.total_gst_paise, 7627)
        self.assertEqual(calc.cgst_paise, 3814)
        self.assertEqual(calc.sgst_paise, 3813)
        self.assertTrue(calc.is_reconciled)

    def test_unknown_hsn_raises_unknown_hsn_error(self) -> None:
        with self.assertRaises(UnknownHsnError) as ctx:
            self.gst.calculate_line_tax("99999999", 100000, self.current_date)
        self.assertIn("not recognized", str(ctx.exception))

    def test_odd_paisa_tax_split_reconciliation(self) -> None:
        # Test case designed to produce an odd GST paisa amount:
        # Net price = ₹1.00 = 100 paise at 18%
        # Taxable = round_half_up(100 * 10000 / 11800) = 85 paise
        # Total GST = 15 paise (ODD!)
        # CGST = round_half_up(15 / 2) = 8 paise
        # SGST = 15 - 8 = 7 paise
        # Check: 85 + 8 + 7 = 100 paise exactly
        calc = self.gst.calculate_line_tax("90031900", 100, self.current_date)
        self.assertEqual(calc.inclusive_price_paise, 100)
        self.assertEqual(calc.taxable_value_paise, 85)
        self.assertEqual(calc.total_gst_paise, 15)
        self.assertEqual(calc.cgst_paise, 8)
        self.assertEqual(calc.sgst_paise, 7)
        self.assertTrue(calc.is_reconciled)

    def test_discounted_line_tax_calculation(self) -> None:
        # Frame gross ₹10,000.00 (1,000,000 paise), ₹1,000.00 discount (100,000 paise)
        # Under Section 15(3) of CGST Act, GST is charged on NET price (900,000 paise)
        net_paise = 1000000 - 100000
        calc = self.gst.calculate_line_tax("90031900", net_paise, self.current_date)

        self.assertEqual(calc.inclusive_price_paise, 900000)
        # Taxable on ₹9,000: round_half_up(900000 * 10000 / 11800) = 762,712 paise (₹7,627.12)
        self.assertEqual(calc.taxable_value_paise, 762712)
        self.assertEqual(calc.total_gst_paise, 137288)
        self.assertEqual(calc.cgst_paise, 68644)
        self.assertEqual(calc.sgst_paise, 68644)
        self.assertTrue(calc.is_reconciled)

    def test_order_level_tax_summary(self) -> None:
        lines = [
            ("90031900", 1101525),  # Frame net
            ("90015000", 904667),   # Lens net
        ]
        summary = self.gst.calculate_order_tax(lines, self.current_date)
        self.assertTrue(summary.is_reconciled)
        self.assertEqual(
            summary.total_taxable_paise + summary.total_cgst_paise + summary.total_sgst_paise,
            summary.total_inclusive_paise,
        )


if __name__ == "__main__":
    unittest.main()

