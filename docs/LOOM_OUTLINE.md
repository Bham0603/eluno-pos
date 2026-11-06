# ELUNO POS — LOOM WALKTHROUGH RECORDING SCRIPT & OUTLINE

**Target Duration:** Maximum 8:00 minutes  
**Presenter:** Engineering Lead / Author  
**Environment:** Terminal / CLI with deterministic mock server running on port 8787  
**Associated Artifact:** `docs/DEMO_WALKTHROUGH.log`

---

## Timecode & Segment Outline

```
0:00 – 0:40 | Segment 1: Architecture & Optical Domain Overview
0:40 – 2:00 | Segment 2: Customer Profile, Clinical Lookup & Data Minimization
2:00 – 3:30 | Segment 3: Offer Engine, Manager Overrides & Exact-Paisa GST
3:30 – 5:00 | Segment 4: Complete Checkout Lifecycle & Cloud Synchronization
5:00 – 6:00 | Segment 5: Full Store Refund, Credit Note & Targeted Restocking
6:00 – 7:00 | Segment 6: Multi-System Day-End Reporting & Reconciliation Hub
7:00 – 7:50 | Segment 7: Key Engineering Invariants, Limitations & AI Disclosure
7:50 – 8:00 | Segment 8: Conclusion & Next Steps
```

---

### Segment 1: Architecture & Optical Domain Overview (0:00 – 0:40)
* **Visual:** Code repository directory structure (`eluno_pos/domain`, `eluno_pos/infrastructure`, `eluno_pos/cli`).
* **Talking Points:**
  - Introduce Eluno POS as a specialized optical retail point-of-sale system built to replace generic retail tills.
  - Highlight the clean, layered architecture: pure domain logic (`cart`, `gst`, `offers`, `lens_validation`, `invoice`) completely decoupled from external I/O and cloud infrastructure.
  - Explain the multi-system reality: local SQLite write-ahead log (WAL) for store-level resilience, SMT Central for clinical prescriptions and staff directory, payment terminal gateway for card/UPI, and Shopify Admin API as the centralized inventory and order system of record.
  - Quick sanity check: run `python -m eluno_pos doctor` showing 5/5 diagnostics green.

### Segment 2: Customer Profile, Clinical Lookup & Data Minimization (0:40 – 2:00)
* **Visual:** Run `python -m eluno_pos customer lookup --phone +919000000001`.
* **Talking Points:**
  - Real-time customer lookup querying Shopify for demographic details and customer tags (e.g. `family`, `priority`).
  - Concurrent query to SMT Central API for verified clinical prescriptions.
  - **Clinical Data Minimization Protocol:** Point out that raw sphere, cylinder, and axis values are masked (`[PROTECTED]`) in the store-floor CLI to protect patient health information under DPDP principles.
  - Explain how the domain engine deterministically validates prescription powers against the lens matrix (`LensValidator`) during pairing: checking sphere/cylinder boundaries, progressive corridor constraints, and maximum cylinder limits before allowing cart assembly.

### Segment 3: Offer Engine, Manager Overrides & Exact-Paisa GST (2:00 – 3:30)
* **Visual:** Show `offers.json` snippet and demonstrate discount calculation.
* **Talking Points:**
  - **Offer Engine:** Date-effective, coupon-aware, and pair-bundle rule resolution. Explaining priority-based vs stackable offers (e.g. `FESTIVE10` yielding 10% off frame).
  - **Audited Manager Overrides:** Highlight the security enhancement: manager overrides strictly require an authorized manager (`can_override_discount: true` in `staff.json`) and an explicit, valid audit reason code (`CUSTOMER_RETENTION`, `PRICE_MATCH`, `WALKOUT_RESCUE`, `DAMAGE_ALLOWANCE`). Rejection occurs before any financial mutation.
  - **Exact-Paisa Backward GST Engine:** Walk through reverse tax calculation from inclusive MRP:
    - Frames (HSN 90031900) at 18% GST (CGST 9% + SGST 9%).
    - Lenses (HSN 90015000) at 5% GST (CGST 2.5% + SGST 2.5%).
    - Show integer paise representation (`amount_paise`) to eliminate floating-point penny drift.

