"""
Comprehensive unit tests for TenantAwareDB class

Tests cover:
- Tenant filter injection (all branches)
- Insert operations with tenant isolation
- Update operations with tenant verification
- Delete operations with tenant verification
- Query operations (get_all, get_value, get_doc)
- Error handling and edge cases
- Configurable tenant verification
- SYSTEM tenant guard (CRITICAL SECURITY)
- Document hooks integration
"""

import sys
import os
import pytest
from unittest.mock import MagicMock, patch
import frappe

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frappe_microservice.core import TenantAwareDB


class TestTenantAwareDBFilters:
    """Test tenant filter injection logic - all branches"""

    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)

    def test_add_tenant_filter_dict(self, db):
        """Test adding tenant filter to dict filters"""
        filters = {"status": "Active"}
        result = db._add_tenant_filter(filters)
        assert result == {"status": "Active", "tenant_id": "test_tenant"}
        # Ensure original filter is not modified (immutability check)
        assert filters == {"status": "Active"}

    def test_add_tenant_filter_list(self, db):
        """Test adding tenant filter to list filters"""
        filters = [["status", "=", "Active"]]
        result = db._add_tenant_filter(filters)
        assert result == [["status", "=", "Active"], ["tenant_id", "=", "test_tenant"]]
        # Ensure original filter is not modified
        assert filters == [["status", "=", "Active"]]

    def test_add_tenant_filter_none(self, db):
        """Test adding tenant filter when filters is None"""
        result = db._add_tenant_filter(None)
        assert result == {"tenant_id": "test_tenant"}

    def test_add_tenant_filter_no_tenant(self, db):
        """Test error when no tenant_id in context"""
        db.get_tenant_id = MagicMock(return_value=None)
        with pytest.raises(ValueError, match="No tenant_id found in context"):
            db._add_tenant_filter({"status": "Active"})

    def test_add_tenant_filter_system_tenant_blocked(self):
        """Test SYSTEM tenant_id is blocked (CRITICAL SECURITY)"""
        get_tenant_id = MagicMock(return_value="SYSTEM")
        db = TenantAwareDB(get_tenant_id)

        with pytest.raises(ValueError, match="SYSTEM tenant_id is not allowed"):
            db._add_tenant_filter({})


class TestTenantAwareDBQueries:
    """Test query operations with tenant filtering"""

    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)

    @patch("frappe.get_all", create=True)
    def test_get_all_calls_frappe(self, mock_get_all, db):
        """Test get_all adds tenant filter"""
        db.get_all("Sales Order", filters={"name": "SO-001"})
        mock_get_all.assert_called_once_with(
            "Sales Order", filters={"name": "SO-001", "tenant_id": "test_tenant"}
        )

    @patch("frappe.get_all", create=True)
    def test_get_all_with_fields(self, mock_get_all, db):
        """Test get_all with fields parameter"""
        db.get_all(
            "Sales Order", filters={"status": "Draft"}, fields=["name", "customer"]
        )

        call_args = mock_get_all.call_args
        assert call_args[1]["filters"]["tenant_id"] == "test_tenant"
        assert call_args[1]["fields"] == ["name", "customer"]

    @patch("frappe.db.get_value", create=True)
    def test_get_value_with_filters(self, mock_get_value, db):
        """Test get_value adds tenant filter"""
        mock_get_value.return_value = "CUST-001"

        db.get_value("Sales Order", "SO-001", "customer")

        # Verify tenant_id was added
        call_args = mock_get_value.call_args
        assert "tenant_id" in str(call_args)

    @patch("frappe.get_doc", create=True)
    def test_get_doc_with_verification(self, mock_get_doc, db):
        """Test get_doc verifies tenant ownership"""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "test_tenant"
        mock_get_doc.return_value = mock_doc

        result = db.get_doc("Sales Order", "SO-001")

        assert result == mock_doc

    @patch("frappe.get_doc", create=True)
    def test_get_doc_wrong_tenant(self, mock_get_doc, db):
        """Test error when getting document from different tenant"""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "different_tenant"
        mock_get_doc.return_value = mock_doc

        with pytest.raises(
            frappe.PermissionError, match="does not belong to current tenant"
        ):
            db.get_doc("Sales Order", "SO-001")


