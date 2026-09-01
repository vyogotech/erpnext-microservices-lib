"""
Fixtures for integration tests that run against a live Frappe/ERPNext site.

These tests run INSIDE the container (via docker compose exec) where the real
Frappe framework, ERPNext, MariaDB, and Redis are available.

The parent tests/conftest.py (which mocks frappe entirely) is excluded via
--confcutdir when invoking pytest — see scripts/run_integration_tests.sh.

Session user model
------------------
The image under test (docker.io/vyogo/erpnext) ships tenancy_app, whose
``"*": before_insert`` hook (``tenancy_app.utils.tenant.set_tenant_id``) owns
tenant_id stamping. It branches on the session user:

  Administrator  -> doc.tenant_id = "SYSTEM"          (platform-level data)
  tenant user    -> doc.tenant_id = <user's tenant_id> (resolved server-side)

So tenant-scoped CRUD must run as a *real tenant user*, exactly as it does in
production — running as Administrator gets every document stamped SYSTEM, which
``TenantAwareDB.insert_doc``'s post-insert guard then rejects.

  Bootstrapping (companies, suppliers, items, users) runs as Administrator.
  Document CRUD under test runs as TEST_TENANT_USER via the ``tenant_db`` fixture.
  Administrator stays tenant_id = SYSTEM throughout — it is never assigned a
  test tenant. ``test_tenant_id_stamping.py`` covers that path deliberately.
"""

import os
from datetime import date

import pytest

import frappe
from frappe_microservice.tenant import TenantAwareDB, patch_valid_dict_for_tenant_id

TEST_TENANT_ID = "integ-test-tenant-001"
TEST_TENANT_USER = "integ-bot@lib-test.local"

# Accounts Manager is required for delete_doc: on Purchase Invoice "Accounts
# User" has delete=0 and only "Accounts Manager" carries delete=1 (System
# Manager is not on the Purchase Invoice perm list at all). Administrator used
# to mask this because it bypasses the permission system entirely.
TENANT_USER_ROLES = ["System Manager", "Accounts User", "Accounts Manager"]

# Session key tenancy_app uses to pin its tenant_id cache to the user it was
# resolved for. Must match _SESSION_TENANT_OWNER_KEY in
# tenancy_app/utils/tenant.py.
_SESSION_TENANT_OWNER_KEY = "tenant_id_user"

BENCH_PATH = "/home/frappe/frappe-bench"
DEFAULT_SITES_PATH = os.path.join(BENCH_PATH, "sites")


def _discover_site():
    """Auto-discover site name from env, currentsite.txt, or directory scan."""
    site = os.environ.get("FRAPPE_SITE")
    if site:
        return site

    sites_path = os.environ.get("FRAPPE_SITES_PATH", DEFAULT_SITES_PATH)
    currentsite = os.path.join(sites_path, "currentsite.txt")
    if os.path.exists(currentsite):
        with open(currentsite) as f:
            name = f.read().strip()
            if name:
                return name

    for candidate in ("dev.localhost", "frontend", "site1.local"):
        if os.path.isdir(os.path.join(sites_path, candidate)):
            return candidate

    return "dev.localhost"


def _ensure_column(doctype, column, coltype="varchar(140)"):
    """Add a column via ALTER TABLE if it doesn't already exist."""
    try:
        frappe.db.sql(f"ALTER TABLE `tab{doctype}` ADD COLUMN `{column}` {coltype}")
    except Exception:
        pass


def _switch_to_tenant(user_email, tenant_id):
    """Switch the Frappe session user AND stamp tenancy_app's session cache.

    Mirrors what tenancy_app's on_session_creation() does on a real web
    login. Without the cache stamp, set_tenant_id() falls back to a
    ``frappe.db.get_value("User", ..., "tenant_id")`` lookup, which the autouse
    ``_rollback`` fixture can leave pointing at stale data between tests.
    """
    frappe.set_user(user_email)
    frappe.session["tenant_id"] = tenant_id
    frappe.session[_SESSION_TENANT_OWNER_KEY] = user_email


