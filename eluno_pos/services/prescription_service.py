"""Prescription service for retrieving and validating optical prescriptions from SMT."""

from datetime import date
from typing import List, Optional, Tuple

from eluno_pos.adapters.smt_client import SmtClient
from eluno_pos.domain.prescription import Prescription


class PrescriptionService:
    """Service to fetch and select valid optical prescriptions for a customer."""

    def __init__(self, smt_client: SmtClient) -> None:
        self.smt = smt_client

    def get_prescriptions_by_phone(self, phone: str) -> List[Prescription]:
        """Fetch all prescriptions for a phone number, preserving SMT order (newest first)."""
        clean_phone = phone.strip()
        if not clean_phone:
            return []

        raw_rxs = self.smt.get_prescriptions_by_phone(clean_phone)
        return [Prescription.from_smt_dict(r) for r in raw_rxs]

    def get_latest_valid_prescription(
        self,
        phone: str,
        reference_date: Optional[date] = None,
    ) -> Optional[Prescription]:
        """Select the newest valid prescription for a customer as of the reference date.

        Date Rule:
        A prescription is valid if:
        1. It was tested/issued on or before reference_date (date <= reference_date).
        2. Its `valid_until` date is greater than or equal to `reference_date` (inclusive).
        The first prescription matching both criteria in newest-first order is selected.

        Returns:
            The newest Prescription valid on reference_date, or None if:
            - Customer has no prescriptions on file.
            - All prescriptions on file are expired or future-dated.
        """
        ref_date = reference_date or date.today()
        prescriptions = self.get_prescriptions_by_phone(phone)
        sorted_rxs = sorted(prescriptions, key=lambda r: r.date, reverse=True)

        for rx in sorted_rxs:
            if rx.is_valid_on(ref_date):
                return rx

        return None

    def evaluate_customer_prescriptions(
        self,
        phone: str,
        reference_date: Optional[date] = None,
    ) -> Tuple[Optional[Prescription], List[Prescription]]:
        """Inspect all prescriptions and categorize them into (latest_valid, invalid_or_expired_list).

        Useful for POS staff to distinguish between a customer with no test on file
        versus a customer whose last test has passed the 12-month validity window or
        contains invalid dates.
        """
        ref_date = reference_date or date.today()
        prescriptions = self.get_prescriptions_by_phone(phone)
        sorted_rxs = sorted(prescriptions, key=lambda r: r.date, reverse=True)

        latest_valid: Optional[Prescription] = None
        invalid_or_expired: List[Prescription] = []

        for rx in sorted_rxs:
            if rx.is_valid_on(ref_date):
                if latest_valid is None:
                    latest_valid = rx
            else:
                invalid_or_expired.append(rx)

        return latest_valid, invalid_or_expired