class TestTenantAwareDBInsert:
    """Test insert operations with all branches"""

    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)

    @patch("frappe.get_doc", create=True)
    @patch("frappe.db.get_value", create=True)
    def test_insert_doc_basic(self, mock_get_value, mock_get_doc, db):
        """Test basic document insertion with tenant_id"""
        mock_doc = MagicMock()
        mock_doc.name = "DOC-001"
        mock_doc.tenant_id = "test_tenant"
        mock_get_doc.return_value = mock_doc
        mock_get_value.return_value = "test_tenant"

        db.insert_doc("Sales Order", {"customer": "CUST-001"})

        # Verify tenant_id was set
        call_args = mock_get_doc.call_args[0][0]
        assert call_args["tenant_id"] == "test_tenant"

        # Verify insert was called
        mock_doc.insert.assert_called_once()

        # Verify post-insert verification
        mock_get_value.assert_called_once()

    @patch("frappe.get_doc", create=True)
    @patch("frappe.db.get_value", create=True)
    def test_insert_doc_verification_disabled(self, mock_get_value, mock_get_doc):
        """Test insert with verification disabled (performance mode)"""
        get_tenant_id = MagicMock(return_value="test_tenant")
        db = TenantAwareDB(get_tenant_id, verify_tenant_on_insert=False)

        mock_doc = MagicMock()
        mock_doc.name = "DOC-001"
        mock_get_doc.return_value = mock_doc

        db.insert_doc("Sales Order", {"customer": "CUST-001"})

        # Verification should be skipped
        mock_get_value.assert_not_called()

    @patch("frappe.get_doc", create=True)
    @patch("frappe.db.get_value", create=True)
    def test_insert_doc_verification_mismatch(self, mock_get_value, mock_get_doc, db):
        """Test error when saved tenant_id doesn't match"""
        mock_doc = MagicMock()
        mock_doc.name = "DOC-001"
        mock_get_doc.return_value = mock_doc
        mock_get_value.return_value = "wrong_tenant"  # Mismatch!

        with pytest.raises(ValueError, match="tenant_id mismatch"):
            db.insert_doc("Sales Order", {"customer": "CUST-001"})

    @patch("frappe.get_doc", create=True)
    def test_insert_doc_no_tenant_id(self, mock_get_doc):
        """Test error when no tenant_id in context"""
        get_tenant_id = MagicMock(return_value=None)
        db = TenantAwareDB(get_tenant_id)

        with pytest.raises(ValueError, match="No tenant_id in context"):
            db.insert_doc("Sales Order", {"customer": "CUST-001"})

    @patch("frappe.get_doc", create=True)
    def test_insert_doc_tenant_override_blocked(self, mock_get_doc, db):
        """Test error when trying to override tenant_id"""
        with pytest.raises(
            frappe.PermissionError, match="Cannot create document for different tenant"
        ):
            db.insert_doc(
                "Sales Order", {"customer": "CUST-001", "tenant_id": "other_tenant"}
            )


class TestTenantAwareDBUpdate:
    """Test update operations"""

    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)

    @patch("frappe.get_doc", create=True)
    def test_update_doc_basic(self, mock_get_doc, db):
        """Test basic document update with tenant verification"""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "test_tenant"

        def _apply_update(d):
            for k, v in d.items():
                setattr(mock_doc, k, v)

        mock_doc.update = MagicMock(side_effect=_apply_update)
        mock_get_doc.return_value = mock_doc

        db.update_doc("Sales Order", "SO-001", {"status": "Completed"})

        mock_doc.update.assert_called_once_with({"status": "Completed"})
        assert mock_doc.status == "Completed"
        mock_doc.save.assert_called_once()

    @patch("frappe.get_doc", create=True)
    def test_update_doc_wrong_tenant(self, mock_get_doc, db):
        """Test error when updating document from different tenant"""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "different_tenant"
        mock_get_doc.return_value = mock_doc

        with pytest.raises(
            frappe.PermissionError, match="does not belong to current tenant"
        ):
            db.update_doc("Sales Order", "SO-001", {"status": "Completed"})


class TestTenantAwareDBDelete:
    """Test delete operations"""

    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)

    @patch("frappe.get_doc", create=True)
    def test_delete_doc_basic(self, mock_get_doc, db):
        """Test basic document deletion with tenant verification"""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "test_tenant"
        mock_get_doc.return_value = mock_doc

        db.delete_doc("Sales Order", "SO-001")

        # Verify delete was called
        mock_doc.delete.assert_called_once()

    @patch("frappe.get_doc", create=True)
    def test_delete_doc_wrong_tenant(self, mock_get_doc, db):
        """Test error when deleting document from different tenant"""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "different_tenant"
        mock_get_doc.return_value = mock_doc

        with pytest.raises(
            frappe.PermissionError, match="does not belong to current tenant"
        ):
            db.delete_doc("Sales Order", "SO-001")


