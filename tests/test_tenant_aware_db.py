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


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--cov=frappe_microservice.core', '--cov-report=html'])
