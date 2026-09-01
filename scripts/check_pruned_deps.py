#!/usr/bin/env python
"""Verify the pruned service image still has everything Frappe actually loads.

The Containerfile installs frappe and erpnext with --no-deps and then installs
only the dependencies they declare minus scripts/excluded_packages.txt (Desk-only:
PDF, Excel, 2FA, search, console, Google integrations). That exclusion list is
measured, not guessed -- and a Frappe upgrade can add a new top-level import that
invalidates it. Run this inside the built image:

    podman run --rm --entrypoint /opt/venv/bin/python \\
        <image> /app/scripts/check_pruned_deps.py

Exits non-zero if booting the service fails, or if any Frappe/ERPNext module
that used to import cleanly now cannot. Modules whose only purpose is a pruned
feature are expected to fail and are listed as ALLOWED.
"""

import importlib
import os
import pkgutil
import sys
import warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("FRAPPE_STREAM_LOGGING", "1")

# Frappe/ERPNext modules that exist only to serve a pruned feature. An
# ImportError here is the prune working as intended, not a regression.
ALLOWED_PREFIXES = (
    "frappe.utils.weasyprint",
    "frappe.utils.password_strength",
    "frappe.utils.boilerplate",
    "frappe.utils.pdf_generator.",
    "frappe.utils.telemetry",
    "frappe.integrations.doctype.google_",
    "frappe.integrations.google_oauth",
    "frappe.integrations.doctype.ldap_settings",
    "frappe.integrations.doctype.geolocation_settings",
    "frappe.geo.doctype.country",
    "frappe.commands",
    "frappe.gettext",
    "frappe.contacts.doctype.contact",
    # Migrate-time only. The service image has no bench (frappe-bench is not
    # installed) and the library never calls frappe.migrate or runs patches --
    # schema sync goes through presync_service_doctypes. frappe.installer, the
    # one installer entry point the library does use (site_config.py), imports
    # fine without pycountry; only frappe.utils.install does not.
    "frappe.patches.",
    "frappe.utils.install",
    "frappe.website.doctype.website_settings.google_indexing",
)


def boot():
    """Import what a real service imports. Must not raise."""
    import frappe

    from frappe_microservice.site_config import create_site_config

    create_site_config()
    frappe.init(
        site=os.getenv("FRAPPE_SITE", "dev.localhost"),
        sites_path=os.getenv("FRAPPE_SITES_PATH", "/app/sites"),
    )
    from frappe.model.base_document import BaseDocument  # noqa: F401
    from frappe.model.document import Document  # noqa: F401

    for mod in (
        "app", "tenant", "auth", "resources", "presync", "isolation",
        "background", "hooks", "naming", "presentation", "controller",
        "cache", "central", "policy", "entrypoint",
    ):
        importlib.import_module(f"frappe_microservice.{mod}")


def walk(package):
    """Import every submodule of `package`, returning {module: error}."""
    broken = {}
    try:
        pkg = importlib.import_module(package)
    except Exception as e:
        return {package: repr(e)}
    for _, name, _ in pkgutil.walk_packages(pkg.__path__, f"{package}."):
        if ".tests" in name or name.endswith("_test") or ".test_" in name:
            continue
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as e:
            broken[name] = str(e)
        except Exception:
            # Non-import failures (needs a DB, needs a site) are not our concern.
            pass
    return broken


def main():
    try:
        boot()
    except Exception as e:
        print(f"FAIL: service boot raised {type(e).__name__}: {e}")
        return 1
    print("OK: service boot imports cleanly")

    unexpected = {}
    for package in ("frappe", "erpnext"):
        for name, err in walk(package).items():
            if not name.startswith(ALLOWED_PREFIXES):
                unexpected[name] = err

    if unexpected:
        print(f"\nFAIL: {len(unexpected)} module(s) broken outside the pruned features:")
        for name, err in sorted(unexpected.items())[:40]:
            print(f"  {name}: {err}")
        return 1

    print("OK: no Frappe/ERPNext module broken outside the pruned features")
    return 0


if __name__ == "__main__":
    sys.exit(main())
