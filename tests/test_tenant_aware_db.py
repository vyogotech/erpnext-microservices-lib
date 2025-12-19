import pytest
from unittest.mock import MagicMock, patch
from frappe_microservice.core import TenantAwareDB

class TestTenantAwareDB:
    @pytest.fixture
    def db(self):
        # Mock the get_tenant_id function
        get_tenant_id = MagicMock(return_value="test_tenant")
        return TenantAwareDB(get_tenant_id)

    def test_add_tenant_filter_dict(self, db):
        filters = {"status": "Active"}
        result = db._add_tenant_filter(filters)
        assert result == {"status": "Active", "tenant_id": "test_tenant"}
        # Ensure original filter is not modified (immutability check)
        assert filters == {"status": "Active"}

    def test_add_tenant_filter_list(self, db):
        filters = [["status", "=", "Active"]]
        result = db._add_tenant_filter(filters)
        assert result == [["status", "=", "Active"], ["tenant_id", "=", "test_tenant"]]
        # Ensure original filter is not modified
        assert filters == [["status", "=", "Active"]]

    def test_add_tenant_filter_none(self, db):
        result = db._add_tenant_filter(None)
        assert result == {"tenant_id": "test_tenant"}

    def test_add_tenant_filter_no_tenant(self, db):
        db.get_tenant_id = MagicMock(return_value=None)
        with pytest.raises(ValueError, match="No tenant_id found in context"):
            db._add_tenant_filter({"status": "Active"})

    @patch("frappe.get_all", create=True)
    def test_get_all_calls_frappe(self, mock_get_all, db):
        db.get_all("Sales Order", filters={"name": "SO-001"})
        mock_get_all.assert_called_once_with(
            "Sales Order", 
            filters={"name": "SO-001", "tenant_id": "test_tenant"}
        )
