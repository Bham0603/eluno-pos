# Response to Stakeholder Thread: Eluno POS v1

**To:** Rohan (Founder), Meena (Retail Head), Dr. Iyer (Optometry Manager), Vikram (Finance), Farhan (Ops/Fulfilment)  
**From:** Priya (Product) & POS Engineering Team | **Date:** 4 Nov 2026 | **Subject:** Scoped Plan & Clarifications for POS v1 (Go-Live: 15 Nov)

---

Team, to replace Shopify POS across all 7 stores by 15 November, our priority is **uncompromising correctness**: every rupee, tax paisa, and inventory movement must reconcile with Shopify and tax authorities. Speed will be achieved through clean architecture without bypassing audit controls.

---

## 1. Key Questions for Stakeholders

| Stakeholder | Question | Why It Matters for v1 |
|---|---|---|
| **Vikram (Finance)** | Confirm exact FY naming format (`ELU/{STORE}/FY26/{NO}`), GST e-invoicing (IRN) / B2B/B2C rules, and whether cancelled invoices need tax-portal filing or internal ledger retention. | Dictates local sequence schemas and statutory audit compliance. |
| **Retail & Finance** | When discount rules conflict (e.g. higher-priority non-stackable offer vs. stackable set yielding greater savings), does the till maximize customer benefit or follow strict priority? | Dictates the optimization logic of the automated offer engine. |
| **Vikram & Meena** | Meena requested up to 10% manager discount; Vikram mandated reason codes and approver identity. Can Retail and Finance confirm: (a) 10% cap approval, (b) reason codes, and (c) authentication via `staff_id` with `can_override_discount: true` in `staff.json`? | Balances floor agility with audit compliance without shared PINs. |
| **Dr. Iyer (Optometry)** | If a customer's prescription is missing or expired (`valid_until` passed), should the till hard-block lens configuration and route to SMT queue, or permit frame-only purchase? | Establishes clinical guardrails and prevents lab remakes. |
| **Farhan (Ops)** | What is the store SOP if network reconnection exceeds 15 minutes post-terminal payment? | Guides offline reconciliation and customer receipt handling. |

---

## 2. Scope Matrix: Building vs. Deferring

### In Scope for v1 (Must Requirements)
1. **Customer & Rx Lookup:** Phone search via Shopify; latest valid Rx from SMT (`/smt/prescriptions`). Cohorts (`priority`, campaigns) from Shopify customer tags.
2. **Cart & Lens Matrix Validation:** Real-time validation of sphere, cylinder, and ADD against `rx_limits`, blocking invalid picks with clear staff prompts.
3. **Automated Offer Engine:** Date-effective rules from `offers.json` (stackability, priority, tags, pair bundles).
4. **Date-Effective GST Engine (by HSN):** Backward tax calculation from MRP to the paisa across 5 categories in `tax_rates.json`:
   * **HSN 90031900** (Frames): 18% (1800 bps) | **HSN 90015000** (Lenses): 5% (500 bps from 2025-09-22; 12% prior)
   * **HSN 90049000** (Sunglasses): 18% (1800 bps) | **HSN 90041000** (Corrective spectacles unit): 5% (500 bps)
   * **HSN 998599** (Services: eye test / fitting): 18% (1800 bps)
5. **Durable Order Creation:** Idempotent terminal charge followed by Shopify order creation carrying line attributes (`pair_ref`, `rx_id`, `for_customer_id`, HSN) and local sequential invoice numbers in `note_attributes`.
6. **Full Refund & Targeted Restock:** Line-level tax reversal, credit notes, frame returned to billing store stock, and custom lens scrapped without restock.
7. **Day-End Store Reconciliation Report:** Daily report for store and date from Shopify orders (bill count, gross, taxable, CGST/SGST, discounts, refunds, payment splits).

