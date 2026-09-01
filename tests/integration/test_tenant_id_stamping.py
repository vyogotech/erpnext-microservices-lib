"""
Integration tests: who owns tenant_id stamping, and what each session user gets.

The image under test ships tenancy_app, whose ``"*"`` before_insert/before_save
hook (``tenancy_app.utils.tenant.set_tenant_id``) is the authority on
tenant_id. It branches on the *session user*, not on whatever the caller put on
the document:

    Administrator  -> tenant_id = "SYSTEM"           (platform-level data)
    tenant user    -> tenant_id = user's tenant_id   (resolved server-side)

``TenantAwareDB`` resolves the tenant from its own callable and then verifies
the persisted value after insert. The two must agree, which means a tenant-
scoped write has to run under the matching tenant user. These tests pin down
both halves of that contract:

  * TestAdministratorStamping — the Administrator path, including the guard
    that fires when a caller asks TenantAwareDB for a tenant while the session
    is still Administrator. That mismatch is what broke the CRUD suite, and it
    is the same shape as a microservice writing with
    ``INTERNAL_SERVICE_USER=Administrator``.
  * TestTenantUserStamping — the production path, asserting the tenant lands on
    the parent row and every child row.
"""

import importlib.util

import pytest
import frappe

from .conftest import TEST_TENANT_ID, TEST_TENANT_USER, item_row, pi_data

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _requires_tenancy_app(frappe_session):
    """Skip unless tenancy_app's stamping hook can actually run in this process.

    Two separate questions, and only checking the first is not enough:

    1. Is tenancy_app recorded as installed? ``frappe.get_installed_apps()``
       answers this from the *database*.
    2. Can this process load it? A microservice image deliberately does not ship
       tenancy_app (see ``tenant.py`` — the library's ``get_valid_dict`` patch
       is the replacement), so its ``"*"`` before_insert hook never fires there.

    Running against a central-site database makes the two diverge: the DB lists
    tenancy_app while the service image cannot import it. Every test in this
    module asserts on hook behaviour, so both must hold.
    """
    if "tenancy_app" not in (frappe.get_installed_apps() or []):
        pytest.skip("tenancy_app not installed on this site")
    try:
        importable = importlib.util.find_spec("tenancy_app") is not None
    except (ImportError, ValueError):
        importable = False
    if not importable:
        pytest.skip("tenancy_app not importable here (microservice image)")


def _child_tenant_ids(parent_name):
    """tenant_id of every Purchase Invoice Item row under `parent_name`."""
    rows = frappe.db.sql(
        "SELECT tenant_id FROM `tabPurchase Invoice Item` WHERE parent = %s",
        (parent_name,),
        as_dict=True,
    )
    return [r["tenant_id"] for r in rows]


# ---------------------------------------------------------------------------
# Administrator
# ---------------------------------------------------------------------------

