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

import pytest
from unittest.mock import MagicMock, patch, call
import frappe
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
            "Sales Order", 
            filters={"name": "SO-001", "tenant_id": "test_tenant"}
        )
    
    @patch("frappe.get_all", create=True)
    def test_get_all_with_fields(self, mock_get_all, db):
        """Test get_all with fields parameter"""
        db.get_all("Sales Order", filters={"status": "Draft"}, fields=["name", "customer"])
        
        call_args = mock_get_all.call_args
        assert call_args[1]['filters']['tenant_id'] == "test_tenant"
        assert call_args[1]['fields'] == ["name", "customer"]
    
    @patch("frappe.db.get_value", create=True)
    def test_get_value_with_filters(self, mock_get_value, db):
        """Test get_value adds tenant filter"""
        mock_get_value.return_value = "CUST-001"
        
        result = db.get_value("Sales Order", "SO-001", "customer")
        
        # Verify tenant_id was added
        call_args = mock_get_value.call_args
        assert 'tenant_id' in str(call_args)
    
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
        
        with pytest.raises(frappe.PermissionError, match="does not belong to current tenant"):
            db.get_doc("Sales Order", "SO-001")


class TestTenantAwareDBInsert:
    """Test insert operations with all branches"""
    
    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)
    
    @patch('frappe.get_doc', create=True)
    @patch('frappe.db.get_value', create=True)
    def test_insert_doc_basic(self, mock_get_value, mock_get_doc, db):
        """Test basic document insertion with tenant_id"""
        mock_doc = MagicMock()
        mock_doc.name = "DOC-001"
        mock_doc.tenant_id = "test_tenant"
        mock_get_doc.return_value = mock_doc
        mock_get_value.return_value = "test_tenant"
        
        result = db.insert_doc('Sales Order', {'customer': 'CUST-001'})
        
        # Verify tenant_id was set
        call_args = mock_get_doc.call_args[0][0]
        assert call_args['tenant_id'] == 'test_tenant'
        
        # Verify insert was called
        mock_doc.insert.assert_called_once()
        
        # Verify post-insert verification
        mock_get_value.assert_called_once()
    
    @patch('frappe.get_doc', create=True)
    @patch('frappe.db.get_value', create=True)
    def test_insert_doc_verification_disabled(self, mock_get_value, mock_get_doc):
        """Test insert with verification disabled (performance mode)"""
        get_tenant_id = MagicMock(return_value="test_tenant")
        db = TenantAwareDB(get_tenant_id, verify_tenant_on_insert=False)
        
        mock_doc = MagicMock()
        mock_doc.name = "DOC-001"
        mock_get_doc.return_value = mock_doc
        
        db.insert_doc('Sales Order', {'customer': 'CUST-001'})
        
        # Verification should be skipped
        mock_get_value.assert_not_called()
    
    @patch('frappe.get_doc', create=True)
    @patch('frappe.db.get_value', create=True)
    def test_insert_doc_verification_mismatch(self, mock_get_value, mock_get_doc, db):
        """Test error when saved tenant_id doesn't match"""
        mock_doc = MagicMock()
        mock_doc.name = "DOC-001"
        mock_get_doc.return_value = mock_doc
        mock_get_value.return_value = "wrong_tenant"  # Mismatch!
        
        with pytest.raises(ValueError, match="tenant_id mismatch"):
            db.insert_doc('Sales Order', {'customer': 'CUST-001'})
    
    @patch('frappe.get_doc', create=True)
    def test_insert_doc_no_tenant_id(self, mock_get_doc):
        """Test error when no tenant_id in context"""
        get_tenant_id = MagicMock(return_value=None)
        db = TenantAwareDB(get_tenant_id)
        
        with pytest.raises(ValueError, match="No tenant_id in context"):
            db.insert_doc('Sales Order', {'customer': 'CUST-001'})
    
    @patch('frappe.get_doc', create=True)
    def test_insert_doc_tenant_override_blocked(self, mock_get_doc, db):
        """Test error when trying to override tenant_id"""
        with pytest.raises(frappe.PermissionError, match="Cannot create document for different tenant"):
            db.insert_doc('Sales Order', {'customer': 'CUST-001', 'tenant_id': 'other_tenant'})


