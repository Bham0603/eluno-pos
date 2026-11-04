"""Lens and Lens Limits domain entities."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class LensLimits:
    """Manufacturing limits for a specific lens variant from lab metadata."""
    sph_min: float
    sph_max: float
    cyl_abs_max: float
    add_required: bool
    add_min: Optional[float] = None
    add_max: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LensLimits":
        """Instantiate limits from rx_limits dictionary."""
        return cls(
            sph_min=float(data["sph_min"]),
            sph_max=float(data["sph_max"]),
            cyl_abs_max=float(data["cyl_abs_max"]),
            add_required=bool(data.get("add_required", False)),
            add_min=float(data["add_min"]) if data.get("add_min") is not None else None,
            add_max=float(data["add_max"]) if data.get("add_max") is not None else None,
        )

    @classmethod
    def from_metafields(cls, data: Dict[str, Any]) -> "LensLimits":
        """Extract LensLimits from variant dictionary, eluno metadata, or rx_limits directly.

        Handles:
        1. Full variant dict: data.get("eluno", {}).get("rx_limits")
        2. Eluno metadata block: data.get("rx_limits")
        3. Direct rx_limits dict: data containing 'sph_min'
        4. Metafields wrapper dict if provided.
        """
        if "eluno" in data and isinstance(data["eluno"], dict):
            rx_limits = data["eluno"].get("rx_limits")
        elif "rx_limits" in data and isinstance(data["rx_limits"], dict):
            rx_limits = data["rx_limits"]
        elif "sph_min" in data:
            rx_limits = data
        elif "metafields" in data and isinstance(data["metafields"], dict):
            mf = data["metafields"]
            rx_limits = mf.get("eluno", {}).get("rx_limits") if isinstance(mf.get("eluno"), dict) else mf.get("rx_limits")
        else:
            rx_limits = None

        if not rx_limits or not isinstance(rx_limits, dict):
            raise ValueError("Missing or invalid 'eluno.rx_limits' in lens metadata")

        return cls.from_dict(rx_limits)


@dataclass(frozen=True)
class LensVariant:
    """Lens variant representation."""
    id: int
    product_id: int
    sku: str
    title: str
    price: str
    lens_type: str
    index: float
    coating: str
    hsn: str
    limits: LensLimits
    lab_tat_days: int = 3
    raw_data: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def config_string(self) -> str:
        """Formatted configuration string (e.g. 'progressive/1.6/blue_cut')."""
        return f"{self.lens_type}/{self.index}/{self.coating}"

    @classmethod
    def from_shopify_variant(cls, data: Dict[str, Any]) -> "LensVariant":
        """Instantiate LensVariant from raw Shopify variant dictionary."""
        eluno = data.get("eluno") or {}
        rx_limits_data = eluno.get("rx_limits")
        if not rx_limits_data:
            raise ValueError(f"Variant {data.get('sku', data.get('id'))} is missing eluno.rx_limits metadata")

        hsn = eluno.get("hsn")
        if not hsn:
            raise ValueError(f"Variant {data.get('sku', data.get('id'))} is missing required 'eluno.hsn' metadata")

        return cls(
            id=data["id"],
            product_id=data["product_id"],
            sku=data["sku"],
            title=data.get("title") or "",
            price=data.get("price") or "0.00",
            lens_type=eluno.get("lens_type", "single_vision"),
            index=float(eluno.get("index", 1.5)),
            coating=eluno.get("coating", "standard"),
            hsn=str(hsn),
            limits=LensLimits.from_metafields(data),
            lab_tat_days=int(eluno.get("lab_tat_days", 3)),
            raw_data=data,
        )

