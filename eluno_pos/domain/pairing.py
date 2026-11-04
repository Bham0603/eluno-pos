"""Frame variant and Pair domain entities."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from eluno_pos.domain.lens import LensVariant


@dataclass(frozen=True)
class FrameVariant:
    """Frame variant representation."""
    id: int
    product_id: int
    sku: str
    title: str
    price: str
    inventory_item_id: Optional[int]
    hsn: str
    category: str = "optical"
    rx_able: bool = True
    size: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_shopify_variant(cls, data: Dict[str, Any]) -> "FrameVariant":
        """Instantiate FrameVariant from raw Shopify variant dictionary."""
        eluno = data.get("eluno") or {}
        hsn = eluno.get("hsn")
        if not hsn:
            raise ValueError(f"Variant {data.get('sku', data.get('id'))} is missing required 'eluno.hsn' metadata")

        return cls(
            id=data["id"],
            product_id=data["product_id"],
            sku=data["sku"],
            title=data.get("title") or "",
            price=data.get("price") or "0.00",
            inventory_item_id=data.get("inventory_item_id"),
            hsn=str(hsn),
            category=eluno.get("category", "optical"),
            rx_able=bool(eluno.get("rx_able", True)),
            size=eluno.get("size"),
            raw_data=data,
        )


@dataclass(frozen=True)
class Pair:
    """Pair association linking a frame, a lens, a prescription, and an optional customer."""
    pair_ref: str
    frame: Optional[FrameVariant] = None
    lens: Optional[LensVariant] = None
    rx_id: Optional[str] = None
    for_customer_id: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.frame and not self.lens:
            raise ValueError("A Pair must contain at least a frame or a lens")

    @property
    def is_complete_pair(self) -> bool:
        """Check whether this pair contains both a frame and a lens."""
        return self.frame is not None and self.lens is not None

    def get_frame_properties(self) -> List[Dict[str, str]]:
        """Generate Shopify line-item properties for the frame line."""
        if not self.frame:
            return []
        props = [
            {"name": "line_type", "value": "frame"},
            {"name": "hsn", "value": self.frame.hsn},
            {"name": "pair_ref", "value": str(self.pair_ref)},
        ]
        if self.for_customer_id is not None:
            props.append({"name": "for_customer_id", "value": str(self.for_customer_id)})
        return props

    def get_lens_properties(self) -> List[Dict[str, str]]:
        """Generate Shopify line-item properties for the lens line."""
        if not self.lens:
            return []
        props = [
            {"name": "line_type", "value": "lens"},
            {"name": "hsn", "value": self.lens.hsn},
            {"name": "pair_ref", "value": str(self.pair_ref)},
            {"name": "lens_config", "value": self.lens.config_string},
        ]
        if self.rx_id:
            props.append({"name": "rx_id", "value": self.rx_id})
        if self.for_customer_id is not None:
            props.append({"name": "for_customer_id", "value": str(self.for_customer_id)})
        return props