class TestTenantFilterStringBypass:
    """Regression: _add_tenant_filter must add tenant_id even for string filters (doc name)."""

    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)

    def test_string_filter_gets_tenant_id(self, db):
        """String filter (doc name) must be converted to dict with tenant_id."""
        result = db._add_tenant_filter("SO-001")
        assert isinstance(result, dict)
        assert result["name"] == "SO-001"
        assert result["tenant_id"] == "test_tenant"

    def test_unsupported_filter_type_raises(self, db):
        """Non-standard filter types must raise TypeError, never silently pass."""
        with pytest.raises(TypeError, match="Unsupported filters type"):
            db._add_tenant_filter(12345)

    def test_set_value_verifies_tenant_via_get_doc(self, db):
        """set_value must verify tenant ownership via get_doc, not raw exists."""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "different_tenant"
        with patch("frappe.get_doc", return_value=mock_doc):
            with pytest.raises(
                frappe.PermissionError, match="does not belong to current tenant"
            ):
                db.set_value("Sales Order", "SO-001", "status", "Completed")

    @patch("frappe.db.set_value", create=True)
    @patch("frappe.get_doc", create=True)
    def test_set_value_passes_for_same_tenant(self, mock_get_doc, mock_set_value, db):
        """set_value succeeds when doc belongs to the current tenant."""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "test_tenant"
        mock_get_doc.return_value = mock_doc
        db.set_value("Sales Order", "SO-001", "status", "Completed")
        mock_set_value.assert_called_once()

    def test_exists_with_string_name_adds_tenant(self, db):
        """exists() with a string name must scope by tenant_id."""
        with patch("frappe.db.exists", return_value=True) as mock_exists:
            db.exists("Sales Order", "SO-001")
            call_args = mock_exists.call_args
            filters = (
                call_args[0][1]
                if len(call_args[0]) > 1
                else call_args[1].get("filters")
            )
            assert isinstance(filters, dict)
            assert filters["tenant_id"] == "test_tenant"
            assert filters["name"] == "SO-001"


class TestGetDocHooksErrorHandling:
    """Tests that microservice_get_doc_hooks handles failures gracefully."""

    def test_get_doc_hooks_returns_empty_on_exception(self):
        """If original_get_doc_hooks raises, return {} instead of crashing."""
        from frappe_microservice.core import MicroserviceApp

        try:
            from tests.test_microservice_app import _reset_microservice_guards
        except ModuleNotFoundError:
            from test_microservice_app import _reset_microservice_guards

        _reset_microservice_guards()

        def exploding_get_doc_hooks():
            raise RuntimeError("hooks DB table missing")

        frappe.get_doc_hooks = exploding_get_doc_hooks
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            MicroserviceApp("test-service", load_framework_hooks=["frappe"])

        result = frappe.get_doc_hooks()
        assert result == {}

    def test_get_doc_hooks_handles_non_dict_return(self):
        """If original returns non-dict, return {} safely."""
        from frappe_microservice.core import MicroserviceApp

        try:
            from tests.test_microservice_app import _reset_microservice_guards
        except ModuleNotFoundError:
            from test_microservice_app import _reset_microservice_guards

        _reset_microservice_guards()
        frappe.get_doc_hooks = MagicMock(return_value="not-a-dict")

        with patch("frappe.get_all_apps", return_value=["frappe"]):
            MicroserviceApp("test-service", load_framework_hooks=["frappe"])

        result = frappe.get_doc_hooks()
        assert result == {}


class TestUpdateDocValidation:
    """update_doc must reject None/invalid data arguments."""

    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)

    @patch("frappe.get_doc", create=True)
    def test_update_doc_none_data_raises(self, mock_get_doc, db):
        mock_doc = MagicMock()
        mock_doc.tenant_id = "test_tenant"
        mock_get_doc.return_value = mock_doc
        with pytest.raises(ValueError, match="non-empty dict"):
            db.update_doc("Sales Order", "SO-001", None)

    @patch("frappe.get_doc", create=True)
    def test_update_doc_empty_dict_raises(self, mock_get_doc, db):
        mock_doc = MagicMock()
        mock_doc.tenant_id = "test_tenant"
        mock_get_doc.return_value = mock_doc
        with pytest.raises(ValueError, match="non-empty dict"):
            db.update_doc("Sales Order", "SO-001", {})

    @patch("frappe.get_doc", create=True)
    def test_update_doc_non_dict_raises(self, mock_get_doc, db):
        mock_doc = MagicMock()
        mock_doc.tenant_id = "test_tenant"
        mock_get_doc.return_value = mock_doc
        with pytest.raises(ValueError, match="non-empty dict"):
            db.update_doc("Sales Order", "SO-001", "bad")

    @patch("frappe.get_doc", create=True)
    def test_update_doc_passes_data_without_tenant_id_to_update(self, mock_get_doc, db):
        """tenant_id must not be forwarded to doc.update (handled earlier in method)."""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "test_tenant"
        mock_doc.update = MagicMock()
        mock_get_doc.return_value = mock_doc
        db.update_doc(
            "Sales Order",
            "SO-001",
            {"status": "Completed", "tenant_id": "must-strip"},
        )
        mock_doc.update.assert_called_once_with({"status": "Completed"})