class TestAdministratorStamping:
    """Administrator writes are platform-level and get tenant_id = SYSTEM."""

    def test_insert_is_stamped_system(
        self, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """A plain doc.insert() as Administrator lands as SYSTEM, not NULL."""
        frappe.set_user("Administrator")

        doc = frappe.get_doc({
            "doctype": "Purchase Invoice",
            **pi_data(test_company, test_supplier, test_item, test_accounts),
        })
        doc.insert(ignore_permissions=True, ignore_mandatory=True)

        stored = frappe.db.get_value("Purchase Invoice", doc.name, "tenant_id")
        assert stored == "SYSTEM", (
            f"Administrator insert should be stamped SYSTEM, got {stored!r}"
        )

    def test_child_rows_are_stamped_system(
        self, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """sync_child_table_tenant_id propagates SYSTEM down to child rows."""
        frappe.set_user("Administrator")

        doc = frappe.get_doc({
            "doctype": "Purchase Invoice",
            **pi_data(test_company, test_supplier, test_item, test_accounts),
        })
        doc.insert(ignore_permissions=True, ignore_mandatory=True)

        child_tenants = _child_tenant_ids(doc.name)
        assert child_tenants, "expected at least one Purchase Invoice Item row"
        assert set(child_tenants) == {"SYSTEM"}

    def test_administrator_is_never_given_the_test_tenant(self):
        """The suite must not stamp its tenant onto Administrator.

        Assigning TEST_TENANT_ID to Administrator would make the CRUD suite pass
        for the wrong reason — the hook would resolve the right tenant from the
        wrong branch — and it would leak into every other suite sharing the
        site. Deliberately asserts only that, leaving whatever the image sets
        Administrator to (SYSTEM, tenant-0, unset) alone.
        """
        admin_tenant = frappe.db.get_value("User", "Administrator", "tenant_id")
        assert admin_tenant != TEST_TENANT_ID, (
            "Administrator was stamped with the integration test tenant; "
            "tenant-scoped tests must run as TEST_TENANT_USER instead."
        )

    def test_tenant_aware_db_rejects_administrator_session(
        self, admin_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """Asking for a tenant while the session is Administrator must fail loudly.

        insert_doc puts the requested tenant on the document, the hook overwrites
        it with SYSTEM, and the post-insert guard catches the divergence. Without
        that guard the row would silently persist as SYSTEM — which
        tenancy_app's read filters expose to *every* tenant.
        """
        with pytest.raises(ValueError, match="tenant_id mismatch after insert"):
            admin_db.insert_doc(
                "Purchase Invoice",
                pi_data(test_company, test_supplier, test_item, test_accounts),
                ignore_permissions=True, ignore_mandatory=True,
            )


# ---------------------------------------------------------------------------
# Tenant user
# ---------------------------------------------------------------------------

class TestTenantUserStamping:
    """The production path: a real tenant user gets its own tenant_id."""

    def test_session_user_is_the_tenant_user(self, tenant_db):
        """The tenant_db fixture switches the session, not just the callable."""
        assert frappe.session.user == TEST_TENANT_USER
        assert tenant_db.get_tenant_id() == TEST_TENANT_ID

    def test_insert_stamps_the_users_tenant_id(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )

        stored = frappe.db.get_value("Purchase Invoice", doc.name, "tenant_id")
        assert stored == TEST_TENANT_ID, (
            f"Expected tenant_id={TEST_TENANT_ID!r}, got {stored!r} — "
            "set_tenant_id may have overwritten it."
        )

    def test_child_rows_inherit_the_parent_tenant_id(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )

        child_tenants = _child_tenant_ids(doc.name)
        assert child_tenants, "expected at least one Purchase Invoice Item row"
        assert set(child_tenants) == {TEST_TENANT_ID}

    def test_direct_insert_without_tenant_aware_db_also_stamps(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """The hook resolves from the session, independently of TenantAwareDB.

        Proves the stamping is the platform's doing rather than something
        insert_doc arranges for itself.
        """
        doc = frappe.get_doc({
            "doctype": "Purchase Invoice",
            **pi_data(test_company, test_supplier, test_item, test_accounts),
        })
        doc.insert(ignore_permissions=True, ignore_mandatory=True)

        stored = frappe.db.get_value("Purchase Invoice", doc.name, "tenant_id")
        assert stored == TEST_TENANT_ID

    def test_update_preserves_tenant_id(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """before_save runs set_tenant_id too — an update must not drift."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )

        tenant_db.update_doc(
            "Purchase Invoice", doc.name,
            {
                "remarks": "tenant_id stamping check",
                "items": [item_row(test_item, test_accounts, qty=4, rate=25.0)],
            },
            ignore_permissions=True,
        )

        stored = frappe.db.get_value("Purchase Invoice", doc.name, "tenant_id")
        assert stored == TEST_TENANT_ID
        assert set(_child_tenant_ids(doc.name)) == {TEST_TENANT_ID}
