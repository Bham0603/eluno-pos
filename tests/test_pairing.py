"""Unit tests for Frame and Lens Pairing."""

import unittest

from eluno_pos.domain.lens import LensLimits, LensVariant
from eluno_pos.domain.pairing import FrameVariant, Pair


class TestPairing(unittest.TestCase):
    """Test suite for minimal frame and lens association via pair_ref."""

    def setUp(self) -> None:
        self.frame = FrameVariant(
            id=91000003,
            product_id=90000003,
            sku="FR-0003",
            title="Tortoise",
            price="10998.00",
            inventory_item_id=92000003,
            hsn="90031900",
            category="optical",
            rx_able=True,
        )

        self.lens = LensVariant(
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
            ),
        )

    def test_complete_pair_association(self) -> None:
        pair = Pair(
            pair_ref="1",
            frame=self.frame,
            lens=self.lens,
            rx_id="RX5007",
            for_customer_id=80001005,
        )

        self.assertEqual(pair.pair_ref, "1")
        self.assertTrue(pair.is_complete_pair)
        self.assertEqual(pair.rx_id, "RX5007")
        self.assertEqual(pair.for_customer_id, 80001005)

        # Inspect frame line-item properties
        frame_props = {p["name"]: p["value"] for p in pair.get_frame_properties()}
        self.assertEqual(frame_props["line_type"], "frame")
        self.assertEqual(frame_props["hsn"], "90031900")
        self.assertEqual(frame_props["pair_ref"], "1")
        self.assertEqual(frame_props["for_customer_id"], "80001005")

        # Inspect lens line-item properties
        lens_props = {p["name"]: p["value"] for p in pair.get_lens_properties()}
        self.assertEqual(lens_props["line_type"], "lens")
        self.assertEqual(lens_props["hsn"], "90015000")
        self.assertEqual(lens_props["pair_ref"], "1")
        self.assertEqual(lens_props["rx_id"], "RX5007")
        self.assertEqual(lens_props["lens_config"], "progressive/1.6/blue_cut")
        self.assertEqual(lens_props["for_customer_id"], "80001005")

    def test_lens_only_pair(self) -> None:
        pair = Pair(
            pair_ref="2",
            frame=None,
            lens=self.lens,
            rx_id="RX5007",
        )
        self.assertFalse(pair.is_complete_pair)
        self.assertEqual(pair.get_frame_properties(), [])
        # 5 properties: line_type, hsn, pair_ref, lens_config, rx_id
        lens_props = pair.get_lens_properties()
        self.assertEqual(len(lens_props), 5)
        prop_dict = {p["name"]: p["value"] for p in lens_props}
        self.assertEqual(prop_dict["line_type"], "lens")
        self.assertEqual(prop_dict["pair_ref"], "2")
        self.assertEqual(prop_dict["rx_id"], "RX5007")

    def test_empty_pair_raises_error(self) -> None:
        with self.assertRaises(ValueError):
            Pair(pair_ref="3", frame=None, lens=None)

    def test_frame_variant_derives_hsn_from_metadata(self) -> None:
        # 1. Optical spectacle frame
        optical_variant = {
            "id": 91000001,
            "product_id": 90000001,
            "sku": "FR-0001",
            "title": "Olive",
            "price": "12998.00",
            "inventory_item_id": 92000001,
            "eluno": {
                "line_type": "frame",
                "hsn": "90031900",
                "category": "optical",
                "rx_able": True,
            },
        }
        frame = FrameVariant.from_shopify_variant(optical_variant)
        self.assertEqual(frame.hsn, "90031900")
        self.assertEqual(frame.category, "optical")

        # 2. Sunglasses frame
        sunglasses_variant = {
            "id": 91000004,
            "product_id": 90000004,
            "sku": "SG-0001",
            "title": "Tortoise",
            "price": "8998.00",
            "inventory_item_id": 92000004,
            "eluno": {
                "line_type": "frame",
                "hsn": "90049000",
                "category": "sunglasses",
                "rx_able": False,
            },
        }
        sunglasses = FrameVariant.from_shopify_variant(sunglasses_variant)
        self.assertEqual(sunglasses.hsn, "90049000")
        self.assertEqual(sunglasses.category, "sunglasses")

        # 3. Missing HSN metadata raises error
        missing_hsn_variant = {
            "id": 91000999,
            "product_id": 90000999,
            "sku": "ERR-0001",
            "eluno": {"category": "optical"},
        }
        with self.assertRaises(ValueError) as ctx:
            FrameVariant.from_shopify_variant(missing_hsn_variant)
        self.assertIn("missing required 'eluno.hsn'", str(ctx.exception))

    def test_lens_variant_derives_hsn_from_metadata(self) -> None:
        lens_variant_data = {
            "id": 91000083,
            "product_id": 90000051,
            "sku": "LN-0043",
            "title": "blue_cut",
            "price": "9499.00",
            "eluno": {
                "line_type": "lens",
                "hsn": "90015000",
                "lens_type": "progressive",
                "index": 1.6,
                "coating": "blue_cut",
                "rx_limits": {
                    "sph_min": -8.0,
                    "sph_max": 6.0,
                    "cyl_abs_max": 4.0,
                    "add_required": True,
                    "add_min": 0.75,
                    "add_max": 3.0,
                },
            },
        }
        lens = LensVariant.from_shopify_variant(lens_variant_data)
        self.assertEqual(lens.hsn, "90015000")

        # Missing HSN raises error
        bad_lens = {
            "id": 91000083,
            "sku": "LN-BAD",
            "eluno": {
                "rx_limits": {"sph_min": -4.0, "sph_max": 4.0, "cyl_abs_max": 2.0},
            },
        }
        with self.assertRaises(ValueError) as ctx:
            LensVariant.from_shopify_variant(bad_lens)
        self.assertIn("missing required 'eluno.hsn'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
