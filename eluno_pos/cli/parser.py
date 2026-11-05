"""Command-line argument parser configuration for Eluno POS CLI."""

import argparse
from typing import List, Optional


def build_parser() -> argparse.ArgumentParser:
    """Construct the standard-library argparse command hierarchy for Eluno POS."""
    parser = argparse.ArgumentParser(
        prog="python -m eluno_pos",
        description="Eluno POS — Omnichannel Eyewear Point of Sale System",
        epilog="Run 'python -m eluno_pos <command> --help' for details on a specific command.",
    )

    # Global options
    parser.add_argument(
        "--db",
        dest="db_path",
        metavar="PATH",
        help="Path to SQLite database file (overrides DATABASE_PATH environment variable).",
    )
    parser.add_argument(
        "--env-file",
        dest="env_file",
        metavar="PATH",
        help="Path to custom .env file to load configuration from.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show detailed developer diagnostics and raw stack traces on error.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available POS operations")

    # 1. DOCTOR: Health check
    doctor_p = subparsers.add_parser("doctor", help="Verify configuration, database durability, and API health.")

    # 2. CUSTOMER: Customer & prescription lookup
    customer_p = subparsers.add_parser("customer", help="Customer and clinical optical prescription lookup.")
    cust_sub = customer_p.add_subparsers(dest="customer_cmd", help="Customer subcommands")
    lookup_p = cust_sub.add_parser("lookup", help="Search customer details and prescriptions by phone number.")
    lookup_p.add_argument("-p", "--phone", help="Customer phone number (e.g. +919000000001).")

    # 3. CHECKOUT: Guided optical checkout
    checkout_p = subparsers.add_parser("checkout", help="Initiate guided 15-step retail optical checkout.")
    checkout_p.add_argument("-s", "--store", help="Store code (e.g. HSR, MM, BR).")
    checkout_p.add_argument("--staff", help="Operating staff ID (e.g. S108).")
    checkout_p.add_argument("-c", "--customer", help="Customer phone number.")
    checkout_p.add_argument("-f", "--frame", type=int, help="Frame product variant ID.")
    checkout_p.add_argument("-l", "--lens", type=int, help="Lens product variant ID.")
    checkout_p.add_argument("-r", "--rx", help="Clinical prescription ID to link (e.g. RX5002).")
    checkout_p.add_argument("--for-member", help="Individual recipient name for family billing.")
    checkout_p.add_argument("--for-customer-id", "--wearer-id", type=int, dest="for_customer_id", help="Persisted customer ID of wearer/recipient.")
    checkout_p.add_argument("--manager-override", type=int, dest="manager_override_bps", help="Manager discount override in basis points (e.g. 500 for 5%%).")
    checkout_p.add_argument("--override-reason", help="Mandatory audit reason code for manager discount.")
    checkout_p.add_argument("--override-manager-id", help="Staff ID of authorizing store manager.")
    checkout_p.add_argument("-m", "--method", default="upi", choices=["upi", "card", "cash"], help="Payment method (default: upi).")
    checkout_p.add_argument("-y", "--yes", action="store_true", dest="auto_confirm", help="Run non-interactively with explicit required arguments, bypassing interactive confirmation prompts.")

    # 4. REFUND: Full return & credit note issuance
    refund_p = subparsers.add_parser("refund", help="Process full return, terminal reversal, and credit note issuance.")
    refund_p.add_argument("-i", "--invoice", help="Invoice number to refund (e.g. ELU/HSR/FY27/00001).")
    refund_p.add_argument("--location", type=int, help="Store location ID for frame restocking.")
    refund_p.add_argument("--reason", help="Return reason code (e.g. CUSTOMER_RETURN, DEFECTIVE_FRAME).")
    refund_p.add_argument("--staff", help="Authorizing staff ID (must belong to invoice store).")
    refund_p.add_argument("-y", "--yes", action="store_true", dest="auto_confirm", help="Run non-interactively with explicit arguments, bypassing confirmation prompts.")

    # 5. REPORT: Day-end store report
    report_p = subparsers.add_parser("report", help="Generate consolidated day-end sales, refund, and GST report.")
    report_p.add_argument("-s", "--store", help="Store code (e.g. HSR).")
    report_p.add_argument("-d", "--date", help="Business date in YYYY-MM-DD format (Asia/Kolkata).")
    report_p.add_argument("--location", type=int, help="Store location ID.")
    report_p.add_argument("--no-reconcile", action="store_false", dest="reconcile_shopify", help="Skip Shopify cloud order cross-check.")

    # 6. RECONCILE: Transaction and recovery hub
    reconcile_p = subparsers.add_parser("reconcile", help="Inspect and recover in-flight, ambiguous, or pending operations.")
    reconcile_p.add_argument("-s", "--store", help="Filter pending transactions by store code.")
    reconcile_p.add_argument("-x", "--execute", action="store_true", help="Execute safe automated reconciliation routines.")
    reconcile_p.add_argument("-y", "--yes", action="store_true", dest="auto_confirm", help="Run non-interactively, bypassing interactive confirmation prompts.")

    # 7. INVOICE: Document inspection
    invoice_p = subparsers.add_parser("invoice", help="Store tax invoice and audit inspection.")
    inv_sub = invoice_p.add_subparsers(dest="invoice_cmd", help="Invoice subcommands")
    inv_show_p = inv_sub.add_parser("show", help="Display full tax document, payment status, and itemized taxes.")
    inv_show_p.add_argument("-i", "--invoice", dest="invoice_no", help="Invoice number to inspect (e.g. ELU/HSR/FY27/00001).")
    inv_show_p.add_argument("pos_invoice_no", nargs="?", help="Positional invoice number.")

    return parser