def _ensure_test_user(email, tenant_id, roles=None):
    """Create a tenant user (as Administrator) and stamp its tenant_id.

    The tenant_id is written with raw SQL rather than through the document
    model on purpose: this runs while the session user is still Administrator,
    and tenancy_app's set_tenant_id hook would stamp SYSTEM over it. That is
    the same thing the provisioning service does when a customer signs up.
    """
    if roles is None:
        roles = TENANT_USER_ROLES

    if not frappe.db.exists("User", email):
        frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": email.split("@")[0].replace("-", " ").title(),
                "send_welcome_email": 0,
                "roles": [{"role": r} for r in roles],
            }
        ).insert(ignore_permissions=True)
    else:
        # Reconcile roles on a user left behind by an earlier run — otherwise a
        # user seeded before TENANT_USER_ROLES changed keeps the stale role set.
        user_doc = frappe.get_doc("User", email)
        missing = set(roles) - {r.role for r in user_doc.roles}
        if missing:
            user_doc.add_roles(*sorted(missing))

    frappe.db.sql(
        "UPDATE `tabUser` SET tenant_id = %s WHERE name = %s",
        (tenant_id, email),
    )
    frappe.db.commit()


# ---------------------------------------------------------------------------
# Shared Purchase Invoice payload helpers
# ---------------------------------------------------------------------------


def pi_data(company, supplier, item, accounts):
    """Minimal valid Purchase Invoice dict (mirrors a mobile-app POST body)."""
    return {
        "supplier": supplier,
        "company": company,
        "items": [item_row(item, accounts)],
    }


def item_row(item, accounts, qty=1, rate=100.0):
    """One Purchase Invoice Item row, with accounts filled in when discovered."""
    row = {"item_code": item, "qty": qty, "rate": rate}
    if accounts.get("expense_account"):
        row["expense_account"] = accounts["expense_account"]
    if accounts.get("cost_center"):
        row["cost_center"] = accounts["cost_center"]
    return row


# ---------------------------------------------------------------------------
# Session-scoped: boot Frappe once for the entire test run
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def frappe_session():
    """Boot Frappe framework against the live site.

    Runs as Administrator throughout. Administrator is deliberately NOT given a
    test tenant_id — it stays SYSTEM, which is its correct production value.
    Tenant-scoped work switches to TEST_TENANT_USER via the ``tenant_db``
    fixture.
    """
    sites_path = os.environ.get("FRAPPE_SITES_PATH", DEFAULT_SITES_PATH)
    site = _discover_site()

    frappe.init(site=site, sites_path=sites_path)
    frappe.connect()
    frappe.set_user("Administrator")
    frappe.flags.in_test = True

    for dt in (
        "Purchase Invoice",
        "Purchase Invoice Item",
        "Purchase Taxes and Charges",
        "Supplier",
        "Item",
        "User",
    ):
        _ensure_column(dt, "tenant_id")

    patch_valid_dict_for_tenant_id()
    frappe.db.commit()

    _ensure_test_user(TEST_TENANT_USER, TEST_TENANT_ID)

    yield

    frappe.set_user("Administrator")
    frappe.destroy()


@pytest.fixture(scope="session")
def test_company(frappe_session):
    """Return the first Company on the site (created by ERPNext setup wizard)."""
    rows = frappe.get_all("Company", limit=1, pluck="name")
    if not rows:
        pytest.skip("No Company found — ERPNext setup wizard not run")
    return rows[0]


@pytest.fixture(scope="session")
def test_accounts(frappe_session, test_company):
    """Discover expense account, cost center, and payable account."""
    expense = frappe.db.get_value(
        "Account",
        {"company": test_company, "root_type": "Expense", "is_group": 0},
        "name",
    )
    cost_center = frappe.db.get_value(
        "Cost Center",
        {"company": test_company, "is_group": 0},
        "name",
    )
    payable = frappe.db.get_value(
        "Account",
        {"company": test_company, "account_type": "Payable", "is_group": 0},
        "name",
    )
    return {
        "expense_account": expense,
        "cost_center": cost_center,
        "payable_account": payable,
    }


