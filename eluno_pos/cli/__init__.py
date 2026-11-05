"""Eluno POS Command-Line Interface package."""

from pathlib import Path
import sys
from typing import List, Optional

from eluno_pos.adapters.exceptions import ElunoPosError
from eluno_pos.cli.commands.checkout import run_checkout
from eluno_pos.cli.commands.customer import run_customer_lookup
from eluno_pos.cli.commands.doctor import run_doctor
from eluno_pos.cli.commands.invoice import run_invoice_show
from eluno_pos.cli.commands.reconcile import run_reconcile
from eluno_pos.cli.commands.refund import run_refund
from eluno_pos.cli.commands.report import run_report
from eluno_pos.cli.context import AppContext
from eluno_pos.cli.parser import build_parser
from eluno_pos.config import AppConfig


def main(args: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint for Eluno POS."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = build_parser()
    parsed_args = parser.parse_args(args)

    if not parsed_args.command:
        parser.print_help()
        return 0

    # Load configuration
    env_file_path = Path(parsed_args.env_file) if parsed_args.env_file else None
    config = AppConfig.from_env(env_file=env_file_path)
    if parsed_args.db_path:
        # Override database path if provided via flag
        config = AppConfig(
            shopify_base_url=config.shopify_base_url,
            smt_base_url=config.smt_base_url,
            terminal_base_url=config.terminal_base_url,
            db_path=parsed_args.db_path,
            http_timeout_seconds=config.http_timeout_seconds,
            max_retries_429=config.max_retries_429,
            max_retries_terminal_503=config.max_retries_terminal_503,
            backoff_factor=config.backoff_factor,
        )

    debug_mode = getattr(parsed_args, "debug", False)

    try:
        ctx = AppContext.create(config)

        # Route command
        if parsed_args.command == "doctor":
            return run_doctor(ctx)

        elif parsed_args.command == "customer":
            if parsed_args.customer_cmd == "lookup" or not parsed_args.customer_cmd:
                return run_customer_lookup(ctx, phone=getattr(parsed_args, "phone", None))
            else:
                parser.parse_args(["customer", "--help"])
                return 1

        elif parsed_args.command == "checkout":
            return run_checkout(
                ctx=ctx,
                store_code=parsed_args.store,
                staff_id=parsed_args.staff,
                customer_phone=parsed_args.customer,
                frame_variant_id=parsed_args.frame,
                lens_variant_id=parsed_args.lens,
                rx_id=parsed_args.rx,
                for_member=parsed_args.for_member,
                for_customer_id=getattr(parsed_args, "for_customer_id", None),
                manager_override_bps=parsed_args.manager_override_bps,
                override_reason=parsed_args.override_reason,
                override_manager_id=parsed_args.override_manager_id,
                payment_method=parsed_args.method,
                auto_confirm=parsed_args.auto_confirm,
            )

        elif parsed_args.command == "refund":
            return run_refund(
                ctx=ctx,
                invoice_no=parsed_args.invoice,
                location_id=parsed_args.location,
                reason_code=parsed_args.reason,
                staff_id=parsed_args.staff,
                auto_confirm=parsed_args.auto_confirm,
            )

        elif parsed_args.command == "report":
            return run_report(
                ctx=ctx,
                store_code=parsed_args.store,
                business_date=parsed_args.date,
                location_id=parsed_args.location,
                reconcile_shopify=parsed_args.reconcile_shopify,
            )

        elif parsed_args.command == "reconcile":
            return run_reconcile(
                ctx=ctx,
                store_code=parsed_args.store,
                execute_recovery=parsed_args.execute,
                auto_confirm=parsed_args.auto_confirm,
            )

        elif parsed_args.command == "invoice":
            inv_no = parsed_args.invoice_no or parsed_args.pos_invoice_no
            return run_invoice_show(ctx, invoice_no=inv_no)

        else:
            parser.print_help()
            return 1

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        return 130
    except ElunoPosError as err:
        if debug_mode:
            raise
        print(f"\n✖ POS Error: {err.message}\n")
        return 1
    except Exception as err:
        if debug_mode:
            raise
        print(f"\n✖ System Error: {err}\n(Run with --debug for stack trace)\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
