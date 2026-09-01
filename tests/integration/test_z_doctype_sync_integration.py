"""
Integration test for Hibernate-style DocType schema sync.

Runs INSIDE the ERPNext container (vyogo/erpnext:sne-version-16) where the real
Frappe framework, MariaDB, and Redis are available. Verifies that
``presync_service_doctypes`` in the default ``migrate`` mode:

1. Creates a new DocType + table with all declared columns.
2. Applies an additive schema change (new field → new column) on the next run,
   preserving previously inserted row data.
3. Skips an unchanged JSON on re-run (no metadata modified-timestamp churn).
4. Ensures the ``tenant_id`` column follows the tenancy mode — present under
   ``MULTI_TENANT=1``, and absent under ``MULTI_TENANT=0`` (where re-creating it
   would silently reintroduce the tenancy-provider dependency).
"""

import json
import os
import time

import pytest

import frappe
from frappe_microservice.isolation import presync_service_doctypes
from frappe_microservice.policy import resolve_bool

pytestmark = pytest.mark.integration

DOCTYPE_NAME = "FMS Sync Test Doctype"
TABLE = f"tab{DOCTYPE_NAME}"
SITES_PATH = os.environ.get("FRAPPE_SITES_PATH", "/home/frappe/frappe-bench/sites")


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


def _write_doctype(doctypes_root, fields):
    """Write <root>/fms_sync_test_doctype/fms_sync_test_doctype.json."""
    folder = os.path.join(doctypes_root, "fms_sync_test_doctype")
    os.makedirs(folder, exist_ok=True)
    from datetime import datetime

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    doc = {
        "actions": [],
        "creation": "2024-01-01 00:00:00.000000",
        "doctype": "DocType",
        "engine": "InnoDB",
        "field_order": [f["fieldname"] for f in fields],
        "fields": fields,
        "links": [],
        "modified": now_str,
        "module": "Custom",
        "name": DOCTYPE_NAME,
        "custom": 1,
        "owner": "Administrator",
        "permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
        "sort_field": "modified",
        "sort_order": "DESC",
    }
    with open(os.path.join(folder, "fms_sync_test_doctype.json"), "w") as f:
        json.dump(doc, f, indent=1)
    return doctypes_root


def _run_sync(doctypes_root):
    try:
        presync_service_doctypes(
            doctypes_path=doctypes_root,
            service_name="fms-sync-test",
            site=SITE,
            sites_path=SITES_PATH,
            fixtures_path="/nonexistent",
        )
    finally:
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


@pytest.fixture
def _cleanup():
    yield
    # presync_service_doctypes() and this test manage their own frappe
    # lifecycle (init/connect/destroy). Re-establish a bound connection at
    # teardown, delete the temp DocType, and leave frappe BOUND so the parent
    # conftest's autouse _rollback fixture (frappe.db.rollback()) still works.
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
    except Exception:
        try:
            frappe.db.rollback()
        except Exception:
            pass
    # Intentionally do NOT frappe.destroy() here (keep the connection bound).


def test_doctype_sync_create_update_skip_and_tenant_id(tmp_path, _cleanup):
    root = str(tmp_path / "doctype")
    os.makedirs(root, exist_ok=True)

    # 1) create with two fields
    _write_doctype(
        root,
        [
            {"fieldname": "field_one", "fieldtype": "Data", "label": "Field One"},
            {"fieldname": "field_two", "fieldtype": "Data", "label": "Field Two"},
        ],
    )
    _run_sync(root)

    frappe.init(site=SITE, sites_path=SITES_PATH)
    frappe.connect()
    assert frappe.db.exists("DocType", DOCTYPE_NAME)
    assert frappe.db.has_column(DOCTYPE_NAME, "field_one")
    assert frappe.db.has_column(DOCTYPE_NAME, "field_two")

    # 4) tenant_id column follows the tenancy mode.
    #
    # In single-tenant mode its ABSENCE is the assertion that matters: presync
    # adding the column back is exactly the regression MULTI_TENANT=0 exists to
    # prevent, and it would silently reintroduce the tenancy-provider dependency.
    if resolve_bool(None, "MULTI_TENANT", default=True):
        assert frappe.db.has_column(DOCTYPE_NAME, "tenant_id")
    else:
        assert not frappe.db.has_column(DOCTYPE_NAME, "tenant_id")

    # Insert a row to prove data survives the additive migration.
    doc = frappe.get_doc(
        {
            "doctype": DOCTYPE_NAME,
            "field_one": "hello",
            "field_two": "world",
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    row_name = doc.name
    modified_before = frappe.db.get_value("DocType", DOCTYPE_NAME, "modified")

    # 2) additive change: add a third field, re-run
    time.sleep(1)
    _write_doctype(
        root,
        [
            {"fieldname": "field_one", "fieldtype": "Data", "label": "Field One"},
            {"fieldname": "field_two", "fieldtype": "Data", "label": "Field Two"},
            {"fieldname": "field_three", "fieldtype": "Data", "label": "Field Three"},
        ],
    )
    _run_sync(root)

    frappe.init(site=SITE, sites_path=SITES_PATH)
    frappe.connect()
    assert frappe.db.has_column(DOCTYPE_NAME, "field_three")
    # previously inserted data survived
    assert frappe.db.get_value(DOCTYPE_NAME, row_name, "field_one") == "hello"
    modified_after_update = frappe.db.get_value("DocType", DOCTYPE_NAME, "modified")
    assert modified_after_update > modified_before

    # 3) no-change re-run is skipped (DocType metadata modified stamp unchanged)
    _run_sync(root)
    frappe.init(site=SITE, sites_path=SITES_PATH)
    frappe.connect()
    modified_after_noop = frappe.db.get_value("DocType", DOCTYPE_NAME, "modified")
    assert (
        modified_after_noop == modified_after_update
    ), "unchanged JSON should be skipped (modified timestamp must not change)"