@pytest.fixture(scope="session")
def ensure_fiscal_year(frappe_session, test_company):
    """Ensure a Fiscal Year covers today's date."""
    today = date.today()

    existing = frappe.db.sql(
        "SELECT name FROM `tabFiscal Year` "
        "WHERE year_start_date <= %s AND year_end_date >= %s LIMIT 1",
        (today, today),
        as_dict=True,
    )
    if existing:
        return existing[0]["name"]

    fy_name = str(today.year)
    if frappe.db.exists("Fiscal Year", fy_name):
        return fy_name

    fy = frappe.get_doc(
        {
            "doctype": "Fiscal Year",
            "year": fy_name,
            "year_start_date": today.replace(month=1, day=1).isoformat(),
            "year_end_date": today.replace(month=12, day=31).isoformat(),
        }
    )
    fy.insert(ignore_permissions=True, ignore_mandatory=True)
    frappe.db.commit()
    return fy_name


@pytest.fixture(scope="session")
def test_supplier(frappe_session, test_company):
    """Ensure a test Supplier exists with the integration test tenant_id."""
    name = "_Test Integ Supplier"
    if not frappe.db.exists("Supplier", name):
        sg = frappe.get_all("Supplier Group", limit=1, pluck="name")
        doc = frappe.get_doc(
            {
                "doctype": "Supplier",
                "supplier_name": name,
                "supplier_group": sg[0] if sg else "All Supplier Groups",
            }
        )
        doc.tenant_id = TEST_TENANT_ID
        doc.insert(ignore_permissions=True)
    frappe.db.set_value("Supplier", name, "tenant_id", TEST_TENANT_ID)
    frappe.db.commit()
    return name


@pytest.fixture(scope="session")
def test_item(frappe_session):
    """Ensure a test Item exists with the integration test tenant_id."""
    name = "_Test Integ Item"
    if not frappe.db.exists("Item", name):
        doc = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": name,
                "item_name": name,
                "item_group": "All Item Groups",
                "is_stock_item": 0,
                "is_purchase_item": 1,
            }
        )
        doc.tenant_id = TEST_TENANT_ID
        doc.insert(ignore_permissions=True)
    frappe.db.set_value("Item", name, "tenant_id", TEST_TENANT_ID)
    frappe.db.commit()
    return name


# ---------------------------------------------------------------------------
# Function-scoped
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_db(frappe_session):
    """TenantAwareDB wired to the integration test tenant.

    Also switches the Frappe session to the matching tenant user, which is what
    ``secure_route`` does in production before any tenant_db call. Both halves
    have to agree: TenantAwareDB resolves the tenant from its callable, while
    tenancy_app's before_insert hook resolves it from the session user. If the
    session were left as Administrator the hook would stamp SYSTEM and
    ``insert_doc``'s post-insert guard would reject every write.

    Session-scoped fixtures (companies, suppliers, items) are instantiated
    before this one by pytest, so their bootstrapping still runs as
    Administrator.
    """
    _switch_to_tenant(TEST_TENANT_USER, TEST_TENANT_ID)
    yield TenantAwareDB(lambda: TEST_TENANT_ID)
    frappe.set_user("Administrator")


@pytest.fixture
def admin_db(frappe_session):
    """TenantAwareDB bound to the test tenant while the session is Administrator.

    The deliberately mismatched pairing used by ``test_tenant_id_stamping.py``
    to pin down what happens on the Administrator write path.
    """
    frappe.set_user("Administrator")
    return TenantAwareDB(lambda: TEST_TENANT_ID)


@pytest.fixture(autouse=True)
def _rollback():
    """Roll back uncommitted changes after each test."""
    yield
    frappe.db.rollback()
