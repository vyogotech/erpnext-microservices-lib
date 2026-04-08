"""
Fixtures for integration tests that run against a live Frappe/ERPNext site.

These tests run INSIDE the container (via docker compose exec) where the real
Frappe framework, ERPNext, MariaDB, and Redis are available.

The parent tests/conftest.py (which mocks frappe entirely) is excluded via
--confcutdir when invoking pytest — see scripts/run_integration_tests.sh.
"""

import os
from datetime import date

import pytest

import frappe
from frappe_microservice.tenant import TenantAwareDB, patch_valid_dict_for_tenant_id

TEST_TENANT_ID = "integ-test-tenant-001"

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
        frappe.db.sql(
            f"ALTER TABLE `tab{doctype}` ADD COLUMN `{column}` {coltype}"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session-scoped: boot Frappe once for the entire test run
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def frappe_session():
    """Boot Frappe framework against the live site."""
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
    ):
        _ensure_column(dt, "tenant_id")

    patch_valid_dict_for_tenant_id()
    frappe.db.commit()

    yield

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

    fy = frappe.get_doc({
        "doctype": "Fiscal Year",
        "year": fy_name,
        "year_start_date": today.replace(month=1, day=1).isoformat(),
        "year_end_date": today.replace(month=12, day=31).isoformat(),
    })
    fy.insert(ignore_permissions=True, ignore_mandatory=True)
    frappe.db.commit()
    return fy_name


@pytest.fixture(scope="session")
def test_supplier(frappe_session, test_company):
    """Ensure a test Supplier exists with the integration test tenant_id."""
    name = "_Test Integ Supplier"
    if not frappe.db.exists("Supplier", name):
        sg = frappe.get_all("Supplier Group", limit=1, pluck="name")
        doc = frappe.get_doc({
            "doctype": "Supplier",
            "supplier_name": name,
            "supplier_group": sg[0] if sg else "All Supplier Groups",
        })
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
        doc = frappe.get_doc({
            "doctype": "Item",
            "item_code": name,
            "item_name": name,
            "item_group": "All Item Groups",
            "is_stock_item": 0,
            "is_purchase_item": 1,
        })
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
    """TenantAwareDB wired to the integration test tenant."""
    return TenantAwareDB(lambda: TEST_TENANT_ID)


@pytest.fixture(autouse=True)
def _rollback():
    """Roll back uncommitted changes after each test."""
    yield
    frappe.db.rollback()