### Explicitly Deferred to v1.1 (Should Requirements & Non-POS Tasks)
* **Sales Channel Capture & Reporting (Should):** Tagging orders (`walk_in`, `whatsapp_booked`, `online_pickup`) and store channel reports.
* **Cash Till Sessions & Drawer Tracking (Should):** Opening float, petty cash paid-outs (tea, courier), paid-ins, and drawer variance.
* **Full Remake Workflow (Should/Stretch):** Commercial liability allocation (lab vs. store vs. customer). Lens replacements bill referencing `parent_invoice_no`.
* **Automated 10 AM Daily Email Digest:** Handled by a backend cron querying Shopify Admin API, outside the till client.
* **Queue Tool & Facilities Maintenance:** SMT questionnaire updates and Indiranagar footfall camera hardware repairs.

---

## 3. Contradictions, Risks & Proposed Solutions

### A. The <10-Second Target & Measurement
We cannot promise a sub-10s checkout without measurement. Estimated latency breakdown:  
`Total Time ≈ Staff Input + Local Calc (<10ms) + Terminal Roundtrip (~2–5s unverified) + Shopify API (~1–2s unverified)`  
Telemetry will track p50/p95 times. Speed will come from in-memory processing and catalog caching, never by skipping audit controls.

### B. Manager Discounts & Non-Repudiable Attribution
The 10% cap requires formal Finance/Retail agreement. Overrides require authentication by a manager with `can_override_discount: true` in `staff.json`, recording approver `staff_id`, discount amount, and reason code (`WALKOUT_RESCUE`, `DAMAGE_ALLOWANCE`, `PRICE_MATCH`). No shared PINs.

### C. Family Billing with Single Customer Constraint
One primary Shopify customer per order. Line items carry `pair_ref`, `rx_id`, and `for_customer_id` properties. Cross-account loyalty is deferred to v1.1.

### D. Invoice Lifecycle & Immutability
Sequential invoice numbers are generated atomically from local SQLite with strict states:
* `POSTED`: Captured on terminal and confirmed in Shopify.
* `PENDING_RECONCILIATION`: Terminal funds captured; awaiting Shopify sync. **Never treated as cancelled; number is never reused.**
* `CANCELLED`: Aborted pre-payment; retained for gapless tax audit sequences.

### E. Network Resilience & Safe Distributed Reconciliation
Shopify's REST API `/orders.json` lacks idempotency keys; blind retries risk duplicate orders and double-decremented stock. Furthermore, **an immediate lookup returning no order does not prove creation failed**, as downstream indexing delays occur.  
**Workflow:** (1) Persist local intent with `invoice_no`. (2) Idempotent terminal charge (`order_ref = invoice_no`). (3) Store `charge_id` locally; mark invoice `PENDING_RECONCILIATION`. (4) Submit order to Shopify. If response is ambiguous (timeout, network drop), keep invoice `PENDING_RECONCILIATION` and reconcile by listing recent orders at that `location_id` and inspecting `note_attributes` locally (as Shopify lacks direct `note_attributes` filtering). Never blindly replay; retry only if failure is conclusively established. (5) Transition to `POSTED` upon confirmed sync.

### F. Prescription Source of Truth & Axis Validation
Shopify customer `tags` supply priority/campaign markers; SMT supplies prescriptions. SMT entry forms must enforce the 5° grid; POS will display an informational advisory for off-grid external prescriptions (e.g. 172°) without blocking checkout.

---

## 4. Production Dependencies & Launch Checklist

1. **Shopify Admin API Credentials:** Custom App token with scopes: `read_products`, `read_customers`, `read_inventory`, `write_inventory`, `read_orders`, and `write_orders`, plus verified rate limits.
2. **Payment Terminal Integration:** SDK/API specs, idempotent retry docs, and payment terminal refund API and reconciliation documentation.
3. **Staff Identity & SMT Services:** Production SMT endpoints for `/smt/prescriptions` and `/smt/staff`, including staff authentication and permission verification.
4. **Finance & Tax Compliance:** Approved FY naming convention, credit note prefix, sign-off on date-effective HSN rates, and confirmation of GST e-invoicing applicability, IRN generation, B2B/B2C reporting, and credit-note filing rules.
5. **Hardware Specifications:** Driver specifications for ESC/POS thermal receipt printers and USB/Bluetooth barcode scanners.

---

*The engineering team is ready to begin Phase 1 upon approval.*
