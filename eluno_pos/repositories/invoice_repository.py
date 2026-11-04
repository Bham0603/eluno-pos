"""SQLite repository for invoice sequencing, state persistence, and audit logging."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import threading
from typing import Any, Dict, Generator, List, Optional

from eluno_pos.domain.invoice import Invoice, InvoiceLine, InvoiceStatus
from eluno_pos.domain.payment import PaymentRecord, PaymentStatus


class InvoiceRepository:
    """Thread-safe SQLite repository managing invoice sequences and transaction audit state."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Provide a connection with busy timeout and Row factory."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,  # Enable explicit transaction control (BEGIN IMMEDIATE)
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _now_iso(self) -> str:
        """Current timestamp in standard ISO-8601 UTC format."""
        return datetime.now(timezone.utc).isoformat()

    def _init_db(self) -> None:
        """Create necessary schema tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS invoice_sequences (
                    store_code TEXT NOT NULL,
                    financial_year TEXT NOT NULL,
                    next_seq INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (store_code, financial_year)
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    invoice_no TEXT PRIMARY KEY,
                    store_code TEXT NOT NULL,
                    financial_year TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    customer_id INTEGER,
                    gross_paise INTEGER NOT NULL,
                    discount_paise INTEGER NOT NULL,
                    net_paise INTEGER NOT NULL,
                    taxable_paise INTEGER NOT NULL,
                    tax_paise INTEGER NOT NULL,
                    checkout_ref TEXT UNIQUE NOT NULL,
                    terminal_charge_id TEXT,
                    shopify_order_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS invoice_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_no TEXT NOT NULL,
                    line_id TEXT NOT NULL,
                    line_type TEXT NOT NULL,
                    variant_id INTEGER NOT NULL,
                    sku TEXT NOT NULL,
                    title TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    unit_price_paise INTEGER NOT NULL,
                    gross_paise INTEGER NOT NULL,
                    discount_paise INTEGER NOT NULL,
                    net_paise INTEGER NOT NULL,
                    hsn TEXT NOT NULL,
                    pair_ref TEXT,
                    rx_id TEXT,
                    lens_config TEXT,
                    properties_json TEXT,
                    FOREIGN KEY(invoice_no) REFERENCES invoices(invoice_no)
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS payment_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkout_ref TEXT NOT NULL,
                    invoice_no TEXT NOT NULL,
                    order_ref TEXT UNIQUE NOT NULL,
                    amount_paise INTEGER NOT NULL,
                    method TEXT NOT NULL,
                    status TEXT NOT NULL,
                    charge_id TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS shopify_order_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_no TEXT UNIQUE NOT NULL,
                    checkout_ref TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    shopify_order_id INTEGER,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    def allocate_invoice(
        self,
        store_code: str,
        financial_year: str,
        checkout_ref: str,
        customer_id: Optional[int],
        gross_paise: int,
        discount_paise: int,
        net_paise: int,
        taxable_paise: int,
        tax_paise: int,
        lines: List[InvoiceLine],
    ) -> Invoice:
        """Atomically allocate the next sequential invoice number and persist invoice records.

        Uses BEGIN IMMEDIATE transaction to guarantee atomic serialization across concurrent threads/processes.
        Format: ELU/{store_code}/{financial_year}/{seq:05d}
        Initial state: PENDING_RECONCILIATION
        """
        now = self._now_iso()
        with self._lock:  # Process-level mutex combined with SQLite BEGIN IMMEDIATE
            with self._get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    cursor = conn.execute(
                        "SELECT next_seq FROM invoice_sequences WHERE store_code = ? AND financial_year = ?;",
                        (store_code, financial_year),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        seq = 1
                        next_seq = 2
                        conn.execute(
                            "INSERT INTO invoice_sequences (store_code, financial_year, next_seq, updated_at) VALUES (?, ?, ?, ?);",
                            (store_code, financial_year, next_seq, now),
                        )
                    else:
                        seq = row["next_seq"]
                        next_seq = seq + 1
                        conn.execute(
                            "UPDATE invoice_sequences SET next_seq = ?, updated_at = ? WHERE store_code = ? AND financial_year = ?;",
                            (next_seq, now, store_code, financial_year),
                        )

                    invoice_no = f"ELU/{store_code}/{financial_year}/{seq:05d}"

                    conn.execute(
                        """
                        INSERT INTO invoices (
                            invoice_no, store_code, financial_year, seq, status,
                            customer_id, gross_paise, discount_paise, net_paise,
                            taxable_paise, tax_paise, checkout_ref, terminal_charge_id,
                            shopify_order_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            invoice_no,
                            store_code,
                            financial_year,
                            seq,
                            InvoiceStatus.PENDING_RECONCILIATION.value,
                            customer_id,
                            gross_paise,
                            discount_paise,
                            net_paise,
                            taxable_paise,
                            tax_paise,
                            checkout_ref,
                            None,
                            None,
                            now,
                            now,
                        ),
                    )

                    for line in lines:
                        conn.execute(
                            """
                            INSERT INTO invoice_lines (
                                invoice_no, line_id, line_type, variant_id, sku, title,
                                quantity, unit_price_paise, gross_paise, discount_paise,
                                net_paise, hsn, pair_ref, rx_id, lens_config, properties_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (
                                invoice_no,
                                line.line_id,
                                line.line_type,
                                line.variant_id,
                                line.sku,
                                line.title,
                                line.quantity,
                                line.unit_price_paise,
                                line.gross_paise,
                                line.discount_paise,
                                line.net_paise,
                                line.hsn,
                                line.pair_ref,
                                line.rx_id,
                                line.lens_config,
                                json.dumps(line.properties),
                            ),
                        )

                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

        return Invoice(
            invoice_no=invoice_no,
            store_code=store_code,
            financial_year=financial_year,
            seq=seq,
            status=InvoiceStatus.PENDING_RECONCILIATION,
            customer_id=customer_id,
            gross_paise=gross_paise,
            discount_paise=discount_paise,
            net_paise=net_paise,
            taxable_paise=taxable_paise,
            tax_paise=tax_paise,
            checkout_ref=checkout_ref,
            terminal_charge_id=None,
            shopify_order_id=None,
            lines=lines,
            created_at=now,
            updated_at=now,
        )

    def get_invoice(self, invoice_no: str) -> Optional[Invoice]:
        """Retrieve an invoice by its official invoice number."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM invoices WHERE invoice_no = ?;", (invoice_no,))
            row = cur.fetchone()
            if not row:
                return None
            return self._build_invoice_from_row(conn, row)

    def get_invoice_by_checkout_ref(self, checkout_ref: str) -> Optional[Invoice]:
        """Retrieve an invoice by its unique checkout session reference."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM invoices WHERE checkout_ref = ?;", (checkout_ref,))
            row = cur.fetchone()
            if not row:
                return None
            return self._build_invoice_from_row(conn, row)

    def _build_invoice_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Invoice:
        """Helper to reconstruct Invoice entity including line items."""
        lines_cur = conn.execute(
            "SELECT * FROM invoice_lines WHERE invoice_no = ? ORDER BY id ASC;",
            (row["invoice_no"],),
        )
        lines = []
        for l in lines_cur.fetchall():
            props = json.loads(l["properties_json"]) if l["properties_json"] else []
            lines.append(
                InvoiceLine(
                    line_id=l["line_id"],
                    line_type=l["line_type"],
                    variant_id=l["variant_id"],
                    sku=l["sku"],
                    title=l["title"],
                    quantity=l["quantity"],
                    unit_price_paise=l["unit_price_paise"],
                    gross_paise=l["gross_paise"],
                    discount_paise=l["discount_paise"],
                    net_paise=l["net_paise"],
                    hsn=l["hsn"],
                    pair_ref=l["pair_ref"],
                    rx_id=l["rx_id"],
                    lens_config=l["lens_config"],
                    properties=props,
                )
            )

        return Invoice(
            invoice_no=row["invoice_no"],
            store_code=row["store_code"],
            financial_year=row["financial_year"],
            seq=row["seq"],
            status=InvoiceStatus(row["status"]),
            customer_id=row["customer_id"],
            gross_paise=row["gross_paise"],
            discount_paise=row["discount_paise"],
            net_paise=row["net_paise"],
            taxable_paise=row["taxable_paise"],
            tax_paise=row["tax_paise"],
            checkout_ref=row["checkout_ref"],
            terminal_charge_id=row["terminal_charge_id"],
            shopify_order_id=row["shopify_order_id"],
            lines=lines,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def update_invoice_payment(self, invoice_no: str, terminal_charge_id: str) -> None:
        """Record captured payment charge reference."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE invoices SET terminal_charge_id = ?, updated_at = ? WHERE invoice_no = ?;",
                (terminal_charge_id, now, invoice_no),
            )

    def update_invoice_shopify_order(
        self,
        invoice_no: str,
        shopify_order_id: int,
        status: InvoiceStatus = InvoiceStatus.POSTED,
    ) -> None:
        """Link confirmed Shopify order ID and advance invoice status."""
        invoice = self.get_invoice(invoice_no)
        if not invoice:
            raise ValueError(f"Invoice {invoice_no} not found")

        if not invoice.can_transition_to(status):
            raise ValueError(f"Cannot transition invoice {invoice_no} from {invoice.status} to {status}")

        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE invoices SET shopify_order_id = ?, status = ?, updated_at = ? WHERE invoice_no = ?;",
                (shopify_order_id, status.value, now, invoice_no),
            )

    def update_invoice_status(self, invoice_no: str, new_status: InvoiceStatus) -> None:
        """Update invoice status while enforcing lifecycle invariants."""
        invoice = self.get_invoice(invoice_no)
        if not invoice:
            raise ValueError(f"Invoice {invoice_no} not found")

        if not invoice.can_transition_to(new_status):
            raise ValueError(
                f"Illegal state transition for invoice {invoice_no}: "
                f"cannot change status from {invoice.status.value} to {new_status.value} "
                f"(is_paid={invoice.is_paid})"
            )

        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE invoices SET status = ?, updated_at = ? WHERE invoice_no = ?;",
                (new_status.value, now, invoice_no),
            )

    def record_payment_attempt(self, attempt: PaymentRecord) -> PaymentRecord:
        """Persist a new payment attempt prior to external network call."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    """
                    INSERT INTO payment_attempts (
                        checkout_ref, invoice_no, order_ref, amount_paise,
                        method, status, charge_id, error_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        attempt.checkout_ref,
                        attempt.invoice_no,
                        attempt.order_ref,
                        attempt.amount_paise,
                        attempt.method,
                        attempt.status.value,
                        attempt.charge_id,
                        attempt.error_message,
                        now,
                        now,
                    ),
                )
                attempt_id = cur.lastrowid
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        return PaymentRecord(
            id=attempt_id,
            checkout_ref=attempt.checkout_ref,
            invoice_no=attempt.invoice_no,
            order_ref=attempt.order_ref,
            amount_paise=attempt.amount_paise,
            method=attempt.method,
            status=attempt.status,
            charge_id=attempt.charge_id,
            error_message=attempt.error_message,
            created_at=now,
            updated_at=now,
        )

    def update_payment_attempt(
        self,
        order_ref: str,
        status: PaymentStatus,
        charge_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update payment attempt status and charge ID upon completion or failure."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE payment_attempts
                SET status = ?, charge_id = COALESCE(?, charge_id),
                    error_message = ?, updated_at = ?
                WHERE order_ref = ?;
                """,
                (status.value, charge_id, error_message, now, order_ref),
            )

    def get_payment_attempt(self, order_ref: str) -> Optional[PaymentRecord]:
        """Fetch payment attempt by order_ref."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM payment_attempts WHERE order_ref = ?;", (order_ref,))
            row = cur.fetchone()
            if not row:
                return None
            return PaymentRecord(
                id=row["id"],
                checkout_ref=row["checkout_ref"],
                invoice_no=row["invoice_no"],
                order_ref=row["order_ref"],
                amount_paise=row["amount_paise"],
                method=row["method"],
                status=PaymentStatus(row["status"]),
                charge_id=row["charge_id"],
                error_message=row["error_message"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def record_shopify_intent(
        self,
        invoice_no: str,
        checkout_ref: str,
        payload: Dict[str, Any],
        status: str = "PENDING",
    ) -> None:
        """Persist Shopify order-creation payload before network invocation."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO shopify_order_intents (
                    invoice_no, checkout_ref, payload_json, status,
                    shopify_order_id, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?);
                """,
                (invoice_no, checkout_ref, json.dumps(payload), status, now, now),
            )

    def update_shopify_intent(
        self,
        invoice_no: str,
        status: str,
        shopify_order_id: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update status of Shopify order creation intent."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE shopify_order_intents
                SET status = ?, shopify_order_id = COALESCE(?, shopify_order_id),
                    error_message = ?, updated_at = ?
                WHERE invoice_no = ?;
                """,
                (status, shopify_order_id, error_message, now, invoice_no),
            )

    def get_shopify_intent(self, invoice_no: str) -> Optional[Dict[str, Any]]:
        """Fetch stored Shopify intent and parsed payload."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM shopify_order_intents WHERE invoice_no = ?;", (invoice_no,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "invoice_no": row["invoice_no"],
                "checkout_ref": row["checkout_ref"],
                "payload": json.loads(row["payload_json"]),
                "status": row["status"],
                "shopify_order_id": row["shopify_order_id"],
                "error_message": row["error_message"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    def list_pending_invoices(self, store_code: Optional[str] = None) -> List[Invoice]:
        """List all invoices currently in PENDING_RECONCILIATION status."""
        with self._get_connection() as conn:
            if store_code:
                cur = conn.execute(
                    "SELECT * FROM invoices WHERE status = ? AND store_code = ? ORDER BY seq ASC;",
                    (InvoiceStatus.PENDING_RECONCILIATION.value, store_code),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM invoices WHERE status = ? ORDER BY store_code, seq ASC;",
                    (InvoiceStatus.PENDING_RECONCILIATION.value,),
                )
            return [self._build_invoice_from_row(conn, row) for row in cur.fetchall()]

