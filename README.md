# Eluno POS — Omnichannel Retail Point of Sale

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Test Suite](https://img.shields.io/badge/tests-186%20passed-brightgreen.svg)]()
[![License](https://img.shields.io/badge/license-Proprietary-red.svg)]()

> **Production-grade, domain-driven retail point-of-sale system engineered specifically for optical retail stores.**  
> Built to replace Shopify POS at the till across Eluno's retail stores, integrating clinical prescription validation, automated offer stacking, exact-paisa GST calculation, resilient terminal payments, durable SQLite audit ledgers, full returns with credit notes, and store day-end reconciliation.

---

## Table of Contents

1. [Project Overview & Problem Statement](#1-project-overview--problem-statement)
2. [Key Features Implemented (Must Requirements)](#2-key-features-implemented-must-requirements)
3. [System Architecture](#3-system-architecture)
   - [CLI Layer](#cli-layer-eluno_poscli)
   - [Domain Layer](#domain-layer-eluno_posdomain)
   - [Services Layer](#services-layer-eluno_posservices)
   - [Adapters Layer](#adapters-layer-eluno_posadapters)
   - [Repositories Layer](#repositories-layer-eluno_posrepositories)
4. [Technology Stack & Project Structure](#4-technology-stack--project-structure)
5. [Prerequisites & Setup Instructions (Windows PowerShell)](#5-prerequisites--setup-instructions-windows-powershell)
6. [Mock Server Startup](#6-mock-server-startup)
7. [Command-Line Interface (CLI) Reference](#7-command-line-interface-cli-reference)
   - [System Doctor](#1-system-doctor-health-check)
   - [Customer & Prescription Lookup](#2-customer--optical-prescription-lookup)
   - [Guided & Non-Interactive Checkout](#3-guided--non-interactive-optical-checkout)
   - [Tax Invoice Inspection](#4-tax-invoice-document-inspection)
   - [Full Refund & Frame Restock](#5-store-return--full-refund-workflow)
   - [Day-End Store Financial Report](#6-day-end-store-financial--tax-report)
   - [Transaction Recovery & Reconciliation](#7-transaction-recovery--reconciliation-hub)
8. [Automated Testing Suite](#8-automated-testing-suite)
9. [Important Engineering Decisions](#9-important-engineering-decisions)
   - [Integer Paise Arithmetic & Deterministic GST](#a-integer-paise-arithmetic--deterministic-gst-calculation)
   - [Pure Data Offer Evaluation & Stacking](#b-pure-data-offer-evaluation--stacking)
   - [Payment Safety & Distributed Reconciliation](#c-payment-safety--distributed-reconciliation)
   - [Decoupled Domain with Shopify as System of Record](#d-decoupled-domain-with-shopify-as-system-of-record)
10. [Deferred to Version 1.1](#10-deferred-to-version-11)
11. [Known Limitations & Production Dependencies](#11-known-limitations--production-dependencies)
12. [Documentation & Architectural References](#12-documentation--architectural-references)

---

## 1. Project Overview & Problem Statement

### Context
**Eluno** is a premium omnichannel optical eyewear brand with seven retail stores across Bengaluru (e.g. Mantri Mall, HSR Layout, Bel Road, Koramangala, Jayanagar, Indiranagar, Vega City) and an online channel. Across stores, daily throughput averages 350–400 walk-ins, ~120 bills, and an average order value of ~₹5,400.

In an optical retail workflow, transactions pair an **off-the-shelf physical frame** stocked in the store with a **custom ophthalmic prescription lens** tailored to the customer's clinical eye measurements and manufactured on demand at the central laboratory.

### The Problem with Shopify POS
Eluno previously operated billing on standard **Shopify POS**, which introduced critical operational, financial, and clinical friction:
1. **Manual Promotional Offers:** Sales staff were forced to calculate and apply discounts by hand, leading to floor errors, unauthorized discounts, and margin erosion.
2. **Missing Clinical Guardrails:** Shopify POS has no optical domain knowledge. It permitted incompatible lens choices (e.g. selecting a standard lens for high astigmatism or a progressive lens without an `ADD` power), resulting in costly laboratory rejections and remake delays.
3. **No Per-Store Sequential Tax Invoices:** Indian GST regulations mandate gapless, store-scoped sequential tax invoices (`ELU/{STORE}/{FY}/{SEQ}`). Shopify's global order numbering does not satisfy statutory Indian audit compliance.
4. **GST Rounding Inconsistencies:** Retail eyewear prices are inclusive of GST across varying HSN categories (Frames @ 18%, Prescription Lenses @ 5%). Decimal float arithmetic and independent tax component rounding produced fractional paisa drift across receipts.
5. **Lack of Operational Reporting:** No dependable day-end store report reconciled local cash/terminal receipts with Shopify cloud orders and HSN tax splits.

### The Solution: Eluno POS v1
**Shopify remains the authoritative system of record** for customer accounts, product catalog, inventory levels, order history, and payment transactions. **Eluno POS v1** replaces the till application with a high-performance, domain-driven point of sale that enforces clinical compatibility, automates promotional stacking, guarantees exact-paisa tax math, executes two-phase payment and inventory reconciliation, and provides robust statutory auditing.

---

## 2. Key Features Implemented (Must Requirements)

| # | Must Requirement | Implementation & Architectural Details |
|---|---|---|
| **1** | **Customer & Prescription Lookup** | Queries Shopify for customer profiles by phone; retrieves clinical optical prescriptions from the Store Management Tool (SMT) API (`/smt/prescriptions`). Parses customer tags (`priority`, `family`, `campaign`). Strictly enforces **clinical privacy data minimization**: displays governance metadata (Rx ID, test date, optometrist, validity status, reading ADD presence) while concealing raw refractive powers (SPH, CYL, AXIS, PD) on frontline screens. |
| **2** | **Cart & Lens Matrix Validation** | Implements `LensValidator` to cross-validate clinical prescription parameters against lens index geometry (`rx_limits`). Enforces spherical (SPH) and cylindrical (CYL) boundaries, mandates near-vision reading `ADD` power for progressive/bifocal lenses, and verifies axis alignment. Invalid picks are blocked with clear, actionable staff prompts before invoice creation. |
| **3** | **Automated Offer Engine** | Evaluates date-effective promotional rules from `offers.json` across product, category, pair, and cart tiers. Automatically selects the optimal combination that maximizes customer savings while strictly respecting exclusivity constraints (`allow_stacking: false`). Supports store manager discount overrides (capped at 10%) requiring non-repudiable manager `staff_id` verification and mandatory audit reason codes. |
| **4** | **Date-Effective GST Engine** | Computes exact backward GST extraction from inclusive Maximum Retail Price (MRP) across 5 statutory HSN categories in `tax_rates.json`: Frames (HSN `90031900` @ 18%), Lenses (HSN `90015000` @ 5% post-Sept 2025; 12% prior), Sunglasses (HSN `90049000` @ 18%), Corrective Spectacles (HSN `90041000` @ 5%), and Services (HSN `998599` @ 18%). Guarantees $\text{Taxable} + \text{CGST} + \text{SGST} = \text{Net Line Amount}$ to the exact paisa with zero rounding drift. |
| **5** | **Durable Order Creation & Payment** | Two-phase transaction flow: dispatches an idempotent charge to the payment terminal gateway (`POST /terminal/charges`), allocates a gapless sequential tax invoice (`ELU/{STORE}/{FY}/{SEQ}`) with dynamic Indian Financial Year derivation (April 1 – March 31), and submits the order to the Shopify Admin API carrying line attributes (`pair_ref`, `rx_id`, `hsn`, `for_customer_id`, `wearer_name`) and store invoice numbers in `note_attributes`. |
| **6** | **Full Refund & Targeted Restock** | Reverses completed sales end-to-end: verifies operating staff against SMT store registry, executes an atomic concurrency check (`BEGIN IMMEDIATE`), verifies terminal refund baseline, issues payment reversal on the terminal (`POST /terminal/charges/{id}/refunds`), allocates a sequential credit note (`ELU/{STORE}/{FY}/CN/{SEQ}`), restocks physical frames (+1 unit at billing store location), marks custom lenses as scrapped (`no_restock`), and posts line-level GST reversal to Shopify. |
| **7** | **Day-End Store Reconciliation Report** | Generates consolidated daily settlement reports for store and date: aggregates gross sales, discounts given, net revenue, completed refunds, and net store revenue. Computes itemized HSN tax breakdown (Gross Taxable, Gross GST, Refund Taxable, Refund GST Reversal, Net Taxable, Net GST Payable), payment method split (UPI, Card, Cash), sales channels, and flags cloud discrepancies against live Shopify orders. |

---

## 3. System Architecture

Eluno POS is built using a clean, layered architectural pattern that separates domain invariants from presentation, persistence, and external service communication.

```
┌────────────────────────────────────────────────────────────────────────┐
│                               CLI LAYER                                │
│    argparse Routing • Tabular Formatting • Input Sanitization • UX     │
│   (doctor, customer, checkout, refund, report, reconcile, invoice)     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│                             SERVICES LAYER                             │
│   CheckoutService  •  OfferService  •  GstService  •  RefundService    │
│   CustomerService  •  PrescriptionService  •  DayEndService  •  Sync   │
└──────────────┬────────────────────┬────────────────────┬───────────────┘
               │                    │                    │
┌──────────────▼───────┐  ┌─────────▼──────────┐  ┌──────▼───────────────┐
│     DOMAIN LAYER     │  │ REPOSITORIES LAYER │  │    ADAPTERS LAYER    │
│  Pure Business Logic │  │ SQLite Persistence │  │ HTTP Client (urllib) │
│  Cart, Money, Offers │  │ WAL Mode, Sequences│  │ Exponential Backoff  │
│  Tax Engine, Limits  │  │ Invoices, Refunds  │  │ 429/503 Resilience   │
│  Lens & Rx Models    │  │ Transaction Locks  │  │ Shopify, SMT, Terminal│
└──────────────────────┘  └────────────────────┘  └──────────────────────┘
```

### CLI Layer (`eluno_pos/cli/`)
- **Parser (`parser.py`):** Standard-library `argparse` configuration defining command hierarchy, options, and help text.
- **Context (`context.py`):** Application runtime context (`CliContext`) managing configuration, repositories, and API client singletons.
- **Commands (`commands/`):** Dedicated command handlers for `doctor.py`, `customer.py`, `checkout.py`, `refund.py`, `report.py`, `reconcile.py`, and `invoice.py`.
- **Formatting (`formatting.py`):** Unicode-box tabular renderers, color-neutral status indicators, and currency formatters (`format_inr`).

### Domain Layer (`eluno_pos/domain/`)
- **100% Decoupled from External APIs:** Free from Shopify REST dependencies, third-party frameworks, or database ORMs.
- **Value Objects:** `Money` (strict integer paise arithmetic), `TaxRate`, `LensLimits`, `PrescriptionPowers`.
- **Entities:** `Customer`, `Prescription`, `Cart`, `CartLine`, `Offer`, `Invoice`, `InvoiceLine`, `CreditNote`, `DayEndReport`.
- **Enums:** `InvoiceStatus` (`DRAFT`, `PENDING_PAYMENT`, `PENDING_RECONCILIATION`, `POSTED`, `CANCELLED`, `REFUNDED`), `RefundStatus` (`INITIATED`, `PENDING`, `COMPLETED`, `FAILED`), `PaymentMethod`, `SalesChannel`.
- **Validators:** `LensValidator` ensuring optical limits and compatibility.

### Services Layer (`eluno_pos/services/`)
- **`CheckoutService`:** Coordinates the 15-step retail checkout lifecycle from customer lookup, prescription validation, pairing, promotion stacking, GST calculation, terminal payment capture, invoice allocation, and Shopify cloud order submission.
- **`OfferService`:** Evaluates data-driven promotional rules, calculates stacking combinations, and applies manager discount overrides.
- **`GstService`:** Computes exact backward GST extraction from inclusive MRP, allocates CGST/SGST per HSN code, and prevents dual-rounding drift.
- **`PaymentService`:** Manages terminal transaction lifecycle, idempotent request tracking, and polling verification.
- **`RefundService`:** Manages store returns, concurrency protection, terminal reversal verification, credit note issuance, and targeted frame restocking.
- **`DayEndService`:** Aggregates local transactions, inspects Shopify cloud orders, and builds statutory day-end settlement reports.

### Adapters Layer (`eluno_pos/adapters/`)
- **`ResilientHttpClient`:** Zero-dependency HTTP transport using Python standard-library `urllib.request`. Implements configurable exponential backoff with full jitter to automatically handle HTTP `429` (Rate Limited) and HTTP `503` (Service Unavailable / Gateway Timeout).
- **`ShopifyClient`:** Interacts with Shopify Admin REST API (`/locations.json`, `/products.json`, `/variants.json`, `/customers/search.json`, `/orders.json`, `/orders/{id}/refunds.json`, `/inventory_levels/adjust.json`).
- **`SmtClient`:** Queries the Store Management Tool API for staff records (`/smt/staff`) and customer prescriptions (`/smt/prescriptions`).
- **`TerminalClient`:** Dispatches payment charges (`POST /terminal/charges`), verifies charge status (`GET /terminal/charges/{id}`), and executes refunds (`POST /terminal/charges/{id}/refunds`).

### Repositories Layer (`eluno_pos/repositories/`)
- **`InvoiceRepository`:** High-performance local SQLite storage (`eluno_pos.db`).
- **WAL & Foreign Keys:** Automatically activates Write-Ahead Logging (`PRAGMA journal_mode=WAL`) and referential integrity (`PRAGMA foreign_keys=ON`).
- **Atomic Sequences:** Concurrency-safe sequence counters for store tax invoices (`ELU/{store}/{FY}/{seq}`) and credit notes (`ELU/{store}/{FY}/CN/{seq}`) ensuring gapless audit trails.
- **Family Wearer Persistence:** Accurately stores individual recipient identity (`for_customer_id` and `wearer_name`) per line item.

---

## 4. Technology Stack & Project Structure

- **Runtime:** Python 3.12+ (standard library only for core POS runtime).
- **Database:** SQLite 3 (built into Python standard library).
- **Network Client:** Standard library `urllib.request` (zero external HTTP dependencies).
- **Test Framework:** `pytest` 9.1+ and standard-library `unittest`.

```
eluno-pos/
├── .env.example                     # Environment configuration template
├── README.md                        # Project documentation & operational manual
├── STAKEHOLDER_REPLY.md             # Formal stakeholder scope, questions & clarifications
├── START_HERE.md                    # Initial orientation guide
├── brief/
│   ├── CANDIDATE_BRIEF.md           # Assignment scope, Must/Should/Stretch tiers
│   └── STAKEHOLDER_THREAD.md        # Original stakeholder email discussion
├── docs/
│   └── CLI_GUIDE.md                 # Comprehensive CLI operational guide & manual
├── starter-kit/                     # Original starter kit (UNTOUCHED)
│   ├── data/                        # Seed data (products, offers, taxes, staff, prescriptions)
│   ├── mock-apis/                   # Local mock server for Shopify, SMT & Terminal
│   └── README.md                    # Starter-kit documentation
├── eluno_pos/                       # Core application package
│   ├── __init__.py
│   ├── __main__.py                  # Application entry point (python -m eluno_pos)
│   ├── config.py                    # Environment configuration loader (AppConfig)
│   ├── adapters/                    # External API clients & resilient HTTP transport
│   │   ├── exceptions.py
│   │   ├── http_client.py
│   │   ├── shopify_client.py
│   │   ├── smt_client.py
│   │   └── terminal_client.py
│   ├── cli/                         # Command-line interface implementation
│   │   ├── context.py
│   │   ├── formatting.py
│   │   ├── parser.py
│   │   └── commands/                # Subcommand controllers
│   │       ├── checkout.py
│   │       ├── customer.py
│   │       ├── doctor.py
│   │       ├── invoice.py
│   │       ├── reconcile.py
│   │       ├── refund.py
│   │       └── report.py
│   ├── domain/                      # Pure business models & domain entities
│   │   ├── cart.py
│   │   ├── customer.py
│   │   ├── invoice.py
│   │   ├── lens.py
│   │   ├── money.py
│   │   ├── offer.py
│   │   ├── pairing.py
│   │   ├── payment.py
│   │   ├── prescription.py
│   │   ├── refund.py
│   │   ├── report.py
│   │   ├── tax.py
│   │   └── validation.py
│   ├── repositories/                # Local SQLite persistence
│   │   └── invoice_repository.py
│   └── services/                    # Business orchestration services
│       ├── checkout_service.py
│       ├── customer_service.py
│       ├── day_end_service.py
│       ├── gst_service.py
│       ├── offer_service.py
│       ├── payment_service.py
│       ├── prescription_service.py
│       ├── refund_service.py
│       └── shopify_order_service.py
└── tests/                           # Complete automated test suite (186 tests)
    ├── test_checkout_flow.py
    ├── test_cli.py
    ├── test_cli_e2e.py
    ├── test_customer_service.py
    ├── test_day_end_service.py
    ├── test_gst_service.py
    ├── test_http_client.py
    ├── test_invoice_repository.py
    ├── test_lens_validation.py
    ├── test_mock_integration.py
    ├── test_offer_service.py
    ├── test_pairing.py
    ├── test_payment_service.py
    ├── test_prescription_service.py
    ├── test_refund_service.py
    ├── test_shopify_client.py
    ├── test_shopify_order_service.py
    ├── test_smt_client.py
    └── test_terminal_client.py
```

---

## 5. Prerequisites & Setup Instructions (Windows PowerShell)

### Prerequisites
- **Python:** 3.12 or newer (64-bit recommended).
- **PowerShell:** Windows PowerShell 5.1 or PowerShell 7+.
- **Git:** Installed and available in PATH.

### Step-by-Step Setup

```powershell
# 1. Clone the repository
git clone https://github.com/Bham0603/eluno-pos.git
cd eluno-pos

# 2. Create and activate a Python virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install testing dependencies
python -m pip install --upgrade pip
python -m pip install pytest python-dotenv

# 4. Create local configuration from template
Copy-Item .env.example .env
```

---

## 6. Mock Server Startup

The application communicates with simulated upstream endpoints (Shopify Admin, SMT Optical Clinic, and Payment Terminal Gateway) provided by the starter kit.

In a **separate Windows PowerShell terminal**, start the mock server:

```powershell
# Navigate to repository root and activate virtual environment
cd eluno-pos
.\.venv\Scripts\Activate.ps1

# Start the mock server on port 8787 (deterministic mode)
python starter-kit/mock-apis/mock_server.py --port 8787 --no-flake
```

> [!TIP]
> **Deterministic vs. Chaotic Evaluation:**  
> The `--no-flake` flag disables artificial HTTP 429 rate limits and HTTP 503 gateway timeouts for clean, predictable walkthroughs.  
> To test Eluno POS's real-world network resilience and exponential backoff retry algorithms, run without `--no-flake`:
> ```powershell
> python starter-kit/mock-apis/mock_server.py --port 8787
> ```

---

## 7. Command-Line Interface (CLI) Reference

All operations are executed via the standard module entry point:
```powershell
python -m eluno_pos [COMMAND] [OPTIONS]
```

### 1. System Doctor (Health Check)
Verifies local database schema, configuration, and connectivity to all upstream APIs without initiating financial mutations:
```powershell
python -m eluno_pos doctor
```

### 2. Customer & Optical Prescription Lookup
Searches customer profile by phone, displaying membership cohorts, active tags, and clinical prescriptions on file while strictly concealing raw refractive powers:
```powershell
python -m eluno_pos customer lookup --phone +919000000001
```

### 3. Guided & Non-Interactive Optical Checkout
Executes the guided 15-step retail optical checkout lifecycle.

**Non-Interactive Scripted Execution (`--yes`):**
Requires complete explicit transaction inputs (`--store`, `--staff`, `--customer`, `--frame`, `--lens`); no silent demo defaults are assumed:
```powershell
python -m eluno_pos checkout --store HSR --staff S108 --customer +919000000001 --frame 91000001 --lens 91000041 --yes
```

**Interactive Guided Execution:**
Walks the operator through store selection, staff login, customer search, clinical prescription selection, frame and lens pairing, compatibility check, promotional discount summary, and explicit payment authorization:
```powershell
python -m eluno_pos checkout
```

### 4. Tax Invoice Document Inspection
Displays the official store tax invoice, itemized line items, applied promotional discounts, HSN tax breakdown, and payment audit details:
```powershell
python -m eluno_pos invoice show --invoice ELU/HSR/FY27/00001
```

### 5. Store Return & Full Refund Workflow
Processes full return of an existing invoice: verifies staff authorization against the store registry, acquires an atomic transaction lock, issues a payment reversal to the terminal, generates a sequential Credit Note, restocks the physical frame, and scraps the custom lens:
```powershell
python -m eluno_pos refund --invoice ELU/HSR/FY27/00001 --staff S108 --reason CUSTOMER_RETURN --yes
```

### 6. Day-End Store Financial & Tax Report
Consolidates the day's local sales, discounts, refunds, and net tax liabilities, and cross-checks them against live Shopify cloud orders:
```powershell
python -m eluno_pos report --store HSR --date 2026-11-05
```

### 7. Transaction Recovery & Reconciliation Hub
Inspects in-flight or ambiguous transactions and resolves pending operations without issuing duplicate terminal charges or duplicate orders:
```powershell
python -m eluno_pos reconcile --store HSR
```

---

## 8. Automated Testing Suite

The repository contains an exhaustive automated test suite covering unit math, service orchestration, resilient HTTP retries, SQLite concurrency, and end-to-end CLI scenarios.

### Running with Pytest
```powershell
python -m pytest tests/ -q
```
**Result:** `186 passed in ~3 minutes`

### Running with Standard Library Unittest
```powershell
python -m unittest discover -s tests -v
```
**Result:** `Ran 186 tests ... OK`

### Test Suite Structure
| Test Module | Focus Area |
|---|---|
| `test_gst_service.py` | Exact-paisa backward GST math, HSN rates, zero rounding drift, and balance preservation. |
| `test_offer_service.py` | Rule conditions, stackability constraints, date eligibility, and 10% manager overrides. |
| `test_lens_validation.py` | SPH/CYL boundaries, progressive `ADD` requirement, axis validation, and error messaging. |
| `test_payment_service.py` | Terminal charge dispatch, polling, idempotency, and error handling. |
| `test_refund_service.py` | Concurrency safety (`BEGIN IMMEDIATE`), baseline tracking, credit note allocation, frame restocking. |
| `test_shopify_order_service.py` | Order payload creation, line item properties (`pair_ref`, `rx_id`), and HSN attributes. |
| `test_invoice_repository.py` | SQLite schema, WAL mode, foreign keys, gapless sequences, and state transitions. |
| `test_day_end_service.py` | Multi-system financial aggregation, payment splits, and Shopify cloud discrepancy detection. |
| `test_http_client.py` | Resilient HTTP transport, exponential backoff, and full jitter for 429 and 503 errors. |
| `test_cli.py` & `test_cli_e2e.py` | Command routing, argument validation, privacy minimization, and end-to-end user journeys. |

---

## 9. Important Engineering Decisions

### A. Integer Paise Arithmetic & Deterministic GST Calculation
- **Integer Representation:** All internal calculations and database storage use integer paise ($1 = 100\text{ paise}$). Float types are strictly forbidden in monetary logic to prevent binary floating-point representation artifacts.
- **Backward Tax Extraction:** Because Indian retail optical prices are inclusive of GST, the taxable amount is extracted backward from the net inclusive line amount:
  $$\text{Taxable Paise} = \text{round}\left(\frac{\text{Net Paise} \times 10000}{10000 + \text{rate\_bps}}\right)$$
- **Zero-Drift Component Allocation:** Total tax is computed as $\text{Total GST} = \text{Net Paise} - \text{Taxable Paise}$. Central GST (CGST) is derived via $\text{round}(\frac{\text{Total GST}}{2})$, and State GST (SGST) is the exact remainder ($\text{Total GST} - \text{CGST}$). This single-component rounding guarantees that $\text{Taxable} + \text{CGST} + \text{SGST} = \text{Net Price}$ to the exact paisa across all invoices and refunds.

### B. Pure Data Offer Evaluation & Stacking
- **Zero Code Changes for New Offers:** Promotional rules in `offers.json` are evaluated dynamically.
- **Customer Benefit Optimization:** The offer engine evaluates product, category, pair, and cart-level rules. If a non-stackable offer yields greater savings than a combination of stackable offers, the engine automatically selects the configuration maximizing customer savings.
- **Audited Manager Overrides:** Floor managers can grant discretionary overrides capped at 10% (1000 bps). Overrides require an authorizing manager's `staff_id` (verified against `can_override_discount: true` in `staff.json`) and a mandatory reason code (`WALKOUT_RESCUE`, `PRICE_MATCH`, `DAMAGE_ALLOWANCE`). Shared PINs are completely prohibited.

### C. Payment Safety & Distributed Reconciliation
- **Two-Phase Commit Pattern:** A local transaction intent is recorded with a generated invoice number before contacting the payment gateway. The charge request sends `order_ref = invoice_no` as an idempotency key.
- **Durable `PENDING_RECONCILIATION` State:** Upon terminal payment confirmation, the local invoice transitions to `PENDING_RECONCILIATION`. If the network drops or Shopify times out during order creation, the transaction is **never marked as cancelled or deleted**. The invoice number is never reused.
- **Safe Recovery:** Automated reconciliation checks Shopify cloud orders by store location and matching `note_attributes` to recover state without ever replaying non-idempotent payment charges or creating duplicate orders.
- **Atomic Concurrency for Refunds:** Refund operations acquire a `BEGIN IMMEDIATE` SQLite transaction lock. Concurrent refund requests for the same invoice are rejected safely. Durably stored baseline `refunded_paise` ensures ambiguous terminal responses cannot cause double-refunds.

### D. Decoupled Domain with Shopify as System of Record
- **Domain Independence:** The domain layer defines its own agnostic models (`Cart`, `Invoice`, `Customer`, `Prescription`). External systems (Shopify, SMT, Terminal) are reached exclusively through adapter boundaries. If Shopify is migrated or replaced in the future, domain logic remains completely untouched.
- **Lineage Properties:** Custom prescription lenses carry minimal tracking tokens (`pair_ref`, `rx_id`, `for_customer_id`, `wearer_name`) in Shopify line item properties, enabling downstream laboratory Order Management Systems (OMS) to match frames and lenses without exposing raw optical measurements on public platforms.

---

## 10. Deferred to Version 1.1

In accordance with the project scope matrix and stakeholder agreements, the following non-blocking capabilities are formally deferred to Version 1.1:

1. **Cash Till Sessions & Physical Drawer Tracking (Should):**
   - Opening float balance entry.
   - Petty cash drop recording (paid-ins and paid-outs for local store expenses like tea, courier).
   - End-of-shift physical counted cash reconciliation and cash variance reporting.
2. **Sales Channel Attribution (Should):**
   - Tagging transactions at checkout with explicit sales channels (`walk_in`, `whatsapp_booked`, `online_pickup`).
   - Store-level sales channel performance reporting.
3. **Partial Return Workflows (Should):**
   - Processing partial refunds (e.g. customer returns frame but retains custom-cut lenses, or lens remake credit).
4. **Laboratory Remake Management (Stretch):**
   - Commercial liability allocation (lab manufacturing defect vs. store optometrist measurement error vs. customer prescription intolerance).
   - Billing replacement lenses referencing `parent_invoice_no`.
5. **Cross-Account Family Loyalty:**
   - Aggregating loyalty reward tiers across distinct family member phone profiles.
6. **Automated 10:00 AM Daily Email Digest:**
   - Standalone scheduled backend cron querying Shopify Admin API to email executive summaries to leadership.
7. **Hardware Peripheral Drivers:**
   - Direct serial communication with USB/Bluetooth barcode scanners and ESC/POS thermal receipt printers.

---

## 11. Known Limitations & Production Dependencies

Prior to launching Eluno POS v1 in live physical retail stores, the following operational and third-party dependencies must be established:

1. **Production Shopify Credentials:**
   - Requires a dedicated Shopify Custom App access token configured with production scopes: `read_products`, `read_customers`, `read_inventory`, `write_inventory`, `read_orders`, and `write_orders`.
2. **Production Staff Authentication:**
   - Transitioning from the local SMT registry to enterprise OAuth2 / OpenID Connect, JWT session tokens, or physical RFID staff badge scanners.
3. **Statutory GST E-Invoicing & IRN Generation:**
   - Direct integration with the Government of India Invoice Registration Portal (IRP/NIC) for generating Invoice Reference Numbers (IRN) and signed B2B/B2C QR codes on tax invoices exceeding statutory turnover thresholds.
4. **Physical Hardware Peripheral Integration:**
   - Native integration with ESC/POS thermal receipt printers (receipt formatting, logo graphics, paper cutter commands) and USB HID / Bluetooth barcode scanners.
5. **Cash Drawer Tracking:**
   - Hardware trigger pulse (RJ11/RJ12 drawer kick-out) on cash sale completion.
6. **Sales Channel Tagging:**
   - Omnichannel attribution pipeline linking WhatsApp CRM and appointment booking webhooks.
7. **Lab Remake Tracking:**
   - Centralized laboratory remake dispatch workflow.

---

## 12. Documentation & Architectural References

- **[CLI User Guide & Operational Manual](docs/CLI_GUIDE.md):** Complete operational reference detailing all CLI commands, lifecycle invariants, evaluator walkthroughs, and error recovery protocols.
- **[Stakeholder Scoped Plan & Clarifications](STAKEHOLDER_REPLY.md):** Formal pre-engineering memorandum addressing stakeholder requirements, risk mitigations, scope boundaries, and architectural trade-offs.
- **[Starter-Kit Seed Data & Schemas](starter-kit/schema.md):** Detailed field definitions for products, lens matrices, customer cohorts, and tax rates.