class TestCommitRollbackGuard:
    """commit/rollback must not crash when frappe.db is None."""

    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)

    def test_commit_when_frappe_db_none(self, db):
        original_db = frappe.db
        try:
            frappe.db = None
            result = db.commit()
            assert result is None
        finally:
            frappe.db = original_db

    def test_rollback_when_frappe_db_none(self, db):
        original_db = frappe.db
        try:
            frappe.db = None
            result = db.rollback()
            assert result is None
        finally:
            frappe.db = original_db

    def test_commit_when_frappe_db_exists(self, db):
        frappe.db.commit.return_value = True
        db.commit()
        frappe.db.commit.assert_called_once()

    def test_rollback_when_frappe_db_exists(self, db):
        frappe.db.rollback.return_value = True
        db.rollback()
        frappe.db.rollback.assert_called_once()


class TestGetUserTenantIdEdgeCases:
    """Edge cases in get_user_tenant_id."""

    def test_no_session_resolves_to_guest(self):
        """When frappe.session is None, defaults to Guest and returns None."""
        from frappe_microservice.core import get_user_tenant_id

        original_session = frappe.session
        try:
            frappe.session = None
            result = get_user_tenant_id()
            assert result is None
        finally:
            frappe.session = original_session

    def test_session_without_user_attr(self):
        """When frappe.session exists but has no user, defaults to Guest."""
        from frappe_microservice.core import get_user_tenant_id

        original_session = frappe.session
        try:
            frappe.session = type("FakeSession", (), {})()
            result = get_user_tenant_id()
            assert result is None
        finally:
            frappe.session = original_session


