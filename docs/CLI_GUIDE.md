# Eluno POS — CLI User Guide & Operational Manual

Welcome to the command-line interface for **Eluno POS**, a domain-driven retail point-of-sale technical assessment prototype engineered specifically for optical retail stores.

The Eluno POS CLI integrates the complete architectural lifecycle across customer identification, clinical optical validation, paired inventory checkout, promotion discounting, exact-paisa GST calculation, payment capture via terminal, durable SQLite state transitions, Shopify cloud synchronization, full refunds with credit notes, day-end store audit reporting, and transaction reconciliation against local mock APIs (`http://localhost:8787`).

---

## 1. System Requirements & Setup

> [!NOTE]
> **Prototype Environment Notice:**  
> This implementation connects to local mock services running on `http://localhost:8787`. It does not connect to live production Eluno credentials, live payment gateways, or physical store hardware. Production deployment requirements are detailed in [`README.md`](../README.md#11-known-limitations--production-dependencies).

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
Eluno POS reads environment variables from a local `.env` file (or system environment). An annotated template is provided in [`.env.example`](../.env.example).

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Configuration variables loaded via `AppConfig.from_env()`:
```ini
# Mock Server Endpoint URLs
SHOPIFY_BASE_URL=http://localhost:8787/admin/api/2026-10
SMT_BASE_URL=http://localhost:8787/smt
TERMINAL_BASE_URL=http://localhost:8787/terminal

# SQLite Database File Location (Ignored by Git)
DATABASE_PATH=eluno_pos.db

# HTTP Client Resilience Settings
HTTP_TIMEOUT_SECONDS=5.0
MAX_RETRIES_429=3
MAX_RETRIES_TERMINAL_503=3
BACKOFF_FACTOR=1.0
```

---

## 2. Command Reference

The Eluno POS application is executed as a Python module:
```bash
python -m eluno_pos [COMMAND] [OPTIONS]
```

Global options:
- `--db PATH`: Path to SQLite database file (overrides `DATABASE_PATH`).
- `--env-file PATH`: Path to custom `.env` file.
- `--debug`: Show detailed developer diagnostics and raw stack traces on error.

To see the list of all commands:
```bash
python -m eluno_pos --help
```

---

### Command 1: `doctor` (System Health & Configuration Check)
Inspects local configuration, database schema durability, and read-only connectivity to upstream APIs without initiating financial mutations.

```bash
python -m eluno_pos doctor
```

**What it verifies:**
1. **Configuration**: Inspects database path, timeout, retry parameters, and API URLs.
2. **SQLite Database**: Verifies SQLite engine, tables, WAL (Write-Ahead Logging), and `foreign_keys = ON`.
3. **Shopify Admin API**: Tests read-only reachability via `GET /locations.json`.
4. **SMT Optical Clinic API**: Tests read-only reachability via `GET /smt/staff`.
5. **Payment Terminal API**: Probes gateway health endpoint via read-only GET.

**Example Output:**
```
========================================================================
  ELUNO POS — SYSTEM DOCTOR & HEALTH CHECK
  Verifying local and upstream system health
========================================================================

[1/5] Checking Configuration...
  • Shopify Base URL:  http://localhost:8787/admin/api/2026-10
  • SMT Base URL:      http://localhost:8787/smt
  • Terminal Base URL: http://localhost:8787/terminal
  • Database Path:     eluno_pos.db
  • Local Mock Mode:   YES (Safe)

[2/5] Checking SQLite Database...
  • Journal Mode:      wal
  • Foreign Keys:      ENABLED
  • Verified Tables:   9 tables (invoice_sequences, invoices, invoice_lines...)

[3/5] Checking Shopify Service Reachability...
  • Status:            REACHABLE (2.1 ms)
  • Available Stores:  8 locations (Eluno Mantri Mall, Eluno HSR Layout...)

[4/5] Checking SMT Service Reachability...
  • Status:            REACHABLE (1.8 ms)
  • Active Staff:      14 staff members registered

[5/5] Checking Terminal Gateway Reachability...
  • Status:            REACHABLE (1.5 ms)

✔ All system diagnostics passed. Eluno POS is ready for operation.
```

---

### Command 2: `customer lookup` (Profile & Prescription Search)
Looks up a customer profile by telephone number, displaying tags, membership tiers, and optical prescriptions on record while strictly enforcing **clinical privacy data minimization** (no raw clinical SPH, CYL, AXIS, or PD values exported to terminal output).

```bash
python -m eluno_pos customer lookup --phone +919000000001
```

**Options:**
- `--phone`, `-p`: Customer phone number (e.g. `+919000000001`).

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

Rx ID  | Test Date  | Source / Optometrist | Status                   | Reading ADD
-------+------------+----------------------+--------------------------+------------
RX5002 | 2026-08-12 | SMT (external)       | Valid (until 2027-08-12) | No
RX5003 | 2026-05-15 | S103 (in_store)      | Valid (until 2027-05-15) | No
```

---

### Command 3: `checkout` (15-Step Guided Retail Optical Checkout)
Orchestrates the complete 15-step retail optical checkout lifecycle. Can be run in full interactive guided mode or non-interactive scripted mode (`--yes`).

```bash
python -m eluno_pos checkout --store HSR --staff S108 --customer +919000000001 --frame 91000001 --lens 91000041 --yes
```

**Options:**
- `--store`, `-s`: Store code (`MM`, `HSR`, `BR`, `KMG`, `JN`, `IN`, `VC`). Must match an active retail store (HQ is a warehouse and excluded).
- `--staff`: Operating staff ID (e.g. `S108`). Validated against the SMT staff registry and verified to belong to the selected store.
- `--customer`, `-c`: Customer phone number (e.g. `+919000000001`).
- `--frame`, `-f`: Shopify Variant ID of the frame (e.g. `91000001`). Must contain `variant.eluno.hsn` metadata.
- `--lens`, `-l`: Shopify Variant ID of the ophthalmic lens (e.g. `91000041`). Must contain `variant.eluno.hsn` metadata.
- `--rx`, `-r`: Specific clinical prescription ID to link (e.g. `RX5002`) or `plano`. Must belong to customer and be valid on transaction date.
- `--for-member`: Recipient name for single-pair wearer attribution (persisted as `wearer_name`). Multi-pair single-invoice family checkout with multiple distinct prescriptions is deferred to v1.1.
- `--for-customer-id`, `--wearer-id`: Persisted integer customer ID of wearer/recipient (kept `None` if unenrolled).
- Sales Channel: Checkout automatically assigns `channel="walk_in"`. Interactive till-side selection of `whatsapp_booked` and `online_pickup` is deferred to v1.1.
- `--manager-override`: Manager discount override in basis points (100 bps = 1.00%, max 1000 bps = 10.00%).
- `--override-reason`: Mandatory reason code for manager override (`CUSTOMER_RETENTION`, `PRICE_MATCH`, `WALKOUT_RESCUE`, `DAMAGE_ALLOWANCE`).
- `--override-manager-id`: Staff ID of authorizing store manager (must have `store_manager` override permissions).
- `--method`, `-m`: Payment method (`upi`, `card`, `cash`). Default: `upi`.
- `--yes`, `-y`: Run non-interactively. Requires complete, explicit transaction inputs (`--store`, `--staff`, `--customer`, `--frame`, `--lens`); no silent demo defaults are assumed for financial safety, and authorizes the financial operation without interactive confirmation.

**Checkout Lifecycle & Safety Invariants:**
1. **Retail Store Filtering**: Validates store code against active Shopify locations with `eluno.kind="store"`. Excludes HQ (`kind="warehouse"`) and inactive stores.
2. **Staff Store Association**: Validates operating staff against SMT staff records for the active store. Rejects unverified staff or staff registered to other stores.
3. **Customer Identification**: Looks up profile by phone and pulls customer tags (`priority`, `family`, `campaign`).
4. **Prescription Safety & Clinical Privacy**: Conceals raw clinical optical measurements (SPH, CYL, AXIS, ADD, PD) from the operator screen. Displays only governance metadata (Rx ID, test date, optometrist/source, validity status, and reading ADD presence). If all prescriptions on file are expired, checkout rejects in `--yes` mode unless `--rx plano` is explicitly authorized; in interactive mode, staff confirmation is required.
5. **Family Wearer Identity**: Distinguishes between purchaser, enrolled family member with a customer ID (`for_customer_id`), and unenrolled family member by text name (`wearer_name`). Unenrolled members never map to the purchaser's ID. Multi-pair family checkout with multiple distinct prescriptions on a single invoice is deferred to v1.1.
6. **Authoritative HSN Source**: Enforces HSN lookup strictly from `variant.eluno.hsn`. Rejects variants missing documented HSN metadata before invoice allocation.
7. **Optical Validation (`LensValidator`)**: Checks SPH, CYL, AXIS, and ADD limits against lens geometry.
8. **Pair Association**: Binds frame and lens under a deterministic `pair_ref` token and persists wearer association.
9. **Manager Override**: Validates authorized store manager role, mandatory audit reason code (`CUSTOMER_RETENTION`, `PRICE_MATCH`, `WALKOUT_RESCUE`, `DAMAGE_ALLOWANCE`), and enforces strict 10% cap.
10. **Promotion Evaluation (`OfferService`)**: Evaluates best savings across product, category, pair, and cart levels.
11. **Tax Engine (`GstService`)**: Computes exact CGST + SGST per HSN (`90031900` @ 18%, `90015000` @ 5%) with zero paisa drift.
12. **Dynamic Financial Year**: Derives invoice financial year dynamically from transaction date using Indian convention (April 1 – March 31, e.g. `FY27` for November 2026).
13. **Payment Authorization Gate**: Interactive confirmation defaults to `False` (`[y/N]`); pressing Enter cancels payment safely without charging.
14. **Payment Capture (`PaymentService`)**: Dispatches charge to terminal with durable sequence tracking.
15. **Cloud Order Sync (`ShopifyOrderService`)**: Creates paid Shopify order with minimized metadata (`rx_id`, `pair_ref`, `hsn`, `for_customer_id`, `wearer_name`, `channel="walk_in"`).
16. **Receipt Presentation**: Formats and prints a structured Unicode receipt card to the terminal console. Physical thermal receipt printing via ESC/POS hardware drivers and PDF/HTML generation are deferred to v1.1.

---

### Command 4: `refund` (Store Return & Full Refund)
Processes a full return of an existing tax invoice, reversing payment on the original terminal charge, issuing a sequentially numbered Credit Note, restocking the physical frame at the store, and scrapping custom prescription lenses.

```bash
python -m eluno_pos refund --invoice ELU/HSR/FY27/00001 --staff S108 --reason CUSTOMER_RETURN --yes
```

**Options:**
- `--invoice`, `-i`: Tax invoice number to refund (e.g. `ELU/HSR/FY27/00001`). Required in `--yes` mode.
- `--location`: Shopify store location ID for frame restocking (defaults to original invoice store location).
- `--reason`: Return reason code (`CUSTOMER_RETURN`, `DEFECTIVE_FRAME`, `INCORRECT_PRESCRIPTION`). Required in `--yes` mode.
- `--staff`: Authorizing staff member ID. Required in all modes.
- `--yes`, `-y`: Run non-interactively with explicit arguments (`--invoice`, `--staff`, `--reason`), bypassing confirmation prompts.

**Refund Staff Authorization & Safety Invariants:**
- **Strict Staff Verification**: The authorizing staff ID is queried against the SMT registry for the original invoice's `store_code`.
- **Store Association Enforcement**: Staff belonging to another store or unknown staff IDs are strictly rejected.
- **Zero Pre-authorization Mutations**: If staff validation fails, zero terminal refund POST calls are made and zero refund intents are allocated in the database.
- **Atomic Concurrency Safety**: Uses `BEGIN IMMEDIATE` transaction to guarantee concurrent requests cannot double-refund.
- **Sequential Credit Notes**: Allocates durable credit note `ELU/{store}/{FY}/CN/{seq}` **only after** conclusive terminal confirmation.
- **Physical Frame Restock**: Physical frames are restocked (+1 available at store location).
- **Custom Lens Scrapped**: Custom ophthalmic lenses are marked `no_restock`.
- **Audit Preservation**: Original sales invoice remains intact in `POSTED` status; refund is linked in `refunds` ledger.

---

### Command 5: `report` (Store Day-End Financial & Tax Report)
Generates the daily store settlement report for audit, reconciling local SQLite transactions with Shopify cloud orders.

```bash
python -m eluno_pos report --store HSR --date 2026-11-05
```

**Options:**
- `--store`, `-s`: Store code (`MM`, `HSR`, `BR`, `KMG`, `JN`, `IN`, `VC`).
- `--date`, `-d`: Business date in `YYYY-MM-DD` format (defaults to today in Asia/Kolkata).
- `--location`: Store location ID (defaults to store code mapping).
- `--no-reconcile`: Skip live comparison against Shopify cloud orders.

**Report Sections:**
- **Executive Summary**: Gross sales, discounts given, net sales, completed refunds, net store revenue.
- **Tax Breakdown by HSN**:
  - HSN `90031900` (Frames): Gross taxable, gross 18% GST (9% CGST + 9% SGST), refund taxable, refund GST reversal, net taxable, net GST.
  - HSN `90015000` (Lenses): Gross taxable, gross 5% GST (2.5% CGST + 2.5% SGST), refund taxable, refund GST reversal, net taxable, net GST.
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
- `--execute`, `-x`: Execute safe automated reconciliation routines.
- `--yes`, `-y`: Run non-interactively, bypassing confirmation prompts.

**Recovery Mechanisms:**
- **Terminal Payment Verification**: Queries payment terminal gateway by actual `charge_id` and checks status (never creates duplicate charges).
- **Refund Baseline Re-evaluation**: Compares `refunded_paise` against durable `baseline_refunded_paise` to verify whether refund already executed on terminal before allocating credit notes.
- **Shopify Cloud Reconciliation**: Matches Shopify orders using persisted invoice metadata (`store_code`, `invoice_no`, `checkout_ref`).
- **Location Integrity**: Restocks frames at the original invoice's persisted location ID. If missing, leaves operation pending for supervisor resolution.
- **Credit Note Allocation**: Allocates credit note numbers sequentially only upon verified terminal capture.

---

### Command 7: `invoice show` (Tax Invoice Document Inspection)
Displays the full formatted tax invoice inspection document and line item breakdown in the terminal without exposing sensitive clinical parameters (HTML/PDF export and physical printing are deferred to v1.1).

```bash
python -m eluno_pos invoice show --invoice ELU/HSR/FY27/00001
```

**Options:**
- `--invoice`, `-i`: Tax invoice number. Can also be supplied as a positional argument.

---

## 3. Evaluator Walkthrough (5-Minute Verification)

Follow these steps to evaluate the application end-to-end:

### Step 1: Health Diagnostic
```bash
python -m eluno_pos doctor
```
*Expected: 5/5 checks passed.*

### Step 2: Customer Search
```bash
python -m eluno_pos customer lookup --phone +919000000001
```
*Expected: Displays Rahul Menon, family tag, and prescriptions RX5002 & RX5003 with clinical privacy minimization.*

### Step 3: Complete Guided Checkout
```bash
python -m eluno_pos checkout --store HSR --staff S108 --customer +919000000001 --frame 91000001 --lens 91000041 --yes
```
*Expected: Generates invoice `ELU/HSR/FY27/00001`, captures payment on terminal, posts order to Shopify, prints receipt.*

### Step 4: Inspect Generated Tax Invoice
```bash
python -m eluno_pos invoice show --invoice ELU/HSR/FY27/00001
```
*Expected: Displays invoice with line items, FESTIVE10 discount, exact HSN breakdown (18% and 5%), and payment reference.*

### Step 5: Full Return & Credit Note Issuance
```bash
python -m eluno_pos refund --invoice ELU/HSR/FY27/00001 --staff S108 --reason CUSTOMER_RETURN --yes
```
*Expected: Issues credit note `ELU/HSR/FY27/CN/00001`, reverses terminal charge, restocks frame, scraps lens.*

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
   - Frames: HSN `90031900` @ 18% ($9\%\text{ CGST} + 9\%\text{ SGST}$).
   - Lenses: HSN `90015000` @ 5% ($2.5\%\text{ CGST} + 2.5\%\text{ SGST}$).
   - $\text{Taxable} + \text{CGST} + \text{SGST} = \text{Net Line Amount}$ exactly to the paisa.
3. **Dynamic Financial Year Derivation**: Indian financial year runs from April 1 to March 31. Financial year tokens (e.g. `FY27` for November 2026) are derived dynamically from the transaction date.
4. **Clinical Privacy Protection**: Optical parameters (SPH, CYL, AXIS, ADD, PD) are validated inside the POS engine and stored in clinic systems (SMT). Frontline CLI outputs and non-clinical systems (Shopify Admin API) receive only data-minimized references (`rx_id`), never full clinical eye measurements.
5. **Strict Staff Store Association**: Staff authorization enforces that acting staff members are verified against the SMT registry for the active store. Unknown staff or staff from another store are rejected prior to any financial mutation.
6. **Explicit Non-Interactive Authorization**: The `--yes` flag requires complete explicit transaction arguments and authorizes operations without interactive prompts. Missing parameters abort immediately without demo-data fallbacks.
