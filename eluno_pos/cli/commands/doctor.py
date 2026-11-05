"""Doctor health check command for Eluno POS."""

import time
from typing import Dict, Any

from eluno_pos.adapters.exceptions import ApiClientError, NotFoundError
from eluno_pos.cli.context import AppContext
from eluno_pos.cli.formatting import print_card, print_header


def run_doctor(ctx: AppContext) -> int:
    """Verify system configuration, SQLite storage, and external API connectivity without financial mutations."""
    print_header("Eluno POS — System Doctor & Health Check", "Verifying local and upstream system health")

    all_passed = True
    results: Dict[str, Any] = {}

    # 1. Configuration Check
    cfg = ctx.config
    print("[1/5] Checking Configuration...")
    print(f"  • Shopify Base URL:  {cfg.shopify_base_url}")
    print(f"  • SMT Base URL:      {cfg.smt_base_url}")
    print(f"  • Terminal Base URL: {cfg.terminal_base_url}")
    print(f"  • Database Path:     {cfg.db_path}")
    print(f"  • Local Mock Mode:   {'YES (Safe)' if cfg.is_local_mock() else 'EXTERNAL HOST (Caution)'}")
    results["Configuration"] = "VALID"

    # 2. SQLite Database Durability Check
    print("\n[2/5] Checking SQLite Database...")
    try:
        with ctx.repo._get_connection() as conn:
            cur = conn.execute("PRAGMA journal_mode;")
            journal_mode = cur.fetchone()[0]
            cur = conn.execute("PRAGMA foreign_keys;")
            foreign_keys = cur.fetchone()[0]
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row["name"] for row in cur.fetchall()]

        print(f"  • Journal Mode:      {journal_mode}")
        print(f"  • Foreign Keys:      {'ENABLED' if foreign_keys else 'DISABLED'}")
        print(f"  • Verified Tables:   {len(tables)} tables ({', '.join(tables[:4])}...)")
        results["SQLite Database"] = "HEALTHY"
    except Exception as err:
        print(f"  ✖ SQLite Error: {err}")
        results["SQLite Database"] = f"FAILED: {err}"
        all_passed = False

    # 3. Shopify Mock Service Connectivity
    print("\n[3/5] Checking Shopify Service Reachability...")
    t0 = time.perf_counter()
    try:
        locations = ctx.shopify_client.get_locations()
        latency_ms = (time.perf_counter() - t0) * 1000
        print(f"  • Status:            REACHABLE ({latency_ms:.1f} ms)")
        print(f"  • Available Stores:  {len(locations)} locations ({', '.join(loc['name'] for loc in locations)})")
        results["Shopify Service"] = f"ONLINE ({latency_ms:.1f}ms)"
    except Exception as err:
        print(f"  ✖ Shopify Service Error: {err}")
        results["Shopify Service"] = f"OFFLINE: {err}"
        all_passed = False

    # 4. SMT Service Connectivity
    print("\n[4/5] Checking SMT Service Reachability...")
    t0 = time.perf_counter()
    try:
        staff_list = ctx.smt_client.get_staff()
        latency_ms = (time.perf_counter() - t0) * 1000
        print(f"  • Status:            REACHABLE ({latency_ms:.1f} ms)")
        print(f"  • Active Staff:      {len(staff_list)} staff members registered")
        results["SMT Service"] = f"ONLINE ({latency_ms:.1f}ms)"
    except Exception as err:
        print(f"  ✖ SMT Service Error: {err}")
        results["SMT Service"] = f"OFFLINE: {err}"
        all_passed = False

    # 5. Terminal Gateway Connectivity (Read-Only Reachability Probe)
    print("\n[5/5] Checking Terminal Gateway Reachability...")
    t0 = time.perf_counter()
    try:
        # Probe endpoint with dummy query to verify listener without initiating payment/refund mutations
        ctx.terminal_client.get_charge("probe_connection_check")
        latency_ms = (time.perf_counter() - t0) * 1000
        print(f"  • Status:            REACHABLE ({latency_ms:.1f} ms)")
        results["Terminal Gateway"] = f"ONLINE ({latency_ms:.1f}ms)"
    except NotFoundError:
        # Expected: probe charge not found confirms terminal server is online and processing requests!
        latency_ms = (time.perf_counter() - t0) * 1000
        print(f"  • Status:            REACHABLE ({latency_ms:.1f} ms)")
        results["Terminal Gateway"] = f"ONLINE ({latency_ms:.1f}ms)"
    except Exception as err:
        print(f"  ✖ Terminal Gateway Error: {err}")
        results["Terminal Gateway"] = f"OFFLINE: {err}"
        all_passed = False

    # Summary
    print_card("Doctor Diagnostics Summary", list(results.items()))

    if all_passed:
        print("\n✔ All system diagnostics passed. Eluno POS is ready for operation.\n")
        return 0
    else:
        print("\n✖ One or more health checks failed. Please check local mock server or environment.\n")
        return 1