class TestPatchValidDictForTenantId:
    """Tests for the ORM-level get_valid_dict patch that injects tenant_id."""

    def setup_method(self):
        from frappe.model.base_document import BaseDocument

        self._original = getattr(BaseDocument, "_tenant_valid_dict_patched", False)

    def teardown_method(self):
        from frappe.model.base_document import BaseDocument

        BaseDocument._tenant_valid_dict_patched = self._original

    def test_patch_is_idempotent(self):
        """Calling patch_valid_dict_for_tenant_id twice must not stack wrappers."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id

        BaseDocument._tenant_valid_dict_patched = False
        patch_valid_dict_for_tenant_id()
        first_fn = BaseDocument.get_valid_dict
        patch_valid_dict_for_tenant_id()
        assert BaseDocument.get_valid_dict is first_fn

    def test_patch_adds_tenant_id_to_valid_dict(self):
        """After patching, get_valid_dict must include tenant_id when set on doc."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict
        BaseDocument.get_valid_dict = lambda self, *a, **kw: {
            "name": "DOC-001",
            "status": "Draft",
        }

        try:
            patch_valid_dict_for_tenant_id()

            doc = MagicMock()
            doc.tenant_id = "tenant-abc"
            result = BaseDocument.get_valid_dict(doc)
            assert result["tenant_id"] == "tenant-abc"
            assert result["name"] == "DOC-001"
        finally:
            BaseDocument.get_valid_dict = original_fn

    def test_patch_does_not_add_tenant_id_when_absent(self):
        """When doc has no tenant_id, get_valid_dict is unchanged."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict
        BaseDocument.get_valid_dict = lambda self, *a, **kw: {"name": "DOC-001"}

        try:
            patch_valid_dict_for_tenant_id()

            doc = MagicMock()
            doc.tenant_id = None
            result = BaseDocument.get_valid_dict(doc)
            assert "tenant_id" not in result
        finally:
            BaseDocument.get_valid_dict = original_fn

    def test_patch_uses_authenticated_request_tenant_for_standalone_side_effects(
        self,
    ):
        """ERPNext-created standalone rows inherit the secure request tenant."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict
        BaseDocument.get_valid_dict = lambda self, *a, **kw: {
            "name": "GL-REVERSAL-001",
        }

        try:
            patch_valid_dict_for_tenant_id(lambda: "tenant-request")

            standalone = type("StandaloneRow", (), {})()
            standalone.tenant_id = "SYSTEM"
            standalone.parent = None
            standalone.parenttype = None

            result = BaseDocument.get_valid_dict(standalone)

            assert result["tenant_id"] == "tenant-request"
            assert standalone.tenant_id == "tenant-request"
        finally:
            BaseDocument.get_valid_dict = original_fn

    def test_patch_rejects_cross_tenant_standalone_write(self):
        """A request may not silently take ownership of another tenant's row."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import (
            TenantAccessError,
            patch_valid_dict_for_tenant_id,
        )

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict
        BaseDocument.get_valid_dict = lambda self, *a, **kw: {
            "name": "GL-FOREIGN-001",
        }

        try:
            patch_valid_dict_for_tenant_id(lambda: "tenant-request")

            standalone = type("StandaloneRow", (), {})()
            standalone.tenant_id = "tenant-foreign"
            standalone.parent = None
            standalone.parenttype = None

            with pytest.raises(TenantAccessError, match="does not belong"):
                BaseDocument.get_valid_dict(standalone)
        finally:
            BaseDocument.get_valid_dict = original_fn

    @patch("frappe.db.get_value", create=True)
    def test_patch_uses_parent_tenant_for_child_rows(self, mock_get_value):
        """A child row must inherit its persisted parent's authoritative tenant."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict
        BaseDocument.get_valid_dict = lambda self, *a, **kw: {
            "name": "PAYMENT-REFERENCE-001",
        }
        mock_get_value.return_value = "tenant-parent"

        try:
            patch_valid_dict_for_tenant_id()

            child = type("ChildRow", (), {})()
            child.tenant_id = "SYSTEM"
            child.parent = "ACC-PAY-2026-00001"
            child.parenttype = "Payment Entry"

            result = BaseDocument.get_valid_dict(child)

            assert result["tenant_id"] == "tenant-parent"
            assert child.tenant_id == "tenant-parent"
            mock_get_value.assert_called_once_with(
                "Payment Entry", "ACC-PAY-2026-00001", "tenant_id"
            )
        finally:
            BaseDocument.get_valid_dict = original_fn

    def _make_row(self, doctype, issingle=None):
        row = type("Row", (), {})()
        row.doctype = doctype
        row.tenant_id = "tenant-abc"
        row.parent = None
        row.parenttype = None
        if issingle is not None:
            row.meta = type("Meta", (), {"issingle": issingle})()
        return row

    def test_patch_skips_global_schema_doctypes(self):
        """Shared schema/config must never be split across tenants."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict
        BaseDocument.get_valid_dict = lambda self, *a, **kw: {"name": "ROW-001"}

        try:
            patch_valid_dict_for_tenant_id()

            # has the column, but is shared schema -> still not stamped
            skipped = BaseDocument.get_valid_dict(self._make_row("Custom Field"))
            assert "tenant_id" not in skipped

            stamped = BaseDocument.get_valid_dict(self._make_row("Purchase Invoice"))
            assert stamped["tenant_id"] == "tenant-abc"
        finally:
            BaseDocument.get_valid_dict = original_fn

    def test_patch_skips_single_doctypes(self):
        """Singles are excluded from the tenant_id ALTER (issingle: 0 filter).

        Document.update_single DELETEs every tabSingles row for the doctype and
        re-INSERTs one per valid-dict key, so a stamped single writes a real
        tenant_id row that the next tenant to save it overwrites.
        """
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict
        BaseDocument.get_valid_dict = lambda self, *a, **kw: {"name": "ROW-001"}

        try:
            patch_valid_dict_for_tenant_id()

            single = BaseDocument.get_valid_dict(
                self._make_row("Buying Settings", issingle=1)
            )
            assert "tenant_id" not in single

            regular = BaseDocument.get_valid_dict(
                self._make_row("Purchase Invoice", issingle=0)
            )
            assert regular["tenant_id"] == "tenant-abc"
        finally:
            BaseDocument.get_valid_dict = original_fn

    def test_patch_not_installed_when_multi_tenant_off(self):
        """MULTI_TENANT=off must leave the write path completely untouched."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict

        def unpatched(self, *a, **kw):
            return {"name": "ROW-001"}

        BaseDocument.get_valid_dict = unpatched

        try:
            patch_valid_dict_for_tenant_id(lambda: "tenant-abc", multi_tenant=False)

            assert BaseDocument.get_valid_dict is unpatched
            assert not getattr(BaseDocument, "_tenant_valid_dict_patched", False)
            result = BaseDocument.get_valid_dict(self._make_row("Purchase Invoice"))
            assert "tenant_id" not in result
        finally:
            BaseDocument.get_valid_dict = original_fn
            BaseDocument._tenant_valid_dict_patched = False

    @patch("frappe.db.get_value", create=True)
    def test_child_tenant_comes_from_parent_doc_without_a_query(self, mock_get_value):
        """Frappe's in-memory parent_doc replaces the per-child-row SELECT."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict
        BaseDocument.get_valid_dict = lambda self, *a, **kw: {"name": "ROW-001"}

        parent_doc = type("Parent", (), {})()
        parent_doc.tenant_id = "tenant-abc"

        def child_row():
            row = type("Row", (), {})()
            row.doctype = "Purchase Invoice Item"
            row.tenant_id = "SYSTEM"
            row.parent = "PINV-0001"
            row.parenttype = "Purchase Invoice"
            row.parent_doc = parent_doc
            return row

        try:
            patch_valid_dict_for_tenant_id()

            for _ in range(5):
                result = BaseDocument.get_valid_dict(child_row())
                assert result["tenant_id"] == "tenant-abc"

            mock_get_value.assert_not_called()
        finally:
            BaseDocument.get_valid_dict = original_fn

    @patch("frappe.db.get_value", create=True)
    def test_child_falls_back_to_query_without_a_live_parent(self, mock_get_value):
        """No parent_doc (weakref collected, standalone child) -> one query."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict
        BaseDocument.get_valid_dict = lambda self, *a, **kw: {"name": "ROW-001"}
        mock_get_value.return_value = "tenant-abc"

        row = type("Row", (), {})()
        row.doctype = "Purchase Invoice Item"
        row.tenant_id = None
        row.parent = "PINV-0001"
        row.parenttype = "Purchase Invoice"
        row.parent_doc = None

        try:
            patch_valid_dict_for_tenant_id()

            result = BaseDocument.get_valid_dict(row)

            assert result["tenant_id"] == "tenant-abc"
            mock_get_value.assert_called_once_with(
                "Purchase Invoice", "PINV-0001", "tenant_id"
            )
        finally:
            BaseDocument.get_valid_dict = original_fn

    @patch("frappe.db.get_value", create=True)
    def test_cross_tenant_parent_still_rejected_from_memory(self, mock_get_value):
        """The security check survives the switch off the DB read."""
        from frappe.model.base_document import BaseDocument
        from frappe_microservice.tenant import (
            TenantAccessError,
            patch_valid_dict_for_tenant_id,
        )

        BaseDocument._tenant_valid_dict_patched = False
        original_fn = BaseDocument.get_valid_dict
        BaseDocument.get_valid_dict = lambda self, *a, **kw: {"name": "ROW-001"}

        parent_doc = type("Parent", (), {})()
        parent_doc.tenant_id = "tenant-other"

        row = type("Row", (), {})()
        row.doctype = "Purchase Invoice Item"
        row.tenant_id = None
        row.parent = "PINV-0001"
        row.parenttype = "Purchase Invoice"
        row.parent_doc = parent_doc

        try:
            patch_valid_dict_for_tenant_id(lambda: "tenant-abc")

            with pytest.raises(TenantAccessError):
                BaseDocument.get_valid_dict(row)

            mock_get_value.assert_not_called()
        finally:
            BaseDocument.get_valid_dict = original_fn


