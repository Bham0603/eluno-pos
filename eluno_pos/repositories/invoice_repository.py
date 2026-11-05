"""SQLite repository for invoice sequencing, state persistence, and audit logging."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import threading
from typing import Any, Dict, Generator, List, Optional

from eluno_pos.adapters.exceptions import AmbiguousOperationError, ValidationError
from eluno_pos.domain.invoice import Invoice, InvoiceLine, InvoiceStatus
from eluno_pos.domain.payment import PaymentRecord, PaymentStatus
from eluno_pos.domain.refund import RefundRecord, RefundStatus


class InvoiceRepository:
    """Thread-safe SQLite repository managing invoice sequences and transaction audit state."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Provide a connection with strict durability, foreign keys, and busy timeout."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None,  # Enable explicit transaction control (BEGIN IMMEDIATE)
        )
        conn.row_factory = sqlite3.Row
        # Connection-level pragmas: enforced per connection
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA synchronous = FULL;")
        try:
            yield conn
        finally:
            conn.close()

    def _now_iso(self) -> str:
        """Current timestamp in standard ISO-8601 UTC format."""
        return datetime.now(timezone.utc).isoformat()

    def _init_db(self) -> None:
        """Create necessary schema tables and indexes with database-level integrity constraints."""
        with self._get_connection() as conn:
            if self.db_path != ":memory:":
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
                    status TEXT NOT NULL CHECK (status IN ('POSTED', 'PENDING_RECONCILIATION', 'CANCELLED')),
                    customer_id INTEGER,
                    gross_paise INTEGER NOT NULL CHECK (gross_paise >= 0),
                    discount_paise INTEGER NOT NULL CHECK (discount_paise >= 0),
                    net_paise INTEGER NOT NULL CHECK (net_paise >= 0),
                    taxable_paise INTEGER NOT NULL CHECK (taxable_paise >= 0),
                    tax_paise INTEGER NOT NULL CHECK (tax_paise >= 0),
                    checkout_ref TEXT UNIQUE NOT NULL,
                    terminal_charge_id TEXT UNIQUE,
                    shopify_order_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (store_code, financial_year, seq)
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
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    unit_price_paise INTEGER NOT NULL CHECK (unit_price_paise >= 0),
                    gross_paise INTEGER NOT NULL CHECK (gross_paise >= 0),
                    discount_paise INTEGER NOT NULL CHECK (discount_paise >= 0),
                    net_paise INTEGER NOT NULL CHECK (net_paise >= 0),
                    taxable_paise INTEGER NOT NULL DEFAULT 0,
                    cgst_paise INTEGER NOT NULL DEFAULT 0,
                    sgst_paise INTEGER NOT NULL DEFAULT 0,
                    tax_paise INTEGER NOT NULL DEFAULT 0,
                    hsn TEXT NOT NULL,
                    pair_ref TEXT,
                    rx_id TEXT,
                    lens_config TEXT,
                    properties_json TEXT,
                    FOREIGN KEY(invoice_no) REFERENCES invoices(invoice_no) ON DELETE RESTRICT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS payment_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkout_ref TEXT NOT NULL,
                    invoice_no TEXT NOT NULL,
                    order_ref TEXT UNIQUE NOT NULL,
                    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
                    method TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('INITIATED', 'CAPTURED', 'FAILED', 'UNKNOWN')),
                    charge_id TEXT UNIQUE,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(invoice_no) REFERENCES invoices(invoice_no) ON DELETE RESTRICT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS shopify_order_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_no TEXT UNIQUE NOT NULL,
                    checkout_ref TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMPLETED', 'AMBIGUOUS', 'RECONCILED')),
                    shopify_order_id INTEGER,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(invoice_no) REFERENCES invoices(invoice_no) ON DELETE RESTRICT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS credit_note_sequences (
                    store_code TEXT NOT NULL,
                    financial_year TEXT NOT NULL,
                    next_seq INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (store_code, financial_year)
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS refunds (
                    refund_id TEXT PRIMARY KEY,
                    credit_note_no TEXT UNIQUE,
                    invoice_no TEXT NOT NULL,
                    checkout_ref TEXT NOT NULL,
                    charge_id TEXT NOT NULL,
                    shopify_order_id INTEGER NOT NULL,
                    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
                    status TEXT NOT NULL CHECK (status IN ('INITIATED', 'TERMINAL_REFUNDED', 'COMPLETED', 'FAILED', 'PENDING_RECONCILIATION')),
                    reason_code TEXT NOT NULL,
                    staff_id TEXT,
                    terminal_refund_id TEXT UNIQUE,
                    shopify_refund_id INTEGER UNIQUE,
                    baseline_refunded_paise INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(invoice_no) REFERENCES invoices(invoice_no) ON DELETE RESTRICT
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS restock_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    refund_id TEXT NOT NULL,
                    variant_id INTEGER NOT NULL,
                    inventory_item_id INTEGER NOT NULL,
                    location_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMPLETED', 'FAILED')),
                    baseline_available INTEGER,
                    resulting_available INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(refund_id) REFERENCES refunds(refund_id) ON DELETE RESTRICT
                );
            """)

            # Safe database migration: Ensure existing Phase 4 tables have Phase 5 columns
            cur = conn.execute("PRAGMA table_info(invoice_lines);")
            existing_lines_cols = {row["name"] for row in cur.fetchall()}
            if "taxable_paise" not in existing_lines_cols:
                conn.execute("ALTER TABLE invoice_lines ADD COLUMN taxable_paise INTEGER NOT NULL DEFAULT 0;")
            if "cgst_paise" not in existing_lines_cols:
                conn.execute("ALTER TABLE invoice_lines ADD COLUMN cgst_paise INTEGER NOT NULL DEFAULT 0;")
            if "sgst_paise" not in existing_lines_cols:
                conn.execute("ALTER TABLE invoice_lines ADD COLUMN sgst_paise INTEGER NOT NULL DEFAULT 0;")
            if "tax_paise" not in existing_lines_cols:
                conn.execute("ALTER TABLE invoice_lines ADD COLUMN tax_paise INTEGER NOT NULL DEFAULT 0;")

            cur = conn.execute("PRAGMA table_info(refunds);")
            existing_refund_cols = {row["name"] for row in cur.fetchall()}
            if "baseline_refunded_paise" not in existing_refund_cols:
                conn.execute("ALTER TABLE refunds ADD COLUMN baseline_refunded_paise INTEGER NOT NULL DEFAULT 0;")

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
                                net_paise, taxable_paise, cgst_paise, sgst_paise, tax_paise,
                                hsn, pair_ref, rx_id, lens_config, properties_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
                                line.taxable_paise,
                                line.cgst_paise,
                                line.sgst_paise,
                                line.tax_paise,
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
            col_keys = l.keys() if hasattr(l, "keys") else []
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
                    taxable_paise=l["taxable_paise"] if "taxable_paise" in col_keys else 0,
                    cgst_paise=l["cgst_paise"] if "cgst_paise" in col_keys else 0,
                    sgst_paise=l["sgst_paise"] if "sgst_paise" in col_keys else 0,
                    tax_paise=l["tax_paise"] if "tax_paise" in col_keys else 0,
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

    def create_refund_intent_atomically(
        self,
        invoice_no: str,
        refund_id: str,
        amount_paise: int,
        reason_code: str,
        staff_id: Optional[str] = None,
    ) -> RefundRecord:
        """Atomically validate invoice state, check active/pending/completed refunds, and persist new refund intent.

        Uses BEGIN IMMEDIATE transaction to guarantee atomic serialization.
        Ensures concurrent requests cannot both pass the check and initiate terminal refund POST requests.
        """
        now = self._now_iso()
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    # 1. Inspect invoice
                    cur = conn.execute("SELECT * FROM invoices WHERE invoice_no = ?;", (invoice_no,))
                    inv_row = cur.fetchone()
                    if not inv_row:
                        raise ValidationError(message=f"Invoice {invoice_no} not found")

                    if inv_row["status"] != InvoiceStatus.POSTED.value:
                        raise ValidationError(
                            message=f"Cannot refund invoice {invoice_no} in status {inv_row['status']}. Only POSTED invoices can be refunded."
                        )

                    if not inv_row["terminal_charge_id"]:
                        raise ValidationError(message=f"Invoice {invoice_no} has no captured terminal charge ID")

                    if not inv_row["shopify_order_id"]:
                        raise ValidationError(message=f"Invoice {invoice_no} has no linked Shopify order ID")

                    # 2. Check existing refunds for this invoice and charge_id
                    cur = conn.execute(
                        "SELECT * FROM refunds WHERE invoice_no = ? OR charge_id = ?;",
                        (invoice_no, inv_row["terminal_charge_id"]),
                    )
                    existing_rows = cur.fetchall()

                    # Block if any unresolved in-flight refund exists
                    for r in existing_rows:
                        if r["status"] in (
                            RefundStatus.INITIATED.value,
                            RefundStatus.TERMINAL_REFUNDED.value,
                            RefundStatus.PENDING_RECONCILIATION.value,
                        ):
                            raise AmbiguousOperationError(
                                message=(
                                    f"Invoice {invoice_no} has an active or unresolved refund intent ({r['refund_id']}) "
                                    f"in status {r['status']}. Resolve pending reconciliation before initiating another refund attempt."
                                )
                            )

                    # Check completed refunds
                    total_completed = sum(
                        r["amount_paise"] for r in existing_rows if r["status"] == RefundStatus.COMPLETED.value
                    )
                    remaining = inv_row["net_paise"] - total_completed
                    if remaining <= 0 or amount_paise > remaining:
                        raise ValidationError(
                            message=(
                                f"Invoice {invoice_no} has already been fully refunded or requested amount "
                                f"({amount_paise} paise) exceeds remaining refundable amount ({remaining} paise)."
                            )
                        )

                    # 3. Insert new refund intent with credit_note_no = NULL
                    conn.execute(
                        """
                        INSERT INTO refunds (
                            refund_id, credit_note_no, invoice_no, checkout_ref,
                            charge_id, shopify_order_id, amount_paise, status,
                            reason_code, staff_id, terminal_refund_id, shopify_refund_id,
                            baseline_refunded_paise, error_message, created_at, updated_at
                        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL, ?, ?);
                        """,
                        (
                            refund_id,
                            invoice_no,
                            inv_row["checkout_ref"],
                            inv_row["terminal_charge_id"],
                            inv_row["shopify_order_id"],
                            amount_paise,
                            RefundStatus.INITIATED.value,
                            reason_code,
                            staff_id,
                            now,
                            now,
                        ),
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

        return self.get_refund(refund_id)

    def allocate_and_persist_credit_note(
        self,
        refund_id: str,
        store_code: str,
        financial_year: str,
    ) -> str:
        """Atomically allocate and assign the next sequential credit note number to a confirmed refund.

        Uses BEGIN IMMEDIATE transaction to guarantee atomic serialization.
        Format: ELU/{store_code}/{financial_year}/CN/{seq:05d}

        Idempotency:
        If credit_note_no is ALREADY allocated for this refund (e.g. during recovery),
        it returns the existing credit_note_no without consuming a new sequence number.
        """
        now = self._now_iso()
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    cur = conn.execute("SELECT credit_note_no FROM refunds WHERE refund_id = ?;", (refund_id,))
                    row = cur.fetchone()
                    if not row:
                        raise ValueError(f"Refund {refund_id} not found")

                    if row["credit_note_no"]:
                        conn.execute("COMMIT;")
                        return row["credit_note_no"]

                    cur = conn.execute(
                        "SELECT next_seq FROM credit_note_sequences WHERE store_code = ? AND financial_year = ?;",
                        (store_code, financial_year),
                    )
                    seq_row = cur.fetchone()
                    if seq_row is None:
                        seq = 1
                        next_seq = 2
                        conn.execute(
                            "INSERT INTO credit_note_sequences (store_code, financial_year, next_seq, updated_at) VALUES (?, ?, ?, ?);",
                            (store_code, financial_year, next_seq, now),
                        )
                    else:
                        seq = seq_row["next_seq"]
                        next_seq = seq + 1
                        conn.execute(
                            "UPDATE credit_note_sequences SET next_seq = ?, updated_at = ? WHERE store_code = ? AND financial_year = ?;",
                            (next_seq, now, store_code, financial_year),
                        )

                    credit_note_no = f"ELU/{store_code}/{financial_year}/CN/{seq:05d}"
                    conn.execute(
                        "UPDATE refunds SET credit_note_no = ?, updated_at = ? WHERE refund_id = ?;",
                        (credit_note_no, now, refund_id),
                    )
                    conn.execute("COMMIT;")
                    return credit_note_no
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

    def allocate_credit_note_no(self, store_code: str, financial_year: str) -> str:
        """Atomically allocate the next sequential credit note number per store.

        Uses BEGIN IMMEDIATE transaction to guarantee atomic serialization.
        Format: ELU/{store_code}/{financial_year}/CN/{seq:05d}
        """
        now = self._now_iso()
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    cursor = conn.execute(
                        "SELECT next_seq FROM credit_note_sequences WHERE store_code = ? AND financial_year = ?;",
                        (store_code, financial_year),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        seq = 1
                        next_seq = 2
                        conn.execute(
                            "INSERT INTO credit_note_sequences (store_code, financial_year, next_seq, updated_at) VALUES (?, ?, ?, ?);",
                            (store_code, financial_year, next_seq, now),
                        )
                    else:
                        seq = row["next_seq"]
                        next_seq = seq + 1
                        conn.execute(
                            "UPDATE credit_note_sequences SET next_seq = ?, updated_at = ? WHERE store_code = ? AND financial_year = ?;",
                            (next_seq, now, store_code, financial_year),
                        )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

        return f"ELU/{store_code}/{financial_year}/CN/{seq:05d}"

    def record_refund_intent(self, record: RefundRecord) -> RefundRecord:
        """Persist a new refund attempt prior to terminal invocation."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT INTO refunds (
                        refund_id, credit_note_no, invoice_no, checkout_ref,
                        charge_id, shopify_order_id, amount_paise, status,
                        reason_code, staff_id, terminal_refund_id, shopify_refund_id,
                        baseline_refunded_paise, error_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        record.refund_id,
                        record.credit_note_no,
                        record.invoice_no,
                        record.checkout_ref,
                        record.charge_id,
                        record.shopify_order_id,
                        record.amount_paise,
                        record.status.value,
                        record.reason_code,
                        record.staff_id,
                        record.terminal_refund_id,
                        record.shopify_refund_id,
                        record.baseline_refunded_paise,
                        record.error_message,
                        now,
                        now,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        return RefundRecord(
            refund_id=record.refund_id,
            credit_note_no=record.credit_note_no,
            invoice_no=record.invoice_no,
            checkout_ref=record.checkout_ref,
            charge_id=record.charge_id,
            shopify_order_id=record.shopify_order_id,
            amount_paise=record.amount_paise,
            status=record.status,
            reason_code=record.reason_code,
            staff_id=record.staff_id,
            terminal_refund_id=record.terminal_refund_id,
            shopify_refund_id=record.shopify_refund_id,
            baseline_refunded_paise=record.baseline_refunded_paise,
            error_message=record.error_message,
            created_at=now,
            updated_at=now,
        )

    def update_refund_baseline(
        self,
        refund_id: str,
        baseline_refunded_paise: int,
    ) -> None:
        """Durably persist baseline refunded_paise on the refund record before terminal POST."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE refunds
                SET baseline_refunded_paise = ?, updated_at = ?
                WHERE refund_id = ?;
                """,
                (baseline_refunded_paise, now, refund_id),
            )

    def update_refund_terminal_captured(
        self,
        refund_id: str,
        terminal_refund_id: str,
        baseline_refunded_paise: int = 0,
    ) -> None:
        """Mark refund as captured on physical/UPI terminal."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE refunds
                SET status = ?, terminal_refund_id = ?, baseline_refunded_paise = ?, updated_at = ?
                WHERE refund_id = ?;
                """,
                (RefundStatus.TERMINAL_REFUNDED.value, terminal_refund_id, baseline_refunded_paise, now, refund_id),
            )

    def update_refund_completed(
        self,
        refund_id: str,
        shopify_refund_id: int,
    ) -> None:
        """Mark refund as fully completed with linked Shopify refund ID."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE refunds
                SET status = ?, shopify_refund_id = ?, updated_at = ?
                WHERE refund_id = ?;
                """,
                (RefundStatus.COMPLETED.value, shopify_refund_id, now, refund_id),
            )

    def update_refund_status(
        self,
        refund_id: str,
        status: RefundStatus,
        error_message: Optional[str] = None,
    ) -> None:
        """Update refund status (e.g. PENDING_RECONCILIATION or FAILED)."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE refunds
                SET status = ?, error_message = ?, updated_at = ?
                WHERE refund_id = ?;
                """,
                (status.value, error_message, now, refund_id),
            )

    def get_refund(self, refund_id: str) -> Optional[RefundRecord]:
        """Fetch refund record by refund_id."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM refunds WHERE refund_id = ?;", (refund_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._build_refund_from_row(row)

    def get_refunds_for_invoice(self, invoice_no: str) -> List[RefundRecord]:
        """Fetch all refund records linked to an invoice."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM refunds WHERE invoice_no = ? ORDER BY created_at ASC;",
                (invoice_no,),
            )
            return [self._build_refund_from_row(r) for r in cur.fetchall()]

    def _build_refund_from_row(self, row: sqlite3.Row) -> RefundRecord:
        """Helper to reconstruct RefundRecord from database row."""
        return RefundRecord(
            refund_id=row["refund_id"],
            credit_note_no=row["credit_note_no"],
            invoice_no=row["invoice_no"],
            checkout_ref=row["checkout_ref"],
            charge_id=row["charge_id"],
            shopify_order_id=row["shopify_order_id"],
            amount_paise=row["amount_paise"],
            status=RefundStatus(row["status"]),
            reason_code=row["reason_code"],
            staff_id=row["staff_id"],
            terminal_refund_id=row["terminal_refund_id"],
            shopify_refund_id=row["shopify_refund_id"],
            baseline_refunded_paise=row["baseline_refunded_paise"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def record_restock_intent(
        self,
        refund_id: str,
        variant_id: int,
        inventory_item_id: int,
        location_id: int,
        quantity: int,
        baseline_available: Optional[int] = None,
    ) -> int:
        """Record intent to restock a refunded frame item."""
        now = self._now_iso()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO restock_intents (
                    refund_id, variant_id, inventory_item_id, location_id,
                    quantity, status, baseline_available, resulting_available,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, NULL, ?, ?);
                """,
                (
                    refund_id,
                    variant_id,
                    inventory_item_id,
                    location_id,
                    quantity,
                    baseline_available,
                    now,
                    now,
                ),
            )
            return cur.lastrowid

    def update_restock_intent(
        self,
        intent_id: int,
        status: str,
        resulting_available: Optional[int] = None,
    ) -> None:
        """Update status and confirmed stock count of a restock intent."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE restock_intents
                SET status = ?, resulting_available = ?, updated_at = ?
                WHERE id = ?;
                """,
                (status, resulting_available, now, intent_id),
            )

    def get_invoices_for_store_and_date(
        self,
        store_code: str,
        date_prefix: str,
    ) -> List[Invoice]:
        """Retrieve invoices for a store where created_at matches the date prefix."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM invoices WHERE store_code = ? AND created_at LIKE ? || '%' ORDER BY seq ASC;",
                (store_code, date_prefix),
            )
            return [self._build_invoice_from_row(conn, row) for row in cur.fetchall()]

    def get_refunds_for_store_and_date(
        self,
        store_code: str,
        date_prefix: str,
    ) -> List[RefundRecord]:
        """Retrieve refunds for a store where created_at matches the date prefix."""
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT r.* FROM refunds r
                JOIN invoices i ON r.invoice_no = i.invoice_no
                WHERE i.store_code = ? AND r.created_at LIKE ? || '%' AND r.status = ?
                ORDER BY r.created_at ASC;
                """,
                (store_code, date_prefix, RefundStatus.COMPLETED.value),
            )
            return [self._build_refund_from_row(r) for r in cur.fetchall()]

