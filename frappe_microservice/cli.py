"""
Operational CLI for frappe-microservice (console script ``frappe-ms``).

This module also fixes a previously broken entry point: ``setup.py`` declared
``frappe-ms = frappe_microservice.cli:main`` but this module did not exist.

Subcommands
-----------
- ``frappe-ms migrate`` — run the one-shot DocType/fixture sync
  (``presync_service_doctypes``) once and exit. Honors ``DOCTYPE_SYNC_MODE``
  (default ``migrate``). Intended for use as a Kubernetes Job / init container.
  Exits non-zero on DB connection failure.
- ``frappe-ms worker`` — guidance/entry for running a dedicated RQ worker
  (``WORKER_MODE=1`` + ``MicroserviceApp.run_worker()``).
"""

import argparse
import os
import sys


def _cmd_migrate(_args) -> int:
    """Run presync_service_doctypes once. Returns process exit code."""
    # Ensure container-safe logging before any frappe.logger() call.
    os.environ.setdefault("FRAPPE_STREAM_LOGGING", "1")

    from frappe_microservice.presync import presync_service_doctypes

    site = os.getenv("FRAPPE_SITE", "site1.local")
    sites_path = os.getenv("FRAPPE_SITES_PATH", "/app/sites")

    # Verify a DB connection is reachable so the Job fails loudly if it is not
    # (presync itself skips silently when it cannot connect, which is the right
    # behavior at container boot but wrong for an explicit migrate command).
    try:
        import frappe

        frappe.init(site=site, sites_path=sites_path)
        frappe.connect()
        frappe.db.sql("SELECT 1")
        frappe.destroy()
    except Exception as e:  # pragma: no cover - exercised via integration
        print(
            f"frappe-ms migrate: cannot connect to site {site!r}: {e}", file=sys.stderr
        )
        return 1

    presync_service_doctypes(site=site, sites_path=sites_path)
    return 0


def _cmd_worker(_args) -> int:
    """Minimal worker entry point / guidance."""
    service_name = os.getenv("SERVICE_NAME", "")
    if not service_name:
        print(
            "frappe-ms worker: set SERVICE_NAME and run your app with "
            "WORKER_MODE=1, then call MicroserviceApp.run_worker(). "
            "The embedded thread worker (ENABLE_RQ=1) is dev-only and cannot "
            "enforce job timeouts.",
            file=sys.stderr,
        )
        return 1
    print(
        "frappe-ms worker: create your MicroserviceApp with ENABLE_RQ=1 and "
        "WORKER_MODE=1, then call app.run_worker(). This subcommand is a "
        "placeholder — wire your service module in your container command.",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frappe-ms",
        description="Operational commands for frappe-microservice.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_migrate = sub.add_parser(
        "migrate", help="Sync service DocTypes/fixtures to the DB once."
    )
    p_migrate.set_defaults(func=_cmd_migrate)

    p_worker = sub.add_parser(
        "worker", help="Run / guidance for a dedicated RQ worker."
    )
    p_worker.set_defaults(func=_cmd_worker)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