### Segment 4: Complete Checkout Lifecycle & Cloud Synchronization (3:30 – 5:00)
* **Visual:** Run `python -m eluno_pos checkout --store HSR --staff S108 --customer +919000000001 --frame 91000001 --lens 91000041 --yes`.
* **Talking Points:**
  - Step through the 15-stage atomic execution pipeline:
    1. Local sequential invoice number allocated (`ELU/HSR/FY27/00001`) with initial state `PENDING_RECONCILIATION`.
    2. Terminal card/UPI charge initiated idempotently using `order_ref = ELU/HSR/FY27/00001`. Payment captured (`chg_99000001`).
    3. Confirmed order submitted to Shopify with comprehensive `note_attributes` (Invoice number, Store, Staff, Lab Rx, Wearer ID).
    4. Local invoice transitioned to `POSTED`.
  - Run `python -m eluno_pos invoice show --invoice ELU/HSR/FY27/00001` showing line items, HSN tax breakdown, and external references.

### Segment 5: Full Store Refund, Credit Note & Targeted Restocking (5:00 – 6:00)
* **Visual:** Run `python -m eluno_pos refund --invoice ELU/HSR/FY27/00001 --staff S108 --reason CUSTOMER_RETURN --yes`.
* **Talking Points:**
  - Walk through the return lifecycle:
    1. Full payment reversal through payment terminal gateway (`trf_99000006`).
    2. Sequential credit note issuance (`ELU/HSR/FY27/CN/00001`).
    3. **Targeted Asymmetric Restocking:** Physical frame stock is restocked (+1 unit at billing store HSR location `70000002`), while custom ophthalmic lenses are scrapped (`no_restock: true`) as lenses cut to custom pupillary distance cannot be resold.
    4. Shopify cloud refund created.

### Segment 6: Multi-System Day-End Reporting & Reconciliation Hub (6:00 – 7:00)
* **Visual:** Run `python -m eluno_pos report --store HSR --date 2026-11-07` followed by `python -m eluno_pos reconcile --store HSR`.
* **Talking Points:**
  - **Day-End Store Report:** Fetches all Shopify orders for the store location across the exact IST business day boundary (Asia/Kolkata).
  - Demonstrates discovery of both sales and historical/same-day refunds.
  - Displays gross revenue, discounts, net revenue, HSN tax breakdowns, and payment method totals.
  - Net figures reflect the refund (Net Store Revenue = ₹0.00, Net GST = ₹0.00).
  - Run `reconcile` to demonstrate zero pending transactions and complete multi-system alignment.

### Segment 7: Key Engineering Invariants, Limitations & AI Disclosure (7:00 – 7:50)
* **Visual:** Display `STAKEHOLDER_REPLY.md` and test suite summary (213 passing tests).
* **Talking Points:**
  - **Core Engineering Invariants:**
    1. Integer arithmetic in paise prevents rounding discrepancies.
    2. Resilient outbox/reconciliation pattern instead of fragile distributed two-phase commits.
    3. Fully deterministic testing suite (213 automated tests across unit, domain, infrastructure, and end-to-end CLI).
  - **Known Production Limitations & Deferred Scope:**
    - Upstream integrations currently run against local mock APIs; production requires Shopify Private App tokens, physical terminal SDKs (Pine Labs/Innoviti), and SMT SSO.
    - Statutory GST e-invoicing (IRN) and physical ESC/POS thermal printer hardware integration.
    - Cash till drawer management and partial line-item refunds deferred to v1.1.
  - **Transparent AI Usage:** AI assistance was leveraged for test generation, mock server edge cases, and documentation alignment while all domain invariants and architectural guardrails were rigorously human-verified.

### Segment 8: Conclusion & Wrap-Up (7:50 – 8:00)
* **Visual:** Terminal showing clean repository status on branch `main`.
* **Talking Points:**
  - Wrap up: Eluno POS delivers audit-ready financial correctness, robust clinical safety, and resilient distributed reconciliation.
  - Ready for stakeholder evaluation.