class TestUpdateDocDocumentModel:
    """Tests using FakeDocument instead of MagicMock.

    These verify that update_doc produces real Document-like child rows,
    not plain dicts.  The old setattr-based code would fail these tests
    with ``AttributeError: 'dict' object has no attribute 'is_new'``.
    """

    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)

    def _make_doc(self, data, table_fieldnames=()):
        try:
            from tests.conftest import FakeDocument
        except ModuleNotFoundError:
            from conftest import FakeDocument
        data.setdefault("doctype", "Purchase Invoice")
        return FakeDocument(data, table_fieldnames=table_fieldnames)

    def test_update_doc_converts_child_table_dicts(self, db):
        """Child table dicts must become FakeChildDocument after update_doc."""
        try:
            from tests.conftest import FakeChildDocument
        except ModuleNotFoundError:
            from conftest import FakeChildDocument

        doc = self._make_doc(
            {
                "name": "INV-001",
                "tenant_id": "test_tenant",
                "items": [],
                "grand_total": 0,
            },
            table_fieldnames=("items",),
        )
        frappe.get_doc.return_value = doc

        result = db.update_doc(
            "Purchase Invoice",
            "INV-001",
            {
                "grand_total": 1000,
                "items": [{"item_code": "ITEM-001", "qty": 5, "rate": 200}],
            },
        )

        assert result.grand_total == 1000
        assert len(result.items) == 1
        assert isinstance(result.items[0], FakeChildDocument)
        assert result.items[0].item_code == "ITEM-001"
        assert result.items[0].qty == 5
        assert result._saved is True

    def test_update_doc_multiple_child_tables(self, db):
        """update_doc must handle multiple child tables correctly."""
        try:
            from tests.conftest import FakeChildDocument
        except ModuleNotFoundError:
            from conftest import FakeChildDocument

        doc = self._make_doc(
            {"name": "INV-002", "tenant_id": "test_tenant", "items": [], "taxes": []},
            table_fieldnames=("items", "taxes"),
        )
        frappe.get_doc.return_value = doc

        result = db.update_doc(
            "Purchase Invoice",
            "INV-002",
            {
                "items": [
                    {"item_code": "A", "qty": 1},
                    {"item_code": "B", "qty": 2},
                ],
                "taxes": [
                    {"charge_type": "On Net Total", "rate": 18},
                ],
            },
        )

        assert all(isinstance(r, FakeChildDocument) for r in result.items)
        assert all(isinstance(r, FakeChildDocument) for r in result.taxes)
        assert result.items[1].item_code == "B"
        assert result.taxes[0].rate == 18
        assert result._saved is True

    def test_update_doc_scalar_only(self, db):
        """Scalar-only updates must work without child table fields."""
        doc = self._make_doc(
            {"name": "INV-003", "tenant_id": "test_tenant", "status": "Draft"},
        )
        frappe.get_doc.return_value = doc

        result = db.update_doc("Purchase Invoice", "INV-003", {"status": "Submitted"})

        assert result.status == "Submitted"
        assert result._saved is True

    def test_setattr_approach_crashes_with_child_tables(self):
        """Proves the old setattr loop would crash — the exact regression we fixed."""
        doc = self._make_doc(
            {"name": "INV-004", "tenant_id": "test_tenant", "items": []},
            table_fieldnames=("items",),
        )

        # Simulate the OLD broken code: raw setattr skips child conversion
        for key, value in {"items": [{"item_code": "X", "qty": 1}]}.items():
            object.__setattr__(doc, key, value)

        assert isinstance(doc.items[0], dict)  # still a dict!

        with pytest.raises(AttributeError, match="is_new"):
            doc.save()

    def test_doc_update_then_save_succeeds(self):
        """doc.update() converts child dicts so save() succeeds."""
        try:
            from tests.conftest import FakeChildDocument
        except ModuleNotFoundError:
            from conftest import FakeChildDocument

        doc = self._make_doc(
            {"name": "INV-005", "tenant_id": "test_tenant", "items": []},
            table_fieldnames=("items",),
        )

        doc.update({"items": [{"item_code": "Y", "qty": 3}]})

        assert isinstance(doc.items[0], FakeChildDocument)
        doc.save()  # must not raise
        assert doc._saved is True

    def test_update_doc_child_rows_support_is_new(self, db):
        """Every child row returned by update_doc must have a working is_new()."""
        doc = self._make_doc(
            {"name": "INV-006", "tenant_id": "test_tenant", "items": []},
            table_fieldnames=("items",),
        )
        frappe.get_doc.return_value = doc

        result = db.update_doc(
            "Purchase Invoice",
            "INV-006",
            {
                "items": [{"item_code": "Z", "qty": 10}],
            },
        )

        for row in result.items:
            assert hasattr(row, "is_new")
            assert callable(row.is_new)
            assert row.is_new() is True


