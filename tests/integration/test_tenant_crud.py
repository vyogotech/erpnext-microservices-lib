"""
Integration tests: TenantAwareDB CRUD with child tables.

Regression coverage for the bug where update_doc used setattr() for all fields,
leaving child-table rows as plain dicts.  Frappe's Document._set_defaults() then
crashed with:

    AttributeError: 'dict' object has no attribute 'is_new'

The fix changed update_doc to use doc.update(data) so child rows go through
Frappe's extend() -> _init_child() and become real Document instances.

These tests run inside the container against a live Frappe/ERPNext site.
"""

import pytest
import frappe
from frappe.model.base_document import BaseDocument

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pi_data(company, supplier, item, accounts):
    """Minimal valid Purchase Invoice dict (mirrors a mobile-app POST body)."""
    row = {"item_code": item, "qty": 1, "rate": 100.0}
    if accounts.get("expense_account"):
        row["expense_account"] = accounts["expense_account"]
    if accounts.get("cost_center"):
        row["cost_center"] = accounts["cost_center"]
    return {
        "supplier": supplier,
        "company": company,
        "items": [row],
    }


def _make_item_row(item, accounts, qty=1, rate=100.0):
    row = {"item_code": item, "qty": qty, "rate": rate}
    if accounts.get("expense_account"):
        row["expense_account"] = accounts["expense_account"]
    if accounts.get("cost_center"):
        row["cost_center"] = accounts["cost_center"]
    return row


# ---------------------------------------------------------------------------
# update_doc with child tables (the core regression)
# ---------------------------------------------------------------------------

class TestUpdateDocChildTables:
    """update_doc must properly convert child-table dicts to Documents."""

    def test_update_scalar_field(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """Baseline: updating a scalar field works."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )

        updated = tenant_db.update_doc(
            "Purchase Invoice", doc.name,
            {"remarks": "scalar update test"},
            ignore_permissions=True,
        )

        assert updated.remarks == "scalar update test"

    def test_update_with_child_table_items(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """Regression: PUT body with 'items' list must not crash on is_new()."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )

        updated = tenant_db.update_doc(
            "Purchase Invoice", doc.name,
            {
                "items": [
                    _make_item_row(test_item, test_accounts, qty=2, rate=50.0),
                    _make_item_row(test_item, test_accounts, qty=3, rate=25.0),
                ],
            },
            ignore_permissions=True,
        )

        assert len(updated.items) == 2

    def test_child_rows_are_documents(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """After update, every child row must be a Document (has is_new)."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )

        updated = tenant_db.update_doc(
            "Purchase Invoice", doc.name,
            {"items": [_make_item_row(test_item, test_accounts, qty=5, rate=10.0)]},
            ignore_permissions=True,
        )

        for row in updated.items:
            assert isinstance(row, BaseDocument), (
                f"Expected child Document, got {type(row).__name__}"
            )
            assert hasattr(row, "is_new")

    def test_update_mixed_scalar_and_child(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """Scalar + child-table fields in a single update call."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )

        updated = tenant_db.update_doc(
            "Purchase Invoice", doc.name,
            {
                "remarks": "mixed update",
                "items": [_make_item_row(test_item, test_accounts, qty=7, rate=14.0)],
            },
            ignore_permissions=True,
        )

        assert updated.remarks == "mixed update"
        assert len(updated.items) == 1

    def test_update_replaces_all_child_rows(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """Sending a new items list replaces old rows entirely."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )
        assert len(doc.items) == 1

        updated = tenant_db.update_doc(
            "Purchase Invoice", doc.name,
            {
                "items": [
                    _make_item_row(test_item, test_accounts, qty=1, rate=10.0),
                    _make_item_row(test_item, test_accounts, qty=2, rate=20.0),
                    _make_item_row(test_item, test_accounts, qty=3, rate=30.0),
                ],
            },
            ignore_permissions=True,
        )

        assert len(updated.items) == 3

    def test_update_preserves_existing_name_on_rows(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        """Sending rows with 'name' set (Desk-style) keeps them as updates."""
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )
        frappe.db.commit()

        original_row_name = doc.items[0].name
        row = _make_item_row(test_item, test_accounts, qty=99, rate=1.0)
        row["name"] = original_row_name
        row["doctype"] = "Purchase Invoice Item"

        updated = tenant_db.update_doc(
            "Purchase Invoice", doc.name,
            {"items": [row]},
            ignore_permissions=True,
        )

        assert len(updated.items) == 1
        assert updated.items[0].qty == 99


# ---------------------------------------------------------------------------
# insert_doc with child tables (no regression)
# ---------------------------------------------------------------------------

class TestInsertDocChildTables:
    """Verify insert_doc still works correctly with child tables."""

    def test_insert_with_items(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )

        assert doc.name is not None
        assert len(doc.items) >= 1
        for row in doc.items:
            assert isinstance(row, BaseDocument)

    def test_insert_with_multiple_items(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        data = _pi_data(test_company, test_supplier, test_item, test_accounts)
        data["items"].append(
            _make_item_row(test_item, test_accounts, qty=2, rate=50.0)
        )

        doc = tenant_db.insert_doc(
            "Purchase Invoice", data,
            ignore_permissions=True, ignore_mandatory=True,
        )

        assert len(doc.items) == 2


# ---------------------------------------------------------------------------
# delete_doc
# ---------------------------------------------------------------------------

class TestDeleteDoc:
    """Basic delete_doc coverage with tenant verification."""

    def test_delete_draft_invoice(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )
        name = doc.name
        frappe.db.commit()

        tenant_db.delete_doc("Purchase Invoice", name)

        assert not frappe.db.exists("Purchase Invoice", name)


# ---------------------------------------------------------------------------
# Full CRUD cycle
# ---------------------------------------------------------------------------

class TestFullCRUDCycle:
    """End-to-end: create -> read -> update (with child table) -> delete."""

    def test_crud_cycle(
        self, tenant_db, test_company, test_supplier, test_item,
        test_accounts, ensure_fiscal_year,
    ):
        # CREATE
        doc = tenant_db.insert_doc(
            "Purchase Invoice",
            _pi_data(test_company, test_supplier, test_item, test_accounts),
            ignore_permissions=True, ignore_mandatory=True,
        )
        name = doc.name
        assert name is not None
        frappe.db.commit()

        # READ
        fetched = tenant_db.get_doc("Purchase Invoice", name)
        assert fetched.name == name
        assert len(fetched.items) == 1

        # UPDATE with child table (the path that was broken)
        updated = tenant_db.update_doc(
            "Purchase Invoice", name,
            {
                "remarks": "crud cycle",
                "items": [
                    _make_item_row(test_item, test_accounts, qty=10, rate=5.0),
                    _make_item_row(test_item, test_accounts, qty=20, rate=2.5),
                ],
            },
            ignore_permissions=True,
        )
        assert updated.remarks == "crud cycle"
        assert len(updated.items) == 2
        for row in updated.items:
            assert isinstance(row, BaseDocument)
        frappe.db.commit()

        # DELETE
        tenant_db.delete_doc("Purchase Invoice", name)
        assert not frappe.db.exists("Purchase Invoice", name)
