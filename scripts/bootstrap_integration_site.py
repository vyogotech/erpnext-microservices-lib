#!/usr/bin/env python3
"""
Bootstrap an empty Frappe+ERPNext site for CI / docker-compose integration tests.

Older ERPNext exposed ``erpnext.setup.utils.before_tests``; current versions removed it.
This script:

1. Skips if ``tabCompany`` already has rows.
2. Otherwise tries legacy ``before_tests`` when present.
3. Otherwise runs ``erpnext.setup.setup_wizard.setup_complete`` with a minimal AU/AUD profile.

Run inside the bench container (same Python as bench), e.g.::

    FRAPPE_SITE=dev.localhost FRAPPE_SITES_PATH=/home/frappe/frappe-bench/sites \\
      /home/frappe/frappe-bench/env/bin/python scripts/bootstrap_integration_site.py

Or with repo mounted at ``/mnt/lib``::

    /home/frappe/frappe-bench/env/bin/python /mnt/lib/scripts/bootstrap_integration_site.py

Expense-service CI mounts the same logic at ``/mnt/expense/scripts/bootstrap_integration_site.py``
(keep the two files in sync).
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    site = os.environ.get("FRAPPE_SITE", "dev.localhost")
    sites_path = os.environ.get(
        "FRAPPE_SITES_PATH", "/home/frappe/frappe-bench/sites"
    )

    import frappe

    frappe.init(site=site, sites_path=sites_path)
    frappe.connect()
    frappe.set_user("Administrator")

    try:
        if frappe.db.count("Company"):
            print("bootstrap_integration_site: Company rows exist — nothing to do")
            return 0

        # ── Legacy ERPNext test bootstrap (still present on some branches) ──
        try:
            from erpnext.setup.utils import before_tests as legacy_before_tests
        except Exception:
            legacy_before_tests = None
        if callable(legacy_before_tests):
            legacy_before_tests()
            frappe.db.commit()
            print("bootstrap_integration_site: ran erpnext.setup.utils.before_tests")
            return 0

        if "erpnext" not in frappe.get_installed_apps():
            print(
                "bootstrap_integration_site: no Company and erpnext not installed — cannot bootstrap",
                file=sys.stderr,
            )
            return 1

        # ── Modern: full setup wizard pipeline (creates CoA, company, defaults) ──
        from erpnext.setup.setup_wizard.setup_wizard import setup_complete

        args = frappe._dict(
            country="Australia",
            timezone="Australia/Sydney",
            company_name="CI Test Company",
            company_abbr="CTC",
            currency="AUD",
            chart_of_accounts="Australia - Chart of Accounts with Account Numbers",
            fy_start_date="2025-07-01",
            fy_end_date="2026-06-30",
            domain="Services",
        )
        setup_complete(args)
        frappe.db.commit()
        print(
            "bootstrap_integration_site: ran erpnext.setup.setup_wizard.setup_complete"
        )
        return 0
    finally:
        frappe.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