class TestTenantAwareDBUpdate:
    """Test update operations"""
    
    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)
    
    @patch('frappe.get_doc', create=True)
    def test_update_doc_basic(self, mock_get_doc, db):
        """Test basic document update with tenant verification"""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "test_tenant"
        mock_get_doc.return_value = mock_doc
        
        result = db.update_doc('Sales Order', 'SO-001', {'status': 'Completed'})
        
        # Verify fields were updated
        assert mock_doc.status == 'Completed'
        
        # Verify save was called
        mock_doc.save.assert_called_once()
    
    @patch('frappe.get_doc', create=True)
    def test_update_doc_wrong_tenant(self, mock_get_doc, db):
        """Test error when updating document from different tenant"""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "different_tenant"
        mock_get_doc.return_value = mock_doc
        
        with pytest.raises(frappe.PermissionError, match="does not belong to current tenant"):
            db.update_doc('Sales Order', 'SO-001', {'status': 'Completed'})


class TestTenantAwareDBDelete:
    """Test delete operations"""
    
    @pytest.fixture
    def db(self):
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)
    
    @patch('frappe.get_doc', create=True)
    def test_delete_doc_basic(self, mock_get_doc, db):
        """Test basic document deletion with tenant verification"""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "test_tenant"
        mock_get_doc.return_value = mock_doc
        
        db.delete_doc('Sales Order', 'SO-001')
        
        # Verify delete was called
        mock_doc.delete.assert_called_once()
    
    @patch('frappe.get_doc', create=True)
    def test_delete_doc_wrong_tenant(self, mock_get_doc, db):
        """Test error when deleting document from different tenant"""
        mock_doc = MagicMock()
        mock_doc.tenant_id = "different_tenant"
        mock_get_doc.return_value = mock_doc
        
        with pytest.raises(frappe.PermissionError, match="does not belong to current tenant"):
            db.delete_doc('Sales Order', 'SO-001')


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
            with pytest.raises(frappe.PermissionError, match="does not belong to current tenant"):
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
            filters = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("filters")
            assert isinstance(filters, dict)
            assert filters["tenant_id"] == "test_tenant"
            assert filters["name"] == "SO-001"


class TestGetDocHooksErrorHandling:
    """Tests that microservice_get_doc_hooks handles failures gracefully."""

    def test_get_doc_hooks_returns_empty_on_exception(self):
        """If original_get_doc_hooks raises, return {} instead of crashing."""
        from frappe_microservice.core import MicroserviceApp
        from tests.test_microservice_app import _reset_microservice_guards

        _reset_microservice_guards()

        def exploding_get_doc_hooks():
            raise RuntimeError("hooks DB table missing")

        frappe.get_doc_hooks = exploding_get_doc_hooks
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp("test-service", load_framework_hooks=["frappe"])

        result = frappe.get_doc_hooks()
        assert result == {}

    def test_get_doc_hooks_handles_non_dict_return(self):
        """If original returns non-dict, return {} safely."""
        from frappe_microservice.core import MicroserviceApp
        from tests.test_microservice_app import _reset_microservice_guards

        _reset_microservice_guards()
        frappe.get_doc_hooks = MagicMock(return_value="not-a-dict")

        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp("test-service", load_framework_hooks=["frappe"])

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
        result = db.commit()
        frappe.db.commit.assert_called_once()

    def test_rollback_when_frappe_db_exists(self, db):
        frappe.db.rollback.return_value = True
        result = db.rollback()
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
            frappe.session = type('FakeSession', (), {})()
            result = get_user_tenant_id()
            assert result is None
        finally:
            frappe.session = original_session


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--cov=frappe_microservice.core', '--cov-report=html'])
