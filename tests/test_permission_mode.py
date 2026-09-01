"""
Unit tests for read-path / set_value authorization.

``frappe`` is mocked (tests/conftest.py). ``TenantAwareDB`` is constructed
directly with a fixed tenant so we can drive ``frappe.has_permission`` /
``doc.has_permission`` return values across off/warn/enforce.
"""

import logging

import pytest
from unittest.mock import MagicMock, patch
import frappe
from flask import g
from frappe_microservice.core import MicroserviceApp
from frappe_microservice.tenant import TenantAwareDB
from frappe_microservice.policy import resolve_mode


TENANT = "tenant-1"


def _db(mode):
    return TenantAwareDB(lambda: TENANT, permission_mode=mode)


# --- resolve_mode ----------------------------------------------------------

def test_resolve_mode_kwarg_beats_env(monkeypatch):
    monkeypatch.setenv("PERMISSION_MODE", "enforce")
    assert resolve_mode("off", "PERMISSION_MODE", "warn") == "off"


def test_resolve_mode_env_beats_default(monkeypatch):
    monkeypatch.setenv("PERMISSION_MODE", "enforce")
    assert resolve_mode(None, "PERMISSION_MODE", "warn") == "enforce"


def test_resolve_mode_default(monkeypatch):
    monkeypatch.delenv("PERMISSION_MODE", raising=False)
    assert resolve_mode(None, "PERMISSION_MODE", "warn") == "warn"


def test_resolve_mode_unknown_warns(monkeypatch, caplog):
    monkeypatch.delenv("PERMISSION_MODE", raising=False)
    with caplog.at_level(logging.WARNING):
        assert resolve_mode("bogus", "PERMISSION_MODE", "warn") == "warn"
    assert any("Unknown mode" in r.getMessage() for r in caplog.records)


# --- read methods matrix (doctype-level) ----------------------------------

READ_METHODS = ["get_all", "get_value", "exists", "count"]


def _call_read(db, method):
    if method == "get_all":
        return db.get_all("Widget")
    if method == "get_value":
        return db.get_value("Widget", "W-1", "field")
    if method == "exists":
        return db.exists("Widget", {"name": "W-1"})
    if method == "count":
        return db.count("Widget")
    raise AssertionError(method)


@pytest.mark.parametrize("method", READ_METHODS)
def test_read_off_proceeds_silently(method, caplog):
    frappe.has_permission = MagicMock(return_value=False)
    db = _db("off")
    with caplog.at_level(logging.WARNING):
        _call_read(db, method)
    assert not any("would-deny" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("method", READ_METHODS)
def test_read_warn_logs_and_proceeds(method, caplog):
    frappe.has_permission = MagicMock(return_value=False)
    db = _db("warn")
    with caplog.at_level(logging.WARNING):
        _call_read(db, method)
    deny = [r for r in caplog.records if "would-deny" in r.getMessage()]
    assert len(deny) == 1


@pytest.mark.parametrize("method", READ_METHODS)
def test_read_enforce_raises(method):
    frappe.has_permission = MagicMock(return_value=False)
    db = _db("enforce")
    with pytest.raises(frappe.PermissionError):
        _call_read(db, method)


@pytest.mark.parametrize("method", READ_METHODS)
def test_read_allowed_no_warn_no_raise(method, caplog):
    frappe.has_permission = MagicMock(return_value=True)
    db = _db("enforce")
    with caplog.at_level(logging.WARNING):
        _call_read(db, method)  # must not raise
    assert not any("would-deny" in r.getMessage() for r in caplog.records)


# --- get_doc (doc-level) ---------------------------------------------------

def _doc(tenant=TENANT, allowed=False):
    d = MagicMock()
    d.doctype = "Widget"
    d.tenant_id = tenant
    d.has_permission = MagicMock(return_value=allowed)
    return d


def test_get_doc_enforce_raises_when_denied():
    doc = _doc(allowed=False)
    frappe.get_doc = MagicMock(return_value=doc)
    db = _db("enforce")
    with pytest.raises(frappe.PermissionError):
        db.get_doc("Widget", "W-1")


def test_get_doc_warn_logs_once():
    doc = _doc(allowed=False)
    frappe.get_doc = MagicMock(return_value=doc)
    db = _db("warn")
    # should return the doc, not raise
    assert db.get_doc("Widget", "W-1") is doc


def test_get_doc_tenant_error_precedes_permission():
    """Cross-tenant doc raises the tenant PermissionError before the perm check."""
    doc = _doc(tenant="other-tenant", allowed=False)
    frappe.get_doc = MagicMock(return_value=doc)
    db = _db("enforce")
    with pytest.raises(frappe.PermissionError):
        db.get_doc("Widget", "W-1")
    # has_permission must NOT have been consulted (tenant check fired first)
    doc.has_permission.assert_not_called()


# --- set_value -------------------------------------------------------------

def test_set_value_enforce_raises_on_write_denied():
    doc = _doc(allowed=False)
    frappe.get_doc = MagicMock(return_value=doc)
    db = _db("enforce")
    with pytest.raises(frappe.PermissionError):
        db.set_value("Widget", "W-1", "field", "v")
    doc.has_permission.assert_called_with("write")


def test_set_value_warn_single_warning(caplog):
    doc = _doc(allowed=False)
    frappe.get_doc = MagicMock(return_value=doc)
    frappe.db.set_value = MagicMock()
    db = _db("warn")
    with caplog.at_level(logging.WARNING):
        db.set_value("Widget", "W-1", "field", "v")
    deny = [r for r in caplog.records if "would-deny" in r.getMessage()]
    assert len(deny) == 1  # only the write check, not a duplicate read check


def test_set_value_tenant_error_precedes_permission():
    doc = _doc(tenant="other", allowed=False)
    frappe.get_doc = MagicMock(return_value=doc)
    db = _db("enforce")
    with pytest.raises(frappe.PermissionError):
        db.set_value("Widget", "W-1", "field", "v")
    doc.has_permission.assert_not_called()


# --- secure_route surfaces 403 --------------------------------------------

def test_enforce_denial_returns_403_via_secure_route():
    with patch('frappe.init'), patch('frappe.connect'):
        app = MicroserviceApp("test-service", permission_mode="enforce")

    @app.secure_route('/widgets', methods=['GET'])
    def list_widgets(user):
        frappe.has_permission = MagicMock(return_value=False)
        return {"data": app.tenant_db.get_all("Widget")}

    with app.flask_app.test_request_context('/widgets'):
        with patch.object(app, '_validate_session', return_value=('u@e.com', None)):
            with patch('frappe_microservice.app.get_user_tenant_id', return_value='t1'):
                with patch('frappe.db.rollback'):
                    import uuid
                    g.request_id = str(uuid.uuid4())
                    g._frappe_rolled_back = False
                    response, status = list_widgets()
    assert status == 403
    assert response.json['type'] == 'PermissionError'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
