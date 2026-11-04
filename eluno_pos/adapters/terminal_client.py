"""Payment terminal client adapter (Razorpay-like)."""

from typing import Any, Dict

from eluno_pos.adapters.exceptions import (
    AmbiguousOperationError,
    ConnectionError,
    ServerError,
    TimeoutError,
)
from eluno_pos.adapters.http_client import HttpClient


class TerminalClient:
    """Client for physical/UPI store payment terminal.

    Contract features:
    - Retries 503 timeouts idempotently using the exact same `order_ref`.
    - Handles payment captures, status verifications, and terminal refunds.
    """

    def __init__(self, http_client: HttpClient, max_retries_503: int = 3) -> None:
        self.http = http_client
        self.max_retries_503 = max_retries_503

    def create_charge(
        self,
        amount_paise: int,
        method: str,
        order_ref: str,
    ) -> Dict[str, Any]:
        """Initiate and capture a payment charge on the terminal.

        SAFETY AND RETRY CONTRACT:
        - The mock server contains a documented synthetic flake: an initial request for a new
          order_ref may return HTTP 503 with {"error": "terminal timeout, retry with the same order_ref"}.
          This 503 occurs PRE-CAPTURE on the server and is safe to retry with the identical order_ref.
        - However, an ambiguous socket timeout (TimeoutError) or connection drop (ConnectionError)
          means the terminal may have already captured funds. The client MUST NOT automatically
          replay the charge request on timeout/connection error.
          Instead, AmbiguousOperationError is raised to prevent double-charging.
        """
        payload = {
            "amount_paise": amount_paise,
            "method": method,
            "order_ref": order_ref,
        }
        try:
            resp = self.http.post(
                "charges",
                json_data=payload,
                allow_retry_429=True,
                allow_retry_503=True,  # Safe ONLY for the mock's documented pre-capture 503
                max_retries_503=self.max_retries_503,
            )
            return resp.data
        except (TimeoutError, ConnectionError) as err:
            raise AmbiguousOperationError(
                message=(
                    f"Terminal charge had an ambiguous outcome ({type(err).__name__}: {err}). "
                    "Do not automatically replay; payment may have been captured on the terminal."
                ),
                status_code=getattr(err, "status_code", None),
                response_body=getattr(err, "response_body", None),
                url=getattr(err, "url", None),
                method="POST",
            ) from err

    def get_charge(self, charge_id: str) -> Dict[str, Any]:
        """Fetch the current status and details of a charge."""
        resp = self.http.get(f"charges/{charge_id}")
        return resp.data

    def create_refund(
        self,
        charge_id: str,
        amount_paise: int,
        reason: str,
    ) -> Dict[str, Any]:
        """Process a refund on the terminal for an existing charge.

        SAFETY & RECONCILIATION RULE:
        Terminal refunds are NOT idempotent in the contract and must NEVER be
        automatically retried on ambiguous outcomes. Doing so risks double-refunding.
        Ambiguous errors are caught and raised as AmbiguousOperationError so callers
        can reconcile status via get_charge(charge_id)['refunded_paise'].
        """
        payload = {
            "charge_id": charge_id,
            "amount_paise": amount_paise,
            "reason": reason,
        }
        try:
            resp = self.http.post(
                "refunds",
                json_data=payload,
                allow_retry_429=True,
                allow_retry_503=False,  # Never automatically retry 503 on refunds
            )
            return resp.data
        except (TimeoutError, ServerError, ConnectionError) as err:
            raise AmbiguousOperationError(
                message=(
                    f"Terminal refund had an ambiguous outcome ({type(err).__name__}: {err}). "
                    "Do not blindly retry; reconcile by checking get_charge(charge_id) to inspect refunded_paise."
                ),
                status_code=getattr(err, "status_code", None),
                response_body=getattr(err, "response_body", None),
                url=getattr(err, "url", None),
                method="POST",
            ) from err

