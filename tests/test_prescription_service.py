"""Unit tests for PrescriptionService."""

import unittest
from datetime import date
from unittest.mock import MagicMock

from eluno_pos.domain.prescription import Prescription
from eluno_pos.services.prescription_service import PrescriptionService


class TestPrescriptionService(unittest.TestCase):
    """Test suite for prescription fetching and validity selection."""

    def setUp(self) -> None:
        self.mock_smt = MagicMock()
        self.service = PrescriptionService(smt_client=self.mock_smt)

    def test_valid_prescription_retrieval(self) -> None:
        # Seed prescription RX5007 for Karthik
        self.mock_smt.get_prescriptions_by_phone.return_value = [
            {
                "rx_id": "RX5007",
                "customer_id": 80001005,
                "phone": "+919000000005",
                "date": "2026-07-10",
                "source": "in_store",
                "optometrist_id": "S107",
                "od": {"sph": -2.5, "cyl": -0.75, "axis": 90, "add": 1.75},
                "os": {"sph": -2.25, "cyl": -0.5, "axis": 85, "add": 1.75},
                "pd": {"binocular": 63, "mono_r": None, "mono_l": None},
                "valid_until": "2027-07-10",
                "notes": None,
            }
        ]

        ref_date = date(2026, 10, 1)
        rx = self.service.get_latest_valid_prescription("+919000000005", reference_date=ref_date)

        self.assertIsNotNone(rx)
        self.assertEqual(rx.rx_id, "RX5007")
        self.assertEqual(rx.customer_id, 80001005)
        # Verify separate OD and OS values
        self.assertEqual(rx.od.sph, -2.5)
        self.assertEqual(rx.od.cyl, -0.75)
        self.assertEqual(rx.od.axis, 90)
        self.assertEqual(rx.od.add, 1.75)

        self.assertEqual(rx.os.sph, -2.25)
        self.assertEqual(rx.os.cyl, -0.5)
        self.assertEqual(rx.os.axis, 85)
        self.assertEqual(rx.os.add, 1.75)

    def test_latest_valid_prescription_selection(self) -> None:
        # Customer with multiple prescriptions (newest first from SMT)
        self.mock_smt.get_prescriptions_by_phone.return_value = [
            {
                "rx_id": "RX5002",
                "customer_id": 80001001,
                "phone": "+919000000001",
                "date": "2026-08-12",
                "source": "external",
                "od": {"sph": 2.5, "cyl": -2.0, "axis": 155, "add": None},
                "os": {"sph": 2.75, "cyl": -2.0, "axis": 155, "add": None},
                "pd": {"binocular": 60},
                "valid_until": "2027-08-12",
            },
            {
                "rx_id": "RX5003",
                "customer_id": 80001001,
                "phone": "+919000000001",
                "date": "2026-05-15",
                "source": "in_store",
                "od": {"sph": 2.0, "cyl": -1.5, "axis": 160, "add": None},
                "os": {"sph": 2.25, "cyl": -1.5, "axis": 160, "add": None},
                "pd": {"binocular": 60},
                "valid_until": "2027-05-15",
            },
        ]

        ref_date = date(2026, 10, 1)
        rx = self.service.get_latest_valid_prescription("+919000000001", reference_date=ref_date)

        self.assertIsNotNone(rx)
        # Must select RX5002 (newest valid) over older RX5003
        self.assertEqual(rx.rx_id, "RX5002")

    def test_expired_prescription_handling(self) -> None:
        # Seed RX5001 for Priya Nair: date: 2025-07-16, valid_until: 2026-07-16
        self.mock_smt.get_prescriptions_by_phone.return_value = [
            {
                "rx_id": "RX5001",
                "customer_id": 80001000,
                "phone": "+919000000000",
                "date": "2025-07-16",
                "source": "in_store",
                "od": {"sph": -4.0, "cyl": 0.0, "axis": None, "add": 2.0},
                "os": {"sph": -3.75, "cyl": 0.0, "axis": None, "add": 2.0},
                "pd": {"binocular": 62},
                "valid_until": "2026-07-16",
            }
        ]

        # On October 1st, 2026, the prescription is expired!
        ref_date = date(2026, 10, 1)
        rx = self.service.get_latest_valid_prescription("+919000000000", reference_date=ref_date)
        self.assertIsNone(rx)

        # Evaluation clearly reports that an expired prescription exists
        latest_valid, expired = self.service.evaluate_customer_prescriptions(
            "+919000000000",
            reference_date=ref_date,
        )
        self.assertIsNone(latest_valid)
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].rx_id, "RX5001")

    def test_future_dated_prescription_is_not_selected(self) -> None:
        """Prescriptions dated after transaction checkout date must never be selected."""
        self.mock_smt.get_prescriptions_by_phone.return_value = [
            {
                "rx_id": "RX_FUTURE",
                "customer_id": 80001001,
                "phone": "+919000000001",
                "date": "2026-11-15",  # Future relative to checkout
                "source": "in_store",
                "od": {"sph": 3.0, "cyl": 0.0, "axis": None, "add": None},
                "os": {"sph": 3.0, "cyl": 0.0, "axis": None, "add": None},
                "pd": {"binocular": 62},
                "valid_until": "2027-11-15",
            },
            {
                "rx_id": "RX_CURRENT_VALID",
                "customer_id": 80001001,
                "phone": "+919000000001",
                "date": "2026-05-10",  # Prior to checkout and unexpired
                "source": "in_store",
                "od": {"sph": 2.5, "cyl": 0.0, "axis": None, "add": None},
                "os": {"sph": 2.5, "cyl": 0.0, "axis": None, "add": None},
                "pd": {"binocular": 62},
                "valid_until": "2027-05-10",
            },
        ]

        checkout_date = date(2026, 10, 20)
        selected = self.service.get_latest_valid_prescription(
            "+919000000001",
            reference_date=checkout_date,
        )

        self.assertIsNotNone(selected)
        # Future-dated RX_FUTURE must be rejected; RX_CURRENT_VALID selected
        self.assertEqual(selected.rx_id, "RX_CURRENT_VALID")

        # If only future-dated prescription exists, return None
        self.mock_smt.get_prescriptions_by_phone.return_value = [
            self.mock_smt.get_prescriptions_by_phone.return_value[0]
        ]
        only_future = self.service.get_latest_valid_prescription(
            "+919000000001",
            reference_date=checkout_date,
        )
        self.assertIsNone(only_future)

    def test_missing_prescription(self) -> None:
        self.mock_smt.get_prescriptions_by_phone.return_value = []

        rx = self.service.get_latest_valid_prescription("+919000000999")
        self.assertIsNone(rx)


if __name__ == "__main__":
    unittest.main()

