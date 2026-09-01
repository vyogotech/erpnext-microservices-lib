"""
Unit tests for tenant strictness + cross-tenant enumeration masking.
"""

import logging
import uuid

import pytest
from unittest.mock import MagicMock, patch
import frappe
from flask import g
from frappe_microservice.core import MicroserviceApp
from frappe_microservice.tenant import TenantAwareDB, TenantAccessError


TENANT = "tenant-1"


class _DocNoTenant:
    """A fetched doc whose table has no tenant_id column."""
    doctype = "Widget"

    def has_permission(self, ptype):
        return True


class _DocWithTenant:
    def __init__(self, tenant):
        self.doctype = "Widget"
        self.tenant_id = tenant

    def has_permission(self, ptype):
        return True


def _db(tenant_strict):
    # permission_mode off to isolate tenant_strict behavior.
    return TenantAwareDB(lambda: TENANT, permission_mode="off",
                         tenant_strict=tenant_strict)


# --- Gap A: missing tenant_id column --------------------------------------

def test_missing_column_off_passes():
    frappe.get_doc = MagicMock(return_value=_DocNoTenant())
    assert isinstance(_db("off").get_doc("Widget", "W-1"), _DocNoTenant)


def test_missing_column_warn_logs_and_passes(caplog):
    frappe.get_doc = MagicMock(return_value=_DocNoTenant())
    with caplog.at_level(logging.WARNING):
        _db("warn").get_doc("Widget", "W-1")
    assert any("would-deny] tenant isolation unavailable" in r.getMessage()
               for r in caplog.records)


def test_missing_column_enforce_raises():
    frappe.get_doc = MagicMock(return_value=_DocNoTenant())
    with pytest.raises(frappe.PermissionError):
        _db("enforce").get_doc("Widget", "W-1")


# --- tenant matching / mismatch -------------------------------------------

def test_tenant_match_returns_all_modes():
    frappe.get_doc = MagicMock(return_value=_DocWithTenant(TENANT))
    for mode in ("off", "warn", "enforce"):
        assert _db(mode).get_doc("Widget", "W-1").tenant_id == TENANT


def test_tenant_mismatch_raises_tenant_access_error_all_modes():
    frappe.get_doc = MagicMock(return_value=_DocWithTenant("other"))
    for mode in ("off", "warn", "enforce"):
        with pytest.raises(TenantAccessError):
            _db(mode).get_doc("Widget", "W-1")


def test_tenant_access_error_is_permission_error():
    assert issubclass(TenantAccessError, frappe.PermissionError)


# --- secure_route masking --------------------------------------------------

def _app(mask):
    with patch('frappe.init'), patch('frappe.connect'):
        return MicroserviceApp("test-service", mask_permission_errors=mask)


def _invoke_raising(app, exc):
    @app.secure_route('/x', methods=['GET'], endpoint='x')
    def x(user):
        raise exc

    with app.flask_app.test_request_context('/x'):
        with patch.object(app, '_validate_session', return_value=('u@e.com', None)):
            with patch('frappe_microservice.app.get_user_tenant_id', return_value='t1'):
                with patch('frappe.db.rollback'):
                    g.request_id = str(uuid.uuid4())
                    g._frappe_rolled_back = False
                    result = x()
    return result


def test_mask_on_tenant_error_becomes_404():
    resp, status = _invoke_raising(_app(True), TenantAccessError("cross tenant"))
    assert status == 404
    assert resp.json['type'] == 'DoesNotExistError'


def test_mask_off_tenant_error_stays_403():
    resp, status = _invoke_raising(_app(False), TenantAccessError("cross tenant"))
    assert status == 403
    assert resp.json['type'] == 'PermissionError'


def test_mask_on_role_permission_error_stays_403():
    # plain PermissionError (role denial) is NOT masked, even with masking on.
    resp, status = _invoke_raising(_app(True), frappe.PermissionError("no read perm"))
    assert status == 403
    assert resp.json['type'] == 'PermissionError'


# --- resource handler: byte-equal 404 when masking -------------------------

def test_resource_masking_byte_equal_to_missing():
    app = _app(True)
    app.register_resource("Widget", methods=["GET"])

    def _run(exc):
        with patch.object(app, '_restore_frappe_local'), \
                patch.object(app, '_validate_session', return_value=('u@e.com', None)), \
                patch('frappe_microservice.app.get_user_tenant_id', return_value='t1'), \
                patch.object(app.tenant_db, 'get_doc', side_effect=exc):
            return app.flask_app.test_client().get('/api/resource/Widget/W-1')

    r_tenant = _run(TenantAccessError("cross tenant"))
    r_missing = _run(frappe.DoesNotExistError("missing"))
    assert r_tenant.status_code == 404
    assert r_missing.status_code == 404
    assert r_tenant.get_data() == r_missing.get_data()


# --- startup column diagnostic --------------------------------------------

def test_startup_column_check_warns_for_missing_tenant_id(caplog):
    app = _app(False)
    app._registered_resources = {"Widget"}
    frappe.db.table_exists = MagicMock(return_value=True)
    frappe.db.has_column = MagicMock(return_value=False)
    with caplog.at_level(logging.WARNING):
        app._check_registered_resource_tenant_columns()
    assert any("no tenant_id column" in r.getMessage() for r in caplog.records)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
