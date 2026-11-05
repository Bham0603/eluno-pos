"""Application context and dependency injection for Eluno POS CLI."""

from dataclasses import dataclass
from typing import Optional

from eluno_pos.adapters.http_client import HttpClient
from eluno_pos.adapters.shopify_client import ShopifyClient
from eluno_pos.adapters.smt_client import SmtClient
from eluno_pos.adapters.terminal_client import TerminalClient
from eluno_pos.config import AppConfig
from eluno_pos.repositories.invoice_repository import InvoiceRepository
from eluno_pos.services.checkout_service import CheckoutService
from eluno_pos.services.customer_service import CustomerService
from eluno_pos.services.day_end_service import DayEndReportService
from eluno_pos.services.gst_service import GstService
from eluno_pos.services.offer_service import OfferService
from eluno_pos.services.payment_service import PaymentService
from eluno_pos.services.prescription_service import PrescriptionService
from eluno_pos.services.refund_service import RefundService
from eluno_pos.services.shopify_order_service import ShopifyOrderService


@dataclass
class AppContext:
    """Dependency container connecting adapters, repositories, and services."""
    config: AppConfig
    shopify_client: ShopifyClient
    smt_client: SmtClient
    terminal_client: TerminalClient
    repo: InvoiceRepository
    customer_service: CustomerService
    prescription_service: PrescriptionService
    offer_service: OfferService
    gst_service: GstService
    payment_service: PaymentService
    shopify_order_service: ShopifyOrderService
    checkout_service: CheckoutService
    refund_service: RefundService
    day_end_service: DayEndReportService

    @classmethod
    def create(cls, config: Optional[AppConfig] = None) -> "AppContext":
        """Instantiate all adapters, repositories, and services using the specified configuration."""
        cfg = config or AppConfig.from_env()

        # 1. HTTP Clients with configured resilience and timeouts
        http_shopify = HttpClient(
            base_url=cfg.shopify_base_url,
            timeout=cfg.http_timeout_seconds,
            max_retries_429=cfg.max_retries_429,
            backoff_factor=cfg.backoff_factor,
        )
        http_smt = HttpClient(
            base_url=cfg.smt_base_url,
            timeout=cfg.http_timeout_seconds,
            max_retries_429=cfg.max_retries_429,
            backoff_factor=cfg.backoff_factor,
        )
        http_terminal = HttpClient(
            base_url=cfg.terminal_base_url,
            timeout=cfg.http_timeout_seconds,
            max_retries_429=cfg.max_retries_429,
            backoff_factor=cfg.backoff_factor,
        )

        # 2. Adapters
        shopify_client = ShopifyClient(http_shopify)
        smt_client = SmtClient(http_smt)
        terminal_client = TerminalClient(http_terminal, max_retries_503=cfg.max_retries_terminal_503)

        # 3. Repository
        repo = InvoiceRepository(cfg.db_path)

        # 4. Domain & Business Services
        customer_service = CustomerService(shopify_client)
        prescription_service = PrescriptionService(smt_client)
        offer_service = OfferService()
        gst_service = GstService()
        payment_service = PaymentService(terminal_client, repo)
        shopify_order_service = ShopifyOrderService(shopify_client, repo)

        checkout_service = CheckoutService(
            invoice_repository=repo,
            payment_service=payment_service,
            shopify_order_service=shopify_order_service,
            offer_service=offer_service,
            gst_service=gst_service,
        )

        refund_service = RefundService(
            terminal_client=terminal_client,
            shopify_client=shopify_client,
            invoice_repository=repo,
        )

        day_end_service = DayEndReportService(
            invoice_repository=repo,
            shopify_client=shopify_client,
            gst_service=gst_service,
        )

        return cls(
            config=cfg,
            shopify_client=shopify_client,
            smt_client=smt_client,
            terminal_client=terminal_client,
            repo=repo,
            customer_service=customer_service,
            prescription_service=prescription_service,
            offer_service=offer_service,
            gst_service=gst_service,
            payment_service=payment_service,
            shopify_order_service=shopify_order_service,
            checkout_service=checkout_service,
            refund_service=refund_service,
            day_end_service=day_end_service,
        )
