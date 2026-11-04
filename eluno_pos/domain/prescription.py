"""Prescription domain entities."""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class EyePrescription:
    """Optical parameters for a single eye (OD = right eye, OS = left eye)."""
    sph: float
    cyl: float = 0.0
    axis: Optional[int] = None
    add: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EyePrescription":
        """Instantiate eye parameters from dictionary."""
        return cls(
            sph=float(data.get("sph", 0.0)),
            cyl=float(data.get("cyl", 0.0)),
            axis=int(data["axis"]) if data.get("axis") is not None else None,
            add=float(data["add"]) if data.get("add") is not None else None,
        )


@dataclass(frozen=True)
class PupillaryDistance:
    """Pupillary Distance (PD) measurements in millimeters."""
    binocular: Optional[int] = None
    mono_r: Optional[int] = None
    mono_l: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PupillaryDistance":
        """Instantiate PD measurements from dictionary."""
        return cls(
            binocular=data.get("binocular"),
            mono_r=data.get("mono_r"),
            mono_l=data.get("mono_l"),
        )


@dataclass(frozen=True)
class Prescription:
    """Clinical optical prescription entity from SMT."""
    rx_id: str
    customer_id: int
    phone: str
    date: str  # ISO date string (YYYY-MM-DD)
    source: str  # 'in_store' or 'external'
    od: EyePrescription
    os: EyePrescription
    pd: PupillaryDistance
    valid_until: str  # ISO date string (YYYY-MM-DD)
    optometrist_id: Optional[str] = None
    notes: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict, repr=False)

    def is_valid_on(self, reference_date: date) -> bool:
        """Check whether prescription is valid on the given reference date (inclusive).

        Rules:
        1. Must have been tested/issued on or before the reference date (date <= reference_date).
           Future-dated prescriptions are not yet valid for dispensing.
        2. Must not be expired as of the reference date (reference_date <= valid_until).
        """
        ref_iso = reference_date.isoformat()
        return self.date <= ref_iso <= self.valid_until

    @property
    def has_off_grid_axis(self) -> bool:
        """Check whether either eye has an axis that does not align to the 5-degree grid."""
        for eye in (self.od, self.os):
            if eye.axis is not None and eye.axis % 5 != 0:
                return True
        return False

    @classmethod
    def from_smt_dict(cls, data: Dict[str, Any]) -> "Prescription":
        """Instantiate domain Prescription from SMT raw dictionary."""
        rx_date = data.get("date") or data.get("tested_at")
        if not rx_date:
            raise ValueError(f"Prescription {data.get('rx_id')} is missing required 'date' field")

        return cls(
            rx_id=data["rx_id"],
            customer_id=int(data["customer_id"]),
            phone=data.get("phone", ""),
            date=str(rx_date),
            source=data.get("source", "in_store"),
            optometrist_id=data.get("optometrist_id"),
            od=EyePrescription.from_dict(data.get("od") or {}),
            os=EyePrescription.from_dict(data.get("os") or {}),
            pd=PupillaryDistance.from_dict(data.get("pd") or {}),
            valid_until=data["valid_until"],
            notes=data.get("notes"),
            raw_data=data,
        )

