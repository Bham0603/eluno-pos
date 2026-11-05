# Eluno POS — CLI User Guide & Operational Manual

Welcome to the command-line interface for **Eluno POS**, an enterprise retail point-of-sale system engineered specifically for optical retail stores.

The Eluno POS CLI integrates the complete architectural lifecycle across customer identification, clinical optical validation, paired inventory checkout, promotion discounting, exact-paisa GST calculation, payment capture via terminal, durable SQLite state transitions, Shopify cloud synchronization, full refunds with credit notes, day-end store audit reporting, and transaction reconciliation.

---

## 1. System Requirements & Setup

### Prerequisites
- **Python**: 3.12+ (tested on Python 3.12 64-bit).
- **SQLite**: Built into Python standard library (`sqlite3` with WAL mode and foreign key support).
- **Operating System**: Windows, macOS, or Linux.

### Local Mock Server Startup
The starter-kit mock API server provides simulated upstream services for Shopify Admin API, SMT Optical Clinic API, and Payment Gateway/Terminal API.

In a dedicated terminal session, start the mock server:
```bash
python starter-kit/mock-apis/mock_server.py --port 8787 --no-flake
```
> [!NOTE]
> The `--no-flake` flag disables artificial simulated 429 rate limits and 503 gateway timeouts for deterministic local evaluation. Remove `--no-flake` to test real-world chaotic network resilience.

