"""Lens compatibility validation engine."""

from dataclasses import dataclass, field
from typing import List, Optional

from eluno_pos.domain.lens import LensVariant
from eluno_pos.domain.prescription import Prescription


@dataclass(frozen=True)
class ValidationAdvisory:
    """Informational or cautionary advisory that does not hard-block checkout."""
    level: str  # 'info', 'warning'
    message: str
    field: Optional[str] = None


@dataclass
class ValidationResult:
    """Result of validating a prescription against a lens variant."""
    is_compatible: bool
    reasons: List[str] = field(default_factory=list)
    advisories: List[ValidationAdvisory] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        """Check whether any advisories are present."""
        return len(self.advisories) > 0


class LensValidator:
    """Validates optical prescriptions against manufacturer lens limits."""

    @staticmethod
    def validate(prescription: Prescription, lens: LensVariant) -> ValidationResult:
        """Validate an optical prescription against the physical limits of a lens variant.

        Checks:
        1. Sphere (SPH) minimum and maximum for OD and OS.
        2. Absolute Cylinder (CYL) maximum for OD and OS.
        3. Reading addition (ADD) presence and bounds if required by lens type (e.g. progressive/bifocal).
        4. Axis stepping advisory for external paper prescriptions.
        """
        reasons: List[str] = []
        advisories: List[ValidationAdvisory] = []
        limits = lens.limits

        # 1. Sphere (SPH) Check
        for eye_label, eye_data in (("Right eye (OD)", prescription.od), ("Left eye (OS)", prescription.os)):
            if eye_data.sph < limits.sph_min or eye_data.sph > limits.sph_max:
                reasons.append(
                    f"{eye_label} sphere {eye_data.sph:+.2f} D is outside lens '{lens.sku}' supported range "
                    f"[{limits.sph_min:+.2f} D to {limits.sph_max:+.2f} D]. Select a higher refractive index lens."
                )

        # 2. Cylinder (CYL) Check
        for eye_label, eye_data in (("Right eye (OD)", prescription.od), ("Left eye (OS)", prescription.os)):
            if abs(eye_data.cyl) > limits.cyl_abs_max:
                reasons.append(
                    f"{eye_label} cylinder {eye_data.cyl:+.2f} D exceeds lens '{lens.sku}' maximum supported "
                    f"astigmatism limit (±{limits.cyl_abs_max:.2f} D)."
                )

        # 3. Axis Requirement for Astigmatism
        for eye_label, eye_data in (("Right eye (OD)", prescription.od), ("Left eye (OS)", prescription.os)):
            if eye_data.cyl != 0.0 and eye_data.axis is None:
                reasons.append(
                    f"{eye_label} cylinder is {eye_data.cyl:+.2f} D but astigmatism axis angle is missing."
                )
            elif eye_data.axis is not None and not (1 <= eye_data.axis <= 180):
                reasons.append(
                    f"{eye_label} axis {eye_data.axis}° is invalid (must be between 1° and 180°)."
                )

        # 4. Reading Addition (ADD) Check
        if limits.add_required:
            for eye_label, eye_data in (("Right eye (OD)", prescription.od), ("Left eye (OS)", prescription.os)):
                if eye_data.add is None:
                    reasons.append(
                        f"Lens '{lens.sku}' is a {lens.lens_type} lens requiring reading addition (ADD), "
                        f"but {eye_label} prescription has no ADD value."
                    )
                else:
                    if limits.add_min is not None and eye_data.add < limits.add_min:
                        reasons.append(
                            f"{eye_label} ADD {eye_data.add:+.2f} D is below lens minimum ({limits.add_min:+.2f} D)."
                        )
                    if limits.add_max is not None and eye_data.add > limits.add_max:
                        reasons.append(
                            f"{eye_label} ADD {eye_data.add:+.2f} D exceeds lens maximum ({limits.add_max:+.2f} D)."
                        )

        # 4. Axis Grid Advisory (Part E decision: Informational for external prescriptions, non-blocking)
        if prescription.has_off_grid_axis:
            axis_summary = f"OD={prescription.od.axis}°, OS={prescription.os.axis}°"
            if prescription.source == "external":
                advisories.append(
                    ValidationAdvisory(
                        level="warning",
                        message=(
                            f"External paper prescription has off-grid axis value(s) ({axis_summary}). "
                            "In-store refractions use a 5° grid. Confirm with customer before dispensing."
                        ),
                        field="axis",
                    )
                )
            else:
                advisories.append(
                    ValidationAdvisory(
                        level="info",
                        message=f"In-store prescription contains non-standard axis value(s) ({axis_summary}).",
                        field="axis",
                    )
                )

        return ValidationResult(
            is_compatible=len(reasons) == 0,
            reasons=reasons,
            advisories=advisories,
        )

