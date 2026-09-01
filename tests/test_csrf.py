"""
Unit tests for CSRF protection + CORS default hygiene.

CSRF: only cookie-authenticated mutations require a signal. We drive
``g.auth_method`` by patching ``_validate_session`` to set it, then invoke the
secured view within a request context.
"""

import logging

import pytest
from unittest.mock import patch
import frappe
from flask import g
from frappe_microservice.core import MicroserviceApp
from frappe_microservice.site_config import _build_config_from_env


def _make_app(csrf_mode):
    with patch('frappe.init'), patch('frappe.connect'):
        return MicroserviceApp("test-service", csrf_mode=csrf_mode)


def _invoke(app, method="POST", headers=None, auth_method="sid_cookie"):
    @app.secure_route('/mut', methods=['GET', 'POST', 'PUT'], endpoint='mut')
    def mut(user):
        return {"ok": True}

    def fake_validate():
        g.auth_method = auth_method
        return ('u@e.com', None)

    with app.flask_app.test_request_context('/mut', method=method,
                                            headers=headers or {}):
        with patch.object(app, '_validate_session', side_effect=fake_validate):
            with patch('frappe_microservice.app.get_user_tenant_id', return_value='t1'):
                g.request_id = 'rid'
                g._frappe_rolled_back = False
                result = mut()
    if isinstance(result, tuple):
        return result[0], result[1]
    return result, result.status_code


# --- CSRF matrix -----------------------------------------------------------

def test_cookie_post_no_signal_off():
    resp, status = _invoke(_make_app("off"))
    assert status == 200


def test_cookie_post_no_signal_warn_logs(caplog):
    with caplog.at_level(logging.WARNING):
        resp, status = _invoke(_make_app("warn"))
    assert status == 200
    assert any("would-deny] CSRF" in r.getMessage() for r in caplog.records)


def test_cookie_post_no_signal_enforce_403():
    resp, status = _invoke(_make_app("enforce"))
    assert status == 403
    assert resp.json['type'] == 'CSRFError'


def test_cookie_post_with_xrw_signal_ok_all_modes():
    for mode in ("off", "warn", "enforce"):
        resp, status = _invoke(
            _make_app(mode),
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        assert status == 200, mode


def test_cookie_get_no_signal_ok_all_modes():
    for mode in ("off", "warn", "enforce"):
        resp, status = _invoke(_make_app(mode), method="GET")
        assert status == 200, mode


def test_bearer_post_no_signal_ok_all_modes():
    for mode in ("off", "warn", "enforce"):
        resp, status = _invoke(_make_app(mode), auth_method="bearer")
        assert status == 200, mode


def test_sid_header_post_no_signal_ok_all_modes():
    for mode in ("off", "warn", "enforce"):
        resp, status = _invoke(_make_app(mode), auth_method="sid_header")
        assert status == 200, mode


# --- CORS default ----------------------------------------------------------

def test_allow_cors_env_respected(monkeypatch):
    monkeypatch.setenv("ALLOW_CORS", "https://app.example.com")
    cfg = _build_config_from_env()
    assert cfg['allow_cors'] == "https://app.example.com"


def test_allow_cors_default_star(monkeypatch):
    monkeypatch.delenv("ALLOW_CORS", raising=False)
    cfg = _build_config_from_env()
    assert cfg['allow_cors'] == '*'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