class TestFindByLabel:
    """Public replacement for services reaching into naming._claimable_row.

    Master rows are tenant-scoped by docname, so a caller holding a user-facing
    label cannot look them up by name — and may not know which field carries the
    label (an Item resolves by item_code OR item_name). That need previously
    forced expense-service to import a private library internal.
    """

    @pytest.fixture
    def db(self):
        return TenantAwareDB(MagicMock(return_value="tid-1"))

    def test_finds_by_first_matching_field(self, db):
        db.get_all = MagicMock(return_value=[{"name": "tid1-travel"}])
        assert db.find_by_label("Item", "Travel", ["item_code"]) == "tid1-travel"

    def test_falls_through_to_later_fields(self, db):
        def get_all(doctype, filters=None, **kwargs):
            # Only item_name carries the label for this row.
            return [{"name": "tid1-travel"}] if "item_name" in (filters or {}) else []

        db.get_all = MagicMock(side_effect=get_all)
        assert (
            db.find_by_label("Item", "Travel", ["item_code", "item_name"])
            == "tid1-travel"
        )

    def test_returns_none_when_this_tenant_has_no_row(self, db):
        db.get_all = MagicMock(return_value=[])
        assert db.find_by_label("Item", "Travel", ["item_code", "item_name"]) is None

    def test_lookup_goes_through_tenant_filtered_get_all(self, db):
        """The isolation guarantee: never a raw frappe query.

        get_all applies _add_tenant_filter, which raises on a missing or SYSTEM
        tenant — so this can never surface another tenant's row or an unowned one.
        """
        db.get_all = MagicMock(return_value=[])
        db.find_by_label("Item", "Travel", ["item_code"])
        assert db.get_all.called

    def test_empty_label_short_circuits(self, db):
        db.get_all = MagicMock(return_value=[{"name": "should-not-be-reached"}])
        assert db.find_by_label("Item", "", ["item_code"]) is None
        assert not db.get_all.called

    def test_accepts_a_bare_string_field(self, db):
        db.get_all = MagicMock(return_value=[{"name": "tid1-travel"}])
        assert db.find_by_label("Item", "Travel", "item_code") == "tid1-travel"


