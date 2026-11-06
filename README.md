# Eluno POS — Omnichannel Retail Point of Sale

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Test Suite](https://img.shields.io/badge/tests-213%20passed-brightgreen.svg)]()
[![License](https://img.shields.io/badge/license-Proprietary-red.svg)]()

> **Technical Assessment Prototype & Reference Implementation for Optical Retail.**  
> Engineered as a domain-driven prototype to evaluate replacing Shopify POS at the till across Eluno's retail stores. Integrates clinical prescription validation, automated offer stacking from data, exact-paisa GST calculation, resilient terminal payments, durable SQLite audit ledgers, full returns with credit notes and targeted restock, and store day-end reconciliation against local mock APIs.

---

## Table of Contents

1. [Project Overview & Problem Statement](#1-project-overview--problem-statement)
2. [Scope Audit: Must, Should, and Stretch Requirements](#2-scope-audit-must-should-and-stretch-requirements)
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
   - [Durable Outbox & Reconciled Transaction Workflow (Not 2PC)](#c-durable-outbox--reconciled-transaction-workflow-not-2pc)
   - [Decoupled Domain & Shopify Schema Alignment](#d-decoupled-domain--shopify-schema-alignment)
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

### The Solution: Eluno POS Assessment Prototype
**Shopify remains the authoritative system of record** for customer accounts, product catalog, inventory levels, order history, and payment transactions. **Eluno POS** is a technical assessment prototype engineered to evaluate replacing the till application with a domain-driven point of sale that communicates with simulated upstream services via local mock APIs (`http://localhost:8787`). It enforces clinical compatibility, automates promotional stacking from data, guarantees exact-paisa tax math, executes durable terminal payment and state reconciliation, and provides robust statutory auditing. (Upstream integrations currently target the starter-kit mock server; production deployment requires establishing real credentials, hardware drivers, and compliance services detailed in [§11](#11-known-limitations--production-dependencies)).

---

## 2. Scope Audit: Must, Should, and Stretch Requirements

The table below provides a complete, transparent audit of all requirements defined in `brief/CANDIDATE_BRIEF.md` and `brief/STAKEHOLDER_THREAD.md`, declaring exact implementation status, architectural details, and deferred boundaries.

### A. Must Requirements (Core Till Capabilities) — 100% IMPLEMENTED

| # | Requirement | Status | Implementation & Architectural Details |
|---|---|---|---|
| **1** | **Customer & Prescription Lookup** | **IMPLEMENTED** | Queries Shopify for customer profiles by phone; retrieves clinical optical prescriptions from the Store Management Tool (SMT) API (`/smt/prescriptions`). Parses customer tags (`priority`, `family`, `campaign`). Strictly enforces **clinical privacy data minimization**: displays governance metadata (Rx ID, test date, optometrist, validity status, reading ADD presence) while concealing raw refractive powers (SPH, CYL, AXIS, PD) on frontline screens. |
| **2** | **Cart & Lens Matrix Validation** | **IMPLEMENTED** | Implements `LensValidator` to cross-validate clinical prescription parameters against lens index geometry (`rx_limits`). Enforces spherical (SPH) and cylindrical (CYL) boundaries, mandates near-vision reading `ADD` power for progressive/bifocal lenses, and verifies axis alignment. Invalid picks are blocked with clear, actionable staff prompts before invoice creation. |
| **3** | **Automated Offer Engine** | **IMPLEMENTED** | Evaluates date-effective promotional rules from `offers.json` across product, category, pair, and cart tiers. Automatically selects the optimal combination that maximizes customer savings while strictly respecting exclusivity constraints (`allow_stacking: false`). Floor manager overrides (capped at 10%) require non-repudiable manager `staff_id` verification (`can_override_discount: true`) and a mandatory, validated audit reason code (`CUSTOMER_RETENTION`, `PRICE_MATCH`, `WALKOUT_RESCUE`, `DAMAGE_ALLOWANCE`). |
| **4** | **Date-Effective GST Engine** | **IMPLEMENTED** | Computes exact backward GST extraction from inclusive Maximum Retail Price (MRP) across 5 statutory HSN categories in `tax_rates.json`: Frames (HSN `90031900` @ 18%), Lenses (HSN `90015000` @ 5% post-Sept 2025; 12% prior), Sunglasses (HSN `90049000` @ 18%), Corrective Spectacles (HSN `90041000` @ 5%), and Services (HSN `998599` @ 18%). Guarantees $	ext{Taxable} + 	ext{CGST} + 	ext{SGST} = 	ext{Net Line Amount}$ to the exact paisa with zero rounding drift. |
| **5** | **Durable Order Creation & Payment** | **IMPLEMENTED** | Durable transaction workflow: atomically allocates a gapless sequential store tax invoice (`ELU/{STORE}/{FY}/{SEQ}`) with dynamic Indian Financial Year derivation (April 1 – March 31), initially persisted in SQLite with status `PENDING_RECONCILIATION`. A payment attempt is separately persisted in SQLite (`payment_attempts`) as `INITIATED` using a stable, unique `order_ref`. The payment charge is dispatched to the terminal gateway (`POST /terminal/charges`); on successful capture, the attempt records `CAPTURED` and stores the gateway `charge_id` (definitive pre-capture failure transitions invoice to `CANCELLED`). A Shopify order intent is recorded in SQLite (`shopify_order_intents`) before submitting the order to Shopify Admin API carrying line attributes (`pair_ref`, `rx_id`, `hsn`, `for_customer_id`, `wearer_name`) and store invoice numbers in `note_attributes`. Confirmed Shopify creation transitions the invoice to `POSTED`. Ambiguous terminal or Shopify outcomes remain `PENDING_RECONCILIATION`. The `reconcile` routine searches Shopify recent orders by location and links a matching existing order by `note_attributes` without automatically resubmitting missing orders or claiming exactly-once guarantees. |
| **6** | **Full Refund & Targeted Restock** | **IMPLEMENTED** | Reverses completed sales end-to-end: verifies operating staff against SMT store registry, executes an atomic concurrency check (`BEGIN IMMEDIATE`), verifies terminal refund baseline, issues payment reversal on the terminal (`POST /terminal/charges/{id}/refunds`), allocates a sequential credit note (`ELU/{STORE}/{FY}/CN/{SEQ}`), restocks physical frames (+1 unit at billing store location), marks custom lenses as scrapped (`no_restock`), and posts line-level GST reversal to Shopify. |
| **7** | **Day-End Store Reconciliation Report** | **IMPLEMENTED** | Generates consolidated daily settlement reports for store and date from Shopify cloud orders: aggregates gross sales, discounts given, net revenue, completed refunds, and net store revenue. Computes itemized HSN tax breakdown (Gross Taxable, Gross GST, Refund Taxable, Refund GST Reversal, Net Taxable, Net GST Payable), payment method split (UPI, Card, Cash), sales channels, and flags cloud discrepancies against live Shopify orders. |

### B. Should Requirements (Operational Enhancements)

| # | Requirement | Status | Current Reality & Scope Boundary |
|---|---|---|---|
| **1** | **Cash Till Sessions & Drawer Tracking** | **DEFERRED** | Sample till session data is provided in the starter kit (`starter-kit/data/till_sessions_sample.json`). Operational cash till sessions, float balance entry, petty cash recording (paid-ins/paid-outs), physical cash drawer tracking, and drawer variance calculation are deferred to v1.1; no application-level till session domain entities or till reporting calculations are implemented in v1. |
| **2** | **Sales Channel Attribution & Reporting** | **PARTIALLY IMPLEMENTED** | Day-end store reporting filters and aggregates sales channels from Shopify orders (`walk_in`, `whatsapp_booked`, `online_pickup`). Till checkout currently defaults to `channel="walk_in"`. Interactive till-side selection of `whatsapp_booked` and `online_pickup` at the point of sale is deferred to v1.1. |
| **3** | **Partial Refund (Frame-only or Lens-only)** | **DEFERRED** | V1 implements complete, atomic full returns with credit notes and targeted restock. Granular item-level returns (e.g. customer keeping frame but returning lenses for remake credit) are deferred to v1.1. |
| **4** | **Printable Invoice (HTML or PDF)** | **DEFERRED** | V1 implements rich, formatted Unicode terminal inspection cards for tax invoices and receipts (`invoice show` and post-checkout receipt card). Standalone HTML/PDF invoice generation and native ESC/POS thermal printer communication are deferred to v1.1. |
| **5** | **Staff Audit Trail with Mandatory Reason Codes** | **IMPLEMENTED** | Mandatory, validated reason codes are strictly enforced for both manager discount overrides (`CUSTOMER_RETENTION`, `PRICE_MATCH`, `WALKOUT_RESCUE`, `DAMAGE_ALLOWANCE`) and full store returns (`CUSTOMER_RETURN`, `DEFECTIVE_FRAME`, `INCORRECT_PRESCRIPTION`). Operating staff ID is verified against the SMT registry. |

### C. Stretch Requirements & Stakeholder Clarifications

| Feature / Request | Status | Architectural Scope Boundary |
|---|---|---|
| **Offer Authoring from Plain English (NLP)** | **DEFERRED** | Natural language prompt compilation into validated JSON promotional rules is deferred. Promotional rules are maintained in structured, pure-data format in `starter-kit/data/offers.json`. |
| **Prescription Entry Assist (Computer Vision)** | **DEFERRED** | OCR / photo-to-structured Rx extraction is deferred. Prescriptions are retrieved digitally from SMT (`/smt/prescriptions`). |
| **Billing-Time Anomaly Flags** | **DEFERRED** | Diagnostic anomaly detection (e.g. >1.00 D jump across tests, first-time progressive warnings) is deferred. |
| **Single-Pair Wearer Attribution** | **IMPLEMENTED** | Supports attributing a pair to a specific wearer/recipient via `--for-member` (name string) and `--for-customer-id` (persisted integer customer ID). Line attributes carry `wearer_name` and `for_customer_id` without polluting purchaser identity. |
| **Multi-Pair Single-Invoice Family Checkout** | **DEFERRED** | Multi-pair single-invoice checkout with multiple distinct clinical prescriptions (e.g. family of three with three different prescriptions on one invoice) is deferred to v1.1. Single-pair checkout is fully supported. |
| **Laboratory Remake Management** | **DEFERRED** | Commercial liability allocation (lab manufacturing defect vs. store optometrist measurement error vs. customer intolerance) and zero-charge replacement lens billing are deferred to v1.1. |
| **Automated 10:00 AM Daily Email Digest** | **DEFERRED** | Scheduled email reporting is an infrastructure cron service responsibility querying Shopify Admin API directly, outside the frontline POS till client. |

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
- **Enums:** `InvoiceStatus` (`POSTED`, `PENDING_RECONCILIATION`, `CANCELLED`), `PaymentStatus` (`INITIATED`, `CAPTURED`, `FAILED`, `UNKNOWN`), `RefundStatus` (`INITIATED`, `TERMINAL_REFUNDED`, `COMPLETED`, `FAILED`, `PENDING_RECONCILIATION`).
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
└── tests/                           # Complete automated test suite (213 tests across 20 modules)
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
    ├── test_mock_server_status.py
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

# 3. Install testing dependencies (pytest only)
python -m pip install --upgrade pip
python -m pip install pytest

# 4. Create local configuration from template
Copy-Item .env.example .env
```

> [!NOTE]
> **Zero Runtime Dependencies:**  
> The core application runtime uses only the Python standard library. Configuration is loaded by `eluno_pos/config.py`, which parses `.env` directly without third-party dependencies like `python-dotenv`. `pytest` is the only external dependency, used solely for executing the automated test suite.

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
Requires complete explicit transaction inputs (`--store`, `--staff`, `--customer`, `--frame`, `--lens`); no silent demo defaults are assumed. Attributing a pair to a family member is supported via `--for-member` and `--for-customer-id` (multi-pair family checkout with distinct prescriptions on one invoice is deferred to v1.1). Channel defaults to `walk_in`. Receipts are rendered as structured terminal cards (physical ESC/POS thermal printing and HTML/PDF invoices are deferred):
```powershell
python -m eluno_pos checkout --store HSR --staff S108 --customer +919000000001 --frame 91000001 --lens 91000041 --yes
```

**Interactive Guided Execution:**
Walks the operator through store selection, staff login, customer search, clinical prescription selection, frame and lens pairing, compatibility check, promotional discount summary, and explicit payment authorization:
```powershell
python -m eluno_pos checkout
```

### 4. Tax Invoice Document Inspection
Displays the formatted store tax invoice inspection document in the terminal, detailing itemized line items, applied promotional discounts, HSN tax breakdown, and payment audit details (standalone HTML/PDF export is deferred to v1.1):
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
**Result:** `213 passed in ~4.5 minutes`

### Running with Standard Library Unittest
```powershell
python -m unittest discover -s tests -v
```
**Result:** `Ran 213 tests ... OK`

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
| `test_mock_integration.py` & `test_mock_server_status.py` | Multi-service integration flows, mock server order filtering, IST date filtering, and status codes. |
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
- **Audited Manager Overrides:** Floor managers can grant discretionary overrides capped at 10% (1000 bps). Overrides require an authorizing manager's `staff_id` (verified against `can_override_discount: true` in `staff.json`) and a mandatory reason code (`CUSTOMER_RETENTION`, `PRICE_MATCH`, `WALKOUT_RESCUE`, `DAMAGE_ALLOWANCE`). Shared PINs are completely prohibited.

### C. Durable Outbox & Reconciled Transaction Workflow (Not 2PC)
- **Durable Intent & Idempotency (Not Two-Phase Commit):** The application does not execute a distributed Two-Phase Commit (2PC) protocol across heterogeneous external systems, as Shopify Admin API and the payment terminal do not support XA/prepare-commit coordinators. Furthermore, it does not claim distributed exactly-once delivery guarantees. Instead, it implements a resilient saga/outbox transaction pattern:
  1. **Invoice Allocation (`PENDING_RECONCILIATION`):** An invoice is atomically allocated in SQLite using `BEGIN IMMEDIATE` and initially persisted with status `PENDING_RECONCILIATION` and a unique sequential store invoice number (`ELU/{STORE}/{FY}/{SEQ}`).
  2. **Payment Attempt Initiation (`INITIATED`):** A payment attempt record is separately persisted in SQLite (`payment_attempts`) with status `INITIATED` using a stable, unique `order_ref` (tied to the invoice).
  3. **Terminal Payment Capture:** The payment charge is dispatched to the terminal gateway using the stable `order_ref` as an idempotency key.
     - On successful terminal capture, the payment attempt records `CAPTURED` and durably stores the gateway `charge_id`.
     - On definitive pre-capture failure (e.g., card declined or aborted before terminal capture), the invoice transitions to `CANCELLED`.
     - On ambiguous terminal outcomes (e.g., socket timeout), the invoice remains in `PENDING_RECONCILIATION`.
  4. **Shopify Order Intent (`PENDING`):** Before calling Shopify Admin API, a Shopify order intent is recorded in SQLite (`shopify_order_intents`) with status `PENDING`.
  5. **Shopify Order Creation:** The paid order is submitted to Shopify Admin API carrying the store invoice number and store code in `note_attributes`.
     - Upon confirmed order creation, the intent is updated to `COMPLETED` and the invoice transitions to `POSTED`.
     - If Shopify order creation encounters an ambiguous outcome (network drop, timeout, or 503), the intent is marked `AMBIGUOUS` and the invoice remains in `PENDING_RECONCILIATION`.
- **Durable `PENDING_RECONCILIATION` State:** Under no circumstances is a paid invoice marked `CANCELLED` or deleted when an upstream sync is ambiguous (as customer funds have already been captured). The invoice number is never reused or orphaned.
- **Payment Recovery via Reconciliation:** The `reconcile` routine queries recent Shopify cloud orders for that store location and inspects `note_attributes` for matching `invoice_no` or `checkout_ref`.
  - If a matching existing order is found in Shopify, reconciliation links the `shopify_order_id`, marks the intent `RECONCILED`, and transitions the invoice to `POSTED`.
  - If no matching order is found, the transaction remains in `PENDING_RECONCILIATION` for supervisor review. Missing orders are not automatically resubmitted to Shopify, preventing duplicate order placement or inventory decrements.
- **Atomic Concurrency for Refunds:** Refund operations acquire a `BEGIN IMMEDIATE` SQLite transaction lock. Concurrent refund requests for the same invoice are rejected safely. Durably stored baseline `refunded_paise` ensures ambiguous terminal responses cannot cause double-refunds.

### D. Decoupled Domain & Shopify Schema Alignment
- **Domain Independence:** The domain layer defines its own agnostic value objects and models (`Cart`, `Invoice`, `Money`, `LensLimits`, `TaxRate`). Domain logic operates independently of external API SDKs, ORMs, or Shopify-specific data structures.
- **Shopify Schema Alignment in Adapters:** While domain entities are decoupled, `ShopifyOrderService` and adapter layers intentionally structure payloads, tax lines, note attributes (`store_code`, `invoice_no`), and line item properties (`pair_ref`, `rx_id`, `for_customer_id`, `wearer_name`, `channel`) to strictly match Shopify Admin REST API specifications. Complete provider independence would require an additional intermediate abstraction layer if migrating away from Shopify.
- **Lineage & Privacy Protection:** Custom prescription lenses carry minimal tracking tokens in Shopify line item properties, enabling downstream laboratory Order Management Systems (OMS) to match frames and lenses without exposing raw clinical optical measurements on public platforms.

---

## 10. Deferred to Version 1.1

In accordance with the project scope matrix and stakeholder agreements, the following non-blocking capabilities are formally deferred to Version 1.1:

1. **Multi-Pair Single-Invoice Family Checkout (Stakeholder Request):**
   - Single-pair wearer attribution (`for_customer_id`, `wearer_name`) is implemented.
   - Multi-pair single-invoice checkout with multiple distinct clinical prescriptions (e.g. family of three with 3 different prescriptions on one invoice) is deferred.
2. **Cash Till Sessions & Physical Drawer Tracking (Should):**
   - Sample till session data exists in starter-kit (`starter-kit/data/till_sessions_sample.json`).
   - Interactive opening float balance entry workflow.
   - Petty cash drop recording (paid-ins and paid-outs for local store expenses like tea, courier).
   - End-of-shift physical counted cash reconciliation and cash variance reporting.
   - Operational cash till functionality is deferred to v1.1; no application-level till domain entities or till reporting calculations are implemented in v1.
3. **Till-Side Sales Channel Selection (Should):**
   - Interactive selection of sales channels (`whatsapp_booked`, `online_pickup`) at till checkout (currently defaults to `channel="walk_in"`; day-end reporting groups by channel if present on orders).
4. **Partial Return Workflows (Should):**
   - Granular item-level returns (e.g. customer returning frame but retaining custom-cut lenses, or lens remake credit). V1 implements complete, atomic full returns.
5. **Printable Invoices & Physical Hardware Peripheral Drivers (Should):**
   - Generation of standalone HTML/PDF tax invoice documents.
   - Direct communication with ESC/POS thermal receipt printers and USB/Bluetooth barcode scanners. (V1 renders structured Unicode terminal inspection cards).
6. **Laboratory Remake Management (Stakeholder Request):**
   - Commercial liability allocation (lab manufacturing defect vs. store optometrist measurement error vs. customer prescription intolerance).
   - Billing replacement lenses referencing `parent_invoice_no`.
7. **Cross-Account Family Loyalty:**
   - Aggregating loyalty reward tiers across distinct family member phone profiles.
8. **Automated 10:00 AM Daily Email Digest:**
   - Standalone scheduled backend cron querying Shopify Admin API to email executive summaries to leadership.

---

## 11. Known Limitations & Production Dependencies

Eluno POS is currently engineered and validated as a technical assessment prototype communicating with local mock APIs (`http://localhost:8787`). Prior to deploying in live physical retail stores, the following operational, compliance, and infrastructure prerequisites must be established:

1. **Production Shopify Admin API Credentials & Private App:**
   - Dedicated Shopify Custom App access token with production scopes: `read_products`, `read_customers`, `read_inventory`, `write_inventory`, `read_orders`, and `write_orders`.
   - Verified API rate-limit tiers (Shopify Plus / standard leaky bucket limits) to support peak retail footfall without throttling.
2. **Production Payment Terminal Integration:**
   - Real-world payment gateway SDK/API integration (e.g. Pine Labs Plutus, Razorpay POS, or Innoviti) replacing the local `/terminal` mock.
   - Hardware card reader / terminal tethering (USB/Ethernet/Wi-Fi) with terminal transaction status webhooks.
3. **Production Staff Authentication & SSO:**
   - Transition from the local SMT JSON staff registry to enterprise OAuth2 / OpenID Connect (OIDC), SAML SSO, or biometric/RFID staff login at the till.
4. **Statutory GST E-Invoicing & IRN Generation:**
   - Direct API integration with the Government of India Invoice Registration Portal (IRP/NIC or authorized GSP) for generating Invoice Reference Numbers (IRN) and digitally signed B2B/B2C QR codes on tax invoices exceeding statutory turnover thresholds.
5. **Physical POS Hardware Peripheral Drivers:**
   - Native serial/USB ESC/POS driver integration for thermal receipt printers (raster graphics, cutter controls).
   - Cash drawer kick-out interface (RJ11/RJ12 24V solenoid pulse) triggered upon cash sale capture.
   - USB HID / Bluetooth barcode scanners configured for serial/batch barcode scanning.
6. **Central Optical Laboratory OMS Webhooks:**
   - Integration with Eluno's central laboratory Order Management System (OMS) to dispatch frame SKUs, lens geometry, and optical measurements for production upon order creation.

---

## 12. Documentation & Architectural References

- **[CLI User Guide & Operational Manual](docs/CLI_GUIDE.md):** Complete operational reference detailing all CLI commands, lifecycle invariants, evaluator walkthroughs, and error recovery protocols.
- **[Stakeholder Scoped Plan & Clarifications](STAKEHOLDER_REPLY.md):** Formal pre-engineering memorandum addressing stakeholder requirements, risk mitigations, scope boundaries, and architectural trade-offs.
- **[Starter-Kit Seed Data & Schemas](starter-kit/schema.md):** Detailed field definitions for products, lens matrices, customer cohorts, and tax rates.