### Configuration (`.env`)
Eluno POS reads environment variables from a local `.env` file (or system environment). An annotated template is provided in [`.env.example`](file:///c:/Users/bham0/OneDrive/Desktop/eluno-pos-exercise-v1.2%20(1)/eluno-pos-exercise/.env.example).

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Default configuration values:
```ini
POS_ENV=development
POS_DB_PATH=eluno_pos.db
SHOPIFY_BASE_URL=http://localhost:8787/admin/api/2026-01
SMT_BASE_URL=http://localhost:8787/smt
TERMINAL_BASE_URL=http://localhost:8787/terminal
HTTP_TIMEOUT_SECONDS=10.0
HTTP_MAX_RETRIES=3
TERMINAL_MAX_RETRIES_503=1
```

---

## 2. Command Reference

The Eluno POS application is executed as a Python module:
```bash
python -m eluno_pos [COMMAND] [OPTIONS]
```

To see the list of all commands:
```bash
python -m eluno_pos --help
```

---

### Command 1: `doctor` (System Health & Configuration Check)
Inspects local configuration, database schema durability, and read-only connectivity to upstream APIs.

```bash
python -m eluno_pos doctor
```

**What it verifies:**
1. **Configuration**: Inspects database path, timeout, retry parameters, and API URLs.
2. **SQLite Database**: Verifies SQLite engine, tables, WAL (Write-Ahead Logging), and `foreign_keys = ON`.
3. **Shopify Admin API**: Tests read-only reachability via `GET /locations`.
4. **SMT Optical Clinic API**: Tests read-only reachability via `GET /staff`.
5. **Payment Terminal API**: Probes gateway health endpoint.

**Example Output:**
```
========================================================================
  ELUNO POS — SYSTEM DIAGNOSTIC (DOCTOR)
  Pre-flight Operational Verification & Integrity Check
========================================================================

✔ Database Configuration: eluno_pos.db (Timeout: 10.0s, Retries: 3)
✔ SQLite Engine: SQLite 3.45.1 [WAL: ON, Foreign Keys: ON]
✔ Upstream Service Reachability:
  • Shopify Admin API (http://localhost:8787/admin/api/2026-01): ONLINE (8 locations discovered)
  • SMT Clinic API (http://localhost:8787/smt): ONLINE (6 staff members discovered)
  • Payment Terminal API (http://localhost:8787/terminal): ONLINE (Gateway ready)

✔ 5/5 diagnostic checks passed. System is fully operational.
```

---

### Command 2: `customer lookup` (Profile & Prescription Search)
Looks up a customer profile by telephone number, displaying tags, membership tiers, and optical prescriptions on record while enforcing **clinical privacy data minimization** (no raw clinical data exported to non-optical systems).

```bash
python -m eluno_pos customer lookup --phone +919000000001
```

**Options:**
- `--phone`, `-p`: Customer 10-digit or E.164 phone number (e.g. `+919000000001`).

**Example Output:**
```
┌──────────────────────────────────────────────────────────────────┐
│  Customer Profile                                                │
├──────────────────────────────────────────────────────────────────┤
│  Customer ID: 80001001                                           │
│  Name: Rahul Menon                                               │
│  Phone: +919000000001                                            │
│  Email: N/A                                                      │
│  Home Store: VC                                                  │
│  Customer Tags: family                                           │
│  Total Orders: 0                                                 │
└──────────────────────────────────────────────────────────────────┘

Retrieving clinical prescriptions from SMT...
  • Found 2 prescription(s) on record:

Rx ID  | Test Date  | Clinic / Optom  | OD (Right Eye)                  | OS (Left Eye)                   | PD   
-------+------------+-----------------+---------------------------------+---------------------------------+------
RX5002 | 2026-08-12 | SMT (external)  | SPH: +2.50, CYL: -2.00, AX: 155 | SPH: +2.75, CYL: -2.00, AX: 155 | 60 mm
RX5003 | 2026-05-15 | S103 (in_store) | SPH: +2.75, CYL: +0.00, AX: -   | SPH: +2.75, CYL: +0.00, AX: -   | 64 mm
```

---

### Command 3: `checkout` (15-Step Guided Retail Optical Checkout)
Orchestrates the complete 15-step retail optical checkout lifecycle. Can be run in full interactive guided mode or non-interactive scripted mode (`--yes`).

```bash
python -m eluno_pos checkout --store HSR --staff S108 --phone +919000000001 --frame 91000001 --lens 91000041 --yes
```

**Options:**
- `--store`, `-s`: Store code (e.g. `HSR`, `VC`, `IND`, `JAY`, `KOR`, `MALL`, `WHITE`).
- `--staff`: Operating staff member ID (e.g. `S108`).
- `--phone`, `-p`: Customer phone number.
- `--frame`, `-f`: Shopify Variant ID of the frame.
- `--lens`, `-l`: Shopify Variant ID of the ophthalmic lens.
- `--rx-id`: Specific prescription ID (defaults to latest on file).
- `--for-member`: Recipient name for family billing (defaults to `Self`).
- `--override-bps`: Staff discount override in basis points (100 bps = 1.00%, max 1000 bps = 10.00%).
- `--override-manager`: Authorizing store manager ID if operating staff lacks override privilege.
- `--override-reason`: Reason code for manager override (e.g. `CUSTOMER_RETENTION`, `PRICE_MATCH`, `SCRATCH_AND_DENT`).
- `--payment-method`, `-m`: Terminal payment method (`upi`, `card`, `cash`). Default: `upi`.
- `--yes`, `-y`: Non-interactive auto-confirmation mode.

**Checkout Lifecycle Stages:**
1. **Store Selection**: Resolves location code and Shopify location ID.
2. **Staff Authentication**: Validates staff credentials and override roles.
3. **Customer Identification**: Pulls profile and tags (`priority`, `family`, `campaign`).
4. **Prescription Retrieval**: Selects clinical prescription or plano option.
5. **Family Billing**: Assigns recipient name for family order tracking.
6. **Catalogue Selection**: Identifies frame and lens variants.
7. **Optical Validation (`LensValidator`)**: Checks SPH, CYL, AXIS, and ADD limits. Rejects incompatible geometries before invoicing.
8. **Pair Association**: Binds frame and lens under a deterministic `pair_ref` token.
9. **Manager Override**: Validates manager role, reason code, and enforces strict 10% (1000 bps) cap.
10. **Promotion Evaluation (`OfferService`)**: Evaluates best savings across product, category, pair, and cart levels.
11. **Tax Engine (`GstService`)**: Computes exact CGST + SGST per HSN (`90031900` @ 12%, `90015000` @ 5%) with zero paisa drift.
12. **Staff Confirmation Gate**: Requires explicit operator confirmation before terminal charging.
13. **Payment Capture (`PaymentService`)**: Dispatches charge to terminal with durable sequence tracking.
14. **Cloud Order Sync (`ShopifyOrderService`)**: Creates paid Shopify order with minimized metadata (`rx_id`, `pair_ref`, `hsn`).
15. **Receipt Issuance**: Generates formatted receipt and dispatches order to optical lab.

---

### Command 4: `refund` (Store Return & Full Refund)
Processes a full return of an existing tax invoice, reversing payment on the original terminal charge, issuing a numbered Credit Note, restocking the physical frame at the store, and scrapping custom prescription lenses.

```bash
python -m eluno_pos refund --invoice ELU/HSR/FY26/00001 --staff S108 --reason DEFECTIVE --yes
```

**Options:**
- `--invoice`, `-i`: Tax invoice number (e.g. `ELU/HSR/FY26/00001`).
- `--location-id`: Shopify store location ID (defaults to original invoice location).
- `--reason`, `-r`: Reason code (`DEFECTIVE`, `WRONG_POWER`, `CUSTOMER_RETURN`, `FRAME_DAMAGE`, `UNCOMFORTABLE_FIT`).
- `--staff`: Authorizing staff member ID.
- `--yes`, `-y`: Non-interactive auto-confirmation mode.

**Refund Invariants:**
- **Atomic Pre-check**: Uses `BEGIN IMMEDIATE` to prevent concurrent duplicate refunds.
- **Credit Note Sequencing**: Allocates durable credit note `ELU/{store}/FY26/CN/{seq}` **only after** terminal confirmation.
- **Frame Restock**: Physical frames are restocked (`restock_type = return`, quantity +1).
- **Custom Lens Scrapped**: Custom prescription lenses are never restocked (`restock_type = no_restock`).
- **Audit Preservation**: Original sales invoice remains intact in `POSTED` status; refund is linked in `refunds` ledger.

---

### Command 5: `report` (Store Day-End Financial & Tax Report)
Generates the daily store settlement report for audit, reconciling local SQLite transactions with Shopify cloud orders.

```bash
python -m eluno_pos report --store HSR --date 2026-11-05
```

**Options:**
- `--store`, `-s`: Store code (`HSR`, `VC`, etc.).
- `--date`, `-d`: Business date in `YYYY-MM-DD` format (defaults to today).
- `--no-reconcile-shopify`: Skip live comparison against Shopify cloud orders.

**Report Sections:**
- **Executive Summary**: Gross sales, discounts given, net sales, completed refunds, net store revenue.
- **Tax Breakdown by HSN**:
  - HSN `90031900` (Frames): Gross taxable, gross 12% GST, refund taxable, refund GST reversal, net taxable, net GST.
  - HSN `90015000` (Lenses): Gross taxable, gross 5% GST, refund taxable, refund GST reversal, net taxable, net GST.
- **Payment Method Split**: Collections via UPI, Card, and Cash.
- **Sales Channel Split**: In-store walk-in vs. omnichannel orders.
- **Operational Audit**: Count of cancelled pre-payment checkouts, pending reconciliations, and cloud discrepancy flags.

---

### Command 6: `reconcile` (Transaction Recovery Hub)
Inspects and safely resolves incomplete checkouts, ambiguous terminal transactions, or pending cloud refunds without replaying non-idempotent operations.

```bash
python -m eluno_pos reconcile --store HSR
```

**Options:**
- `--store`, `-s`: Filter by store code (optional).

**Recovery Actions:**
- **Pending Invoices**: Checks if terminal captured payment using idempotent `order_ref`. If captured, finalizes Shopify order without double-charging. If not captured, safely marks invoice `CANCELLED`.
- **Pending Refunds**: Checks terminal charge `refunded_paise` against baseline. If confirmed, issues credit note and finalizes Shopify refund.
- **Pending Shopify Intents**: Retries cloud sync for captured payments that experienced upstream network timeouts.

---

### Command 7: `invoice show` (Tax Invoice Document Inspection)
Displays the full printable tax invoice document and line item breakdown without exposing sensitive clinical parameters.

```bash
python -m eluno_pos invoice show --invoice ELU/HSR/FY26/00001
```

**Options:**
- `--invoice`, `-i`: Tax invoice number.

---

## 3. Evaluator Walkthrough (5-Minute Verification)

Follow these simple steps to evaluate the application end-to-end:

### Step 1: Health Diagnostic
```bash
python -m eluno_pos doctor
```
*Expected: 5/5 checks passed.*

### Step 2: Customer Search
```bash
python -m eluno_pos customer lookup --phone +919000000001
```
*Expected: Displays Rahul Menon, family tag, and prescriptions RX5002 & RX5003.*

### Step 3: Complete Guided Checkout
```bash
python -m eluno_pos checkout --store HSR --staff S108 --phone +919000000001 --frame 91000001 --lens 91000041 --yes
```
*Expected: Generates invoice `ELU/HSR/FY26/00001`, captures payment on terminal, posts order to Shopify, prints receipt.*

### Step 4: Inspect Generated Tax Invoice
```bash
python -m eluno_pos invoice show --invoice ELU/HSR/FY26/00001
```
*Expected: Displays invoice with line items, FESTIVE10 discount, HSN breakdown, and payment reference.*

### Step 5: Full Return & Credit Note Issuance
```bash
python -m eluno_pos refund --invoice ELU/HSR/FY26/00001 --staff S108 --reason DEFECTIVE --yes
```
*Expected: Issues credit note `ELU/HSR/FY26/CN/00001`, reverses terminal charge, restocks frame, scraps lens.*

### Step 6: Generate Day-End Report
```bash
python -m eluno_pos report --store HSR --date 2026-11-05
```
*Expected: Shows 1 sale, 1 refund, net revenue ₹0.00, exact HSN GST math.*

### Step 7: Check Reconciliation Status
```bash
python -m eluno_pos reconcile --store HSR
```
*Expected: Reports 0 pending checkouts, 0 pending refunds.*

---

## 4. Optical & Financial Precision Rules

1. **Integer Paise Representation**: All internal monetary values are stored and calculated in integer paise ($1 = 100\text{ paise}$). Conversion to decimal string representation occurs only at API boundaries.
2. **Deterministic GST Allocation**:
   - Frames: HSN `90031900` @ 12% ($6\%\text{ CGST} + 6\%\text{ SGST}$).
   - Lenses: HSN `90015000` @ 5% ($2.5\%\text{ CGST} + 2.5\%\text{ SGST}$).
   - $\text{Taxable} + \text{CGST} + \text{SGST} = \text{Net Line Amount}$ exactly to the paisa.
3. **Clinical Privacy Protection**: Optical parameters (SPH, CYL, AXIS, ADD, PD) are validated inside the POS engine and stored in clinic systems (SMT). Non-clinical systems (Shopify Admin API) receive only the anonymized reference identifier (`rx_id`), never full clinical eye measurements.
