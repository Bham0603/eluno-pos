"""Store Management Tool (SMT) client adapter."""

from typing import Any, Dict, List, Optional

from eluno_pos.adapters.http_client import HttpClient


class SmtClient:
    """Client for Store Management Tool (SMT) optical clinical and staff services."""

    def __init__(self, http_client: HttpClient) -> None:
        self.http = http_client

    def get_prescriptions_by_phone(self, phone: str) -> List[Dict[str, Any]]:
        """Fetch clinical optical prescriptions for a customer phone number.

        Returned list is sorted newest first.
        """
        # Ensure query matches phone format
        resp = self.http.get("prescriptions", params={"phone": phone})
        return resp.data.get("prescriptions", [])

    def get_staff(self, store_code: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch staff members, optionally filtered by store code (e.g. 'HSR', 'MM')."""
        params: Dict[str, Any] = {}
        if store_code:
            params["store"] = store_code
        resp = self.http.get("staff", params=params)
        return resp.data.get("staff", [])

