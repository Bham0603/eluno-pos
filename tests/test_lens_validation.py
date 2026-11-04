"""Unit tests for lens compatibility validation engine."""

import unittest

from eluno_pos.domain.lens import LensLimits, LensVariant
from eluno_pos.domain.prescription import EyePrescription, Prescription, PupillaryDistance
from eluno_pos.domain.validation import LensValidator


class TestLensValidation(unittest.TestCase):
    """Test suite for prescription validation against lens manufacturing limits."""

    def setUp(self) -> None:
        # Standard Single Vision 1.50 Lens
        self.single_vision_lens = LensVariant(
            id=91000041,
            product_id=90000041,
            sku="LN-0001",
            title="standard",
            price="999.00",
            lens_type="single_vision",
            index=1.5,
            coating="standard",
            hsn="90015000",
            limits=LensLimits(
                sph_min=-4.0,
                sph_max=4.0,
                cyl_abs_max=2.0,
                add_required=False,
            ),
        )

        # High-Index Progressive 1.60 Lens
        self.progressive_lens = LensVariant(
            id=91000083,
            product_id=90000051,
            sku="LN-0043",
            title="blue_cut",
            price="9499.00",
            lens_type="progressive",
            index=1.6,
            coating="blue_cut",
            hsn="90015000",
            limits=LensLimits(
                sph_min=-6.0,
                sph_max=6.0,
                cyl_abs_max=3.0,
                add_required=True,
                add_min=0.75,
                add_max=3.0,
            ),
        )

        # Baseline Compatible Prescription (Karthik Iyer RX5007)
        self.base_rx = Prescription(
            rx_id="RX5007",
            customer_id=80001005,
            phone="+919000000005",
            date="2026-07-10",
            source="in_store",
            od=EyePrescription(sph=-2.5, cyl=-0.75, axis=90, add=1.75),
            os=EyePrescription(sph=-2.25, cyl=-0.5, axis=85, add=1.75),
            pd=PupillaryDistance(binocular=63),
            valid_until="2027-07-10",
        )

    def test_compatible_progressive_lens(self) -> None:
        result = LensValidator.validate(self.base_rx, self.progressive_lens)
        self.assertTrue(result.is_compatible)
        self.assertEqual(len(result.reasons), 0)
        self.assertFalse(result.has_warnings)

    def test_compatible_single_vision_lens(self) -> None:
        sv_rx = Prescription(
            rx_id="RX_SV",
            customer_id=80001001,
            phone="+919000000001",
            date="2026-01-01",
            source="in_store",
            od=EyePrescription(sph=-2.0, cyl=-1.0, axis=90, add=None),
            os=EyePrescription(sph=-2.25, cyl=-1.0, axis=90, add=None),
            pd=PupillaryDistance(binocular=62),
            valid_until="2027-01-01",
        )
        result = LensValidator.validate(sv_rx, self.single_vision_lens)
        self.assertTrue(result.is_compatible)
        self.assertEqual(len(result.reasons), 0)

    def test_sph_outside_supported_range(self) -> None:
        # OD sphere is -5.50 D, which exceeds the -4.00 D limit of 1.50 index
        high_power_rx = Prescription(
            rx_id="RX_HIGH_MYOPIA",
            customer_id=80001001,
            phone="+919000000001",
            date="2026-01-01",
            source="in_store",
            od=EyePrescription(sph=-5.5, cyl=-0.5, axis=90, add=None),
            os=EyePrescription(sph=-3.0, cyl=-0.5, axis=90, add=None),
            pd=PupillaryDistance(binocular=62),
            valid_until="2027-01-01",
        )

        result = LensValidator.validate(high_power_rx, self.single_vision_lens)
        self.assertFalse(result.is_compatible)
        self.assertEqual(len(result.reasons), 1)
        self.assertIn("Right eye (OD) sphere -5.50 D is outside lens 'LN-0001' supported range", result.reasons[0])
        self.assertIn("Select a higher refractive index lens", result.reasons[0])

    def test_cyl_exceeding_supported_range(self) -> None:
        # OS cylinder is -2.75 D, which exceeds cyl_abs_max 2.00 D
        high_astigmatism_rx = Prescription(
            rx_id="RX_HIGH_CYL",
            customer_id=80001001,
            phone="+919000000001",
            date="2026-01-01",
            source="in_store",
            od=EyePrescription(sph=-2.0, cyl=-1.0, axis=90, add=None),
            os=EyePrescription(sph=-2.0, cyl=-2.75, axis=90, add=None),
            pd=PupillaryDistance(binocular=62),
            valid_until="2027-01-01",
        )

        result = LensValidator.validate(high_astigmatism_rx, self.single_vision_lens)
        self.assertFalse(result.is_compatible)
        self.assertEqual(len(result.reasons), 1)
        self.assertIn("Left eye (OS) cylinder -2.75 D exceeds lens 'LN-0001' maximum supported astigmatism limit", result.reasons[0])

    def test_missing_required_add_for_progressive_lens(self) -> None:
        # Prescription with no ADD submitted for a progressive lens
        no_add_rx = Prescription(
            rx_id="RX_NO_ADD",
            customer_id=80001005,
            phone="+919000000005",
            date="2026-07-10",
            source="in_store",
            od=EyePrescription(sph=-2.5, cyl=-0.75, axis=90, add=None),
            os=EyePrescription(sph=-2.25, cyl=-0.5, axis=85, add=None),
            pd=PupillaryDistance(binocular=63),
            valid_until="2027-07-10",
        )

        result = LensValidator.validate(no_add_rx, self.progressive_lens)
        self.assertFalse(result.is_compatible)
        # Both eyes flagged for missing ADD
        self.assertEqual(len(result.reasons), 2)
        self.assertIn("requiring reading addition (ADD)", result.reasons[0])
        self.assertIn("Right eye (OD) prescription has no ADD value", result.reasons[0])
        self.assertIn("Left eye (OS) prescription has no ADD value", result.reasons[1])

    def test_external_off_grid_axis_produces_advisory_without_blocking(self) -> None:
        # Seed prescription RX5999 (OD axis 87, OS axis 92)
        external_rx = Prescription(
            rx_id="RX5999",
            customer_id=80001003,
            phone="+919000000003",
            date="2026-09-30",
            source="external",
            od=EyePrescription(sph=-2.25, cyl=-0.75, axis=87, add=None),
            os=EyePrescription(sph=-2.0, cyl=-0.5, axis=92, add=None),
            pd=PupillaryDistance(binocular=63),
            valid_until="2027-09-30",
        )

        result = LensValidator.validate(external_rx, self.single_vision_lens)
        # Must NOT hard-block billing
        self.assertTrue(result.is_compatible)
        self.assertEqual(len(result.reasons), 0)
        # Must surface informational advisory for store staff
        self.assertTrue(result.has_warnings)
        self.assertEqual(len(result.advisories), 1)
        self.assertEqual(result.advisories[0].level, "warning")
        self.assertIn("External paper prescription has off-grid axis value(s)", result.advisories[0].message)
        self.assertIn("OD=87°, OS=92°", result.advisories[0].message)

    def test_missing_lens_metadata_raises_value_error(self) -> None:
        raw_variant_missing_limits = {
            "id": 999,
            "product_id": 888,
            "sku": "BAD_LENS",
            "eluno": {"lens_type": "single_vision", "hsn": "90015000"},  # Missing rx_limits!
        }
        with self.assertRaises(ValueError) as ctx:
            LensVariant.from_shopify_variant(raw_variant_missing_limits)
        self.assertIn("missing eluno.rx_limits metadata", str(ctx.exception))

    def test_add_below_minimum(self) -> None:
        # progressive_lens requires add in [0.75, 3.0]
        low_add_rx = Prescription(
            rx_id="RX_LOW_ADD",
            customer_id=80001005,
            phone="+919000000005",
            date="2026-07-10",
            source="in_store",
            od=EyePrescription(sph=-2.5, cyl=-0.75, axis=90, add=0.50),  # < 0.75
            os=EyePrescription(sph=-2.25, cyl=-0.5, axis=85, add=1.75),
            pd=PupillaryDistance(binocular=63),
            valid_until="2027-07-10",
        )
        result = LensValidator.validate(low_add_rx, self.progressive_lens)
        self.assertFalse(result.is_compatible)
        self.assertEqual(len(result.reasons), 1)
        self.assertIn("Right eye (OD) ADD +0.50 D is below lens minimum (+0.75 D)", result.reasons[0])

    def test_add_above_maximum(self) -> None:
        # progressive_lens requires add in [0.75, 3.0]
        high_add_rx = Prescription(
            rx_id="RX_HIGH_ADD",
            customer_id=80001005,
            phone="+919000000005",
            date="2026-07-10",
            source="in_store",
            od=EyePrescription(sph=-2.5, cyl=-0.75, axis=90, add=1.75),
            os=EyePrescription(sph=-2.25, cyl=-0.5, axis=85, add=3.50),  # > 3.0
            pd=PupillaryDistance(binocular=63),
            valid_until="2027-07-10",
        )
        result = LensValidator.validate(high_add_rx, self.progressive_lens)
        self.assertFalse(result.is_compatible)
        self.assertEqual(len(result.reasons), 1)
        self.assertIn("Left eye (OS) ADD +3.50 D exceeds lens maximum (+3.00 D)", result.reasons[0])

    def test_missing_axis_when_cylinder_is_non_zero(self) -> None:
        # OD has non-zero cylinder (-1.00) but no axis specified
        missing_axis_rx = Prescription(
            rx_id="RX_NO_AXIS",
            customer_id=80001001,
            phone="+919000000001",
            date="2026-01-01",
            source="in_store",
            od=EyePrescription(sph=-2.0, cyl=-1.0, axis=None, add=None),
            os=EyePrescription(sph=-2.0, cyl=0.0, axis=None, add=None),
            pd=PupillaryDistance(binocular=62),
            valid_until="2027-01-01",
        )
        result = LensValidator.validate(missing_axis_rx, self.single_vision_lens)
        self.assertFalse(result.is_compatible)
        self.assertEqual(len(result.reasons), 1)
        self.assertIn("Right eye (OD) cylinder is -1.00 D but astigmatism axis angle is missing", result.reasons[0])

    def test_zero_cylinder_with_no_axis_is_compatible(self) -> None:
        # Pure spherical prescription with zero cylinder and no axis
        spherical_rx = Prescription(
            rx_id="RX_SPHERICAL",
            customer_id=80001001,
            phone="+919000000001",
            date="2026-01-01",
            source="in_store",
            od=EyePrescription(sph=-2.0, cyl=0.0, axis=None, add=None),
            os=EyePrescription(sph=-3.0, cyl=0.0, axis=None, add=None),
            pd=PupillaryDistance(binocular=62),
            valid_until="2027-01-01",
        )
        result = LensValidator.validate(spherical_rx, self.single_vision_lens)
        self.assertTrue(result.is_compatible)
        self.assertEqual(len(result.reasons), 0)

    def test_sph_and_cyl_boundary_values(self) -> None:
        # Test exact boundary limits: sph_min=-4.0, sph_max=4.0, cyl_abs_max=2.0
        boundary_rx = Prescription(
            rx_id="RX_BOUNDARY",
            customer_id=80001001,
            phone="+919000000001",
            date="2026-01-01",
            source="in_store",
            od=EyePrescription(sph=-4.0, cyl=-2.0, axis=90, add=None),  # Exact lower SPH & max CYL
            os=EyePrescription(sph=4.0, cyl=-2.0, axis=180, add=None),  # Exact upper SPH & max CYL
            pd=PupillaryDistance(binocular=62),
            valid_until="2027-01-01",
        )
        result = LensValidator.validate(boundary_rx, self.single_vision_lens)
        self.assertTrue(result.is_compatible)
        self.assertEqual(len(result.reasons), 0)

    def test_independent_od_and_os_validation(self) -> None:
        # OD is in range (-2.0), OS exceeds sphere range (-5.0 exceeds -4.0)
        os_fail_rx = Prescription(
            rx_id="RX_OS_FAIL",
            customer_id=80001001,
            phone="+919000000001",
            date="2026-01-01",
            source="in_store",
            od=EyePrescription(sph=-2.0, cyl=0.0, axis=None, add=None),
            os=EyePrescription(sph=-5.0, cyl=0.0, axis=None, add=None),
            pd=PupillaryDistance(binocular=62),
            valid_until="2027-01-01",
        )
        result_os = LensValidator.validate(os_fail_rx, self.single_vision_lens)
        self.assertFalse(result_os.is_compatible)
        self.assertEqual(len(result_os.reasons), 1)
        self.assertIn("Left eye (OS)", result_os.reasons[0])
        self.assertNotIn("Right eye (OD)", result_os.reasons[0])

        # Conversely, OD exceeds range and OS is in range
        od_fail_rx = Prescription(
            rx_id="RX_OD_FAIL",
            customer_id=80001001,
            phone="+919000000001",
            date="2026-01-01",
            source="in_store",
            od=EyePrescription(sph=5.0, cyl=0.0, axis=None, add=None),
            os=EyePrescription(sph=2.0, cyl=0.0, axis=None, add=None),
            pd=PupillaryDistance(binocular=62),
            valid_until="2027-01-01",
        )
        result_od = LensValidator.validate(od_fail_rx, self.single_vision_lens)
        self.assertFalse(result_od.is_compatible)
        self.assertEqual(len(result_od.reasons), 1)
        self.assertIn("Right eye (OD)", result_od.reasons[0])
        self.assertNotIn("Left eye (OS)", result_od.reasons[0])

    def test_lens_limits_from_metafields_resolution(self) -> None:
        # 1. Full variant dict with eluno.rx_limits
        variant_dict = {
            "id": 91000041,
            "eluno": {
                "hsn": "90015000",
                "rx_limits": {
                    "sph_min": -4.0,
                    "sph_max": 4.0,
                    "cyl_abs_max": 2.0,
                    "add_required": False,
                },
            },
        }
        limits_from_variant = LensLimits.from_metafields(variant_dict)
        self.assertEqual(limits_from_variant.sph_min, -4.0)

        # 2. Directly passing eluno dictionary
        limits_from_eluno = LensLimits.from_metafields(variant_dict["eluno"])
        self.assertEqual(limits_from_eluno.sph_max, 4.0)

        # 3. Directly passing rx_limits dict
        limits_from_rx_limits = LensLimits.from_metafields(variant_dict["eluno"]["rx_limits"])
        self.assertEqual(limits_from_rx_limits.cyl_abs_max, 2.0)


if __name__ == "__main__":
    unittest.main()

