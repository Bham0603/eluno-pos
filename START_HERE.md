# Eluno POS — Evaluator & Quick-Start Guide

Welcome to **Eluno POS**, a domain-driven point-of-sale technical assessment prototype engineered specifically for optical retail stores.

This implementation replaces the till billing interface of Shopify POS, connecting to the provided local mock APIs (`http://localhost:8787`) to enforce clinical optical compatibility, automate promotional discounting from data, calculate exact-paisa GST by HSN, ensure durable terminal payment and state reconciliation, execute full returns with credit notes and targeted restock, and generate day-end store audit reports.

---

## 1. Documentation Index

- **[`README.md`](README.md):** Primary architectural overview, complete Must/Should/Stretch scope declaration matrix, detailed design decisions, and system specifications.
- **[`docs/CLI_GUIDE.md`](docs/CLI_GUIDE.md):** Complete operator manual and CLI reference covering all 7 commands, options, safety invariants, and a 5-minute evaluator walkthrough.
- **[`STAKEHOLDER_REPLY.md`](STAKEHOLDER_REPLY.md):** Part A formal pre-engineering memorandum answering stakeholder questions, scoping trade-offs, and production launch dependencies.
- **[`brief/CANDIDATE_BRIEF.md`](brief/CANDIDATE_BRIEF.md):** Original exercise brief defining Must, Should, and Stretch requirements.
- **[`starter-kit/schema.md`](starter-kit/schema.md):** Data schemas for products, lens matrices, customer cohorts, and tax rates.

---

## 2. Quick Start (Windows PowerShell)

### Step 1: Environment Setup
```powershell
# Clone repository and navigate to root
git clone https://github.com/Bham0603/eluno-pos.git
cd eluno-pos

# Create and activate Python virtual environment (Python 3.12+)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install required testing dependencies (pytest only)
python -m pip install --upgrade pip
python -m pip install pytest

# Initialize local configuration
Copy-Item .env.example .env
```

> [!NOTE]
> The core application runtime uses only the Python standard library; `eluno_pos/config.py` loads `.env` directly without external packages like `python-dotenv`. `pytest` is required only for executing the test suite.

### Step 2: Start Mock Server (Terminal 1)
In a dedicated PowerShell window, start the starter-kit mock server (deterministic mode):
```powershell
.\.venv\Scripts\Activate.ps1
python starter-kit/mock-apis/mock_server.py --port 8787 --no-flake
```
*(To test real-world chaotic network resilience with simulated 429 rate limits and 503 gateway timeouts, omit `--no-flake`.)*

### Step 3: Run Full Automated Test Suite (Terminal 2)
```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest tests/ -q
```
**Expected Outcome:** `213 passed` across 20 test files.

### Step 4: 5-Minute Evaluator CLI Walkthrough (Terminal 2)

```powershell
# 1. System Doctor Health Check (read-only verification of local DB and mock APIs)
python -m eluno_pos doctor

# 2. Customer & Prescription Search (with clinical privacy data minimization)
python -m eluno_pos customer lookup --phone +919000000001

# 3. Non-Interactive 15-Step Optical Checkout
python -m eluno_pos checkout --store HSR --staff S108 --customer +919000000001 --frame 91000001 --lens 91000041 --yes

# 4. Inspect Generated Tax Invoice
python -m eluno_pos invoice show --invoice ELU/HSR/FY27/00001

# 5. Full Store Return & Sequential Credit Note Issuance
python -m eluno_pos refund --invoice ELU/HSR/FY27/00001 --staff S108 --reason CUSTOMER_RETURN --yes

# 6. Generate Store Day-End Sales & Tax Reconciliation Report
python -m eluno_pos report --store HSR --date 2026-11-05

# 7. Check Transaction Recovery Hub
python -m eluno_pos reconcile --store HSR
```

---

## 3. Scope Status Summary

| Tier | Status | Details |
|---|---|---|
| **Must (1–7)** | **100% IMPLEMENTED** | Customer/Rx lookup, optical lens matrix validation, data-driven offer engine, date-effective GST by HSN, durable terminal charge & Shopify order sync with sequential store invoice numbers, full refunds with frame restock / lens scrap, day-end reporting from Shopify orders. |
| **Should** | **PARTIAL / DEFERRED** | Mandatory staff audit reason codes implemented for overrides and refunds. Cash till sessions, cash drawer tracking, and till-side sales channel selection deferred to v1.1. Partial refunds (frame-only/lens-only) and HTML/PDF printing deferred. |
| **Stretch** | **DEFERRED** | NLP offer authoring, vision prescription entry assist, and billing anomaly detection deferred. |

Refer to [`README.md`](README.md#2-scope-audit-must-should-and-stretch-requirements) for the complete, line-by-line audit matrix.
