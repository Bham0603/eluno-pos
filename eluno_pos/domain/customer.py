"""Customer domain entity."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Customer:
    """Customer entity representation in Eluno POS."""
    id: int
    first_name: str
    last_name: Optional[str]
    phone: str
    email: Optional[str]
    tags: str
    total_spent: str
    orders_count: int
    home_store: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def full_name(self) -> str:
        """Return formatted full name."""
        if self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name

    @property
    def tag_list(self) -> List[str]:
        """Return tags as a list of trimmed strings."""
        if not self.tags:
            return []
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    def has_tag(self, tag: str) -> bool:
        """Check whether customer has a specific tag."""
        return tag in self.tag_list

    @property
    def is_priority(self) -> bool:
        """Check whether customer has priority status."""
        return self.has_tag("priority")

    @classmethod
    def from_shopify_dict(cls, data: Dict[str, Any]) -> "Customer":
        """Instantiate domain Customer from raw Shopify customer dictionary."""
        eluno_block = data.get("eluno") or {}
        return cls(
            id=data["id"],
            first_name=data.get("first_name") or "",
            last_name=data.get("last_name"),
            phone=data.get("phone") or "",
            email=data.get("email"),
            tags=data.get("tags") or "",
            total_spent=data.get("total_spent") or "0.00",
            orders_count=int(data.get("orders_count") or 0),
            home_store=eluno_block.get("home_store"),
            raw_data=data,
        )