class TestTenantAwareDBImplicitMasterAutonaming:
    """Test implicit tenant-scoped autonaming and resolution for master doctypes on insert_doc"""

    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="e373311e-84b2-4d1a-9f5e-123456789abc")
        return TenantAwareDB(get_tenant_id)

    # monkeypatch, not @patch: earlier tests in this module rebind ``frappe.db``
    # itself, so a decorator-installed patch can be silently discarded when their
    # context exits. monkeypatch resolves the attribute at call time and restores
    # whatever was actually there.
    TENANT = "e373311e-84b2-4d1a-9f5e-123456789abc"

    def _stub_frappe(self, monkeypatch, returned_doc, exists=False):
        """Patch the ``frappe`` each module actually holds.

        tenant.py and naming.py bind ``frappe`` at import time, which happens
        before conftest swaps sys.modules["frappe"]. Patching a bare
        ``import frappe`` here would therefore target a different object than
        the code under test uses, and the stub would silently never apply.
        """
        from frappe_microservice import naming as naming_mod
        from frappe_microservice import tenant as tenant_mod

        get_doc = MagicMock(return_value=returned_doc)
        for mod in (tenant_mod, naming_mod):
            frappe_ref = mod.frappe
            monkeypatch.setattr(frappe_ref, "get_doc", get_doc, raising=False)
            monkeypatch.setattr(
                frappe_ref.db,
                "get_value",
                MagicMock(return_value=self.TENANT),
                raising=False,
            )
            monkeypatch.setattr(
                frappe_ref.db, "exists", MagicMock(return_value=exists), raising=False
            )
        return get_doc

    def test_insert_doc_master_doctype_implicit_tenant_scoped_set_name(
        self, db, monkeypatch
    ):
        doc_mock = MagicMock()
        doc_mock.name = "e373311e-travel"
        doc_mock.tenant_id = self.TENANT
        self._stub_frappe(monkeypatch, doc_mock)

        with patch("frappe_microservice.naming._claimable_row", return_value=None):
            db.insert_doc("Item", {"item_code": "Travel", "item_name": "Travel"})

        assert doc_mock.insert.called
        kwargs = doc_mock.insert.call_args[1]
        # The docname must ride on set_name — a "name" key in the values dict is
        # discarded by Frappe's set_new_name() for field-autonamed doctypes.
        assert kwargs.get("set_name") == "e373311e-travel"

    def test_insert_doc_master_doctype_implicit_reuses_existing(self, db, monkeypatch):
        existing_doc = MagicMock()
        existing_doc.name = "e373311e-acme"
        existing_doc.tenant_id = self.TENANT
        get_doc = self._stub_frappe(monkeypatch, existing_doc)

        with patch(
            "frappe_microservice.naming._claimable_row", return_value="e373311e-acme"
        ):
            res = db.insert_doc("Supplier", {"supplier_name": "Acme"})

        assert res == existing_doc
        get_doc.assert_called_with("Supplier", "e373311e-acme")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=frappe_microservice.core", "--cov-report=html"])
