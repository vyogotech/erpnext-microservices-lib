"""
Integration tests for presync runtime (module maps, fixtures, post-presync hook).

Runs against a live Frappe site inside the integration container. Mirrors the
signup-service / auth-service / reports-service boot sequence:

1. module_def.json imported before doctype/fixture JSONs
2. Custom module name registered in-memory so DocType metadata imports cleanly
3. SERVICE_POST_PRESYNC hook runs via importlib (not frappe.get_attr)
"""

import json
import os
import sys

import pytest

import frappe
from frappe_microservice.isolation import presync_service_doctypes

pytestmark = pytest.mark.integration

DOCTYPE_NAME = "FMS Presync Integration Doctype"
MODULE_NAME = "Presync Integration Service"
SERVICE_APP = "presync_integration_service"
SITES_PATH = os.environ.get("FRAPPE_SITES_PATH", "/home/frappe/frappe-bench/sites")
MARKER_KEY = "fms_presync_integration_marker"


def _discover_site():
    site = os.environ.get("FRAPPE_SITE")
    if site:
        return site
    currentsite = os.path.join(SITES_PATH, "currentsite.txt")
    if os.path.exists(currentsite):
        with open(currentsite) as f:
            name = f.read().strip()
            if name:
                return name
    return "dev.localhost"


SITE = _discover_site()


def _write_service_layout(root):
    """Build a minimal service tree like signup-service under *root*."""
    service = os.path.join(root, "service")
    fixtures = os.path.join(service, "fixtures")
    doctypes = os.path.join(
        service, SERVICE_APP, SERVICE_APP, "doctype", "presync_integration_doctype"
    )
    os.makedirs(fixtures, exist_ok=True)
    os.makedirs(doctypes, exist_ok=True)

    app_root = os.path.join(service, SERVICE_APP)
    with open(os.path.join(app_root, "__init__.py"), "w") as f:
        pass
    with open(os.path.join(app_root, "hooks.py"), "w") as f:
        pass

    with open(os.path.join(fixtures, "module_def.json"), "w") as f:
        json.dump(
            [
                {
                    "name": MODULE_NAME,
                    "doctype": "Module Def",
                    "module_name": MODULE_NAME,
                    "app_name": SERVICE_APP,
                    "custom": 1,
                }
            ],
            f,
        )

    with open(os.path.join(doctypes, "presync_integration_doctype.json"), "w") as f:
        json.dump(
            {
                "actions": [],
                "creation": "2024-01-01 00:00:00.000000",
                "doctype": "DocType",
                "engine": "InnoDB",
                "field_order": ["title", "tenant_id"],
                "fields": [
                    {"fieldname": "title", "fieldtype": "Data", "label": "Title"},
                    {
                        "fieldname": "tenant_id",
                        "fieldtype": "Data",
                        "label": "Tenant ID",
                    },
                ],
                "links": [],
                "modified": "2024-01-01 00:00:00.000000",
                "module": MODULE_NAME,
                "name": DOCTYPE_NAME,
                "custom": 1,
                "owner": "Administrator",
                "permissions": [
                    {"role": "System Manager", "read": 1, "write": 1, "create": 1},
                ],
                "sort_field": "modified",
                "sort_order": "DESC",
            },
            f,
            indent=1,
        )

    with open(os.path.join(service, "bootstrap.py"), "w") as f:
        f.write(
            "def run_post_presync():\n"
            "    import frappe\n"
            f"    frappe.cache.set_value({MARKER_KEY!r}, 'ok')\n"
        )

    return service


def _run_presync(service_root):
    env = {
        "SERVICE_PATH": service_root,
        "SERVICE_NAME": "presync-integration-service",
        "SERVICE_POST_PRESYNC": "bootstrap.run_post_presync",
        "FRAPPE_SITE": SITE,
        "FRAPPE_SITES_PATH": SITES_PATH,
        "LOAD_FRAMEWORK_HOOKS": "erpnext",
    }
    old_env = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        if service_root not in sys.path:
            sys.path.insert(0, service_root)

        presync_service_doctypes(fixtures_path=os.path.join(service_root, "fixtures"))
    finally:
        # Clean up isolation flags so subsequent tests start fresh
        from frappe.utils.redis_wrapper import RedisWrapper

        for flag in [
            "_microservice_isolation_applied",
            "_microservice_controller_patched",
            "_microservice_load_app_hooks_patched",
            "_microservice_hooks_resolution_patched",
            "_microservice_get_hooks_jinja_patched",
        ]:
            if hasattr(frappe, flag):
                delattr(frappe, flag)
        if hasattr(RedisWrapper, "_microservice_key_patched"):
            delattr(RedisWrapper, "_microservice_key_patched")
        # Restore env
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def _cleanup():
    yield
    try:
        frappe.init(site=SITE, sites_path=SITES_PATH)
        frappe.connect()
        frappe.set_user("Administrator")
        frappe.flags.in_test = True
        frappe.flags.print_messages = True
        if frappe.db.exists("DocType", DOCTYPE_NAME):
            frappe.delete_doc(
                "DocType", DOCTYPE_NAME, force=True, ignore_permissions=True
            )
            frappe.db.commit()
        frappe.cache.delete_value(MARKER_KEY)
    except Exception:
        try:
            frappe.db.rollback()
        except Exception:
            pass


def test_presync_module_fixture_doctype_and_post_hook(tmp_path, _cleanup):
    """
    End-to-end presync: Module Def + custom DocType + post-presync hook.

    Regression guard for orphan SQL tables (DDL committed, DocType row missing)
    when frappe.local.module_app was empty during import.
    """
    service_root = _write_service_layout(str(tmp_path))
    _run_presync(service_root)

    frappe.init(site=SITE, sites_path=SITES_PATH)
    frappe.connect()

    assert frappe.db.exists("Module Def", MODULE_NAME)
    assert frappe.db.exists("DocType", DOCTYPE_NAME)
    assert frappe.db.table_exists(DOCTYPE_NAME)
    assert frappe.db.has_column(DOCTYPE_NAME, "title")
    assert frappe.db.has_column(DOCTYPE_NAME, "tenant_id")
    assert frappe.cache.get_value(MARKER_KEY) == "ok"

    doc = frappe.get_doc(
        {
            "doctype": DOCTYPE_NAME,
            "title": "integration-test",
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    assert frappe.db.get_value(DOCTYPE_NAME, doc.name, "title") == "integration-test"
