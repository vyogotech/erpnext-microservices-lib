"""
Unit tests for log & error-response hygiene + truthful health checks.

Covers
- (A) Session cookies / sid never logged in full.
- (B) 500 error body gates str(e) behind debug.
- (C) secure_route per-request logs are debug-level (nothing at INFO).
- (D) /health reports real connection state; /health/ready does a DB ping.
"""

import logging

import pytest
from unittest.mock import patch
import frappe
from flask import g
from frappe_microservice.core import MicroserviceApp


@pytest.fixture
def app():
    with patch('frappe.init'), patch('frappe.connect'):
        return MicroserviceApp("test-service", central_site_url="http://central")


# --- (A) cookie / sid redaction -------------------------------------------

@patch('requests.get')
def test_sid_never_logged_in_full(mock_get, app, caplog):
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {'message': 'user@example.com'}

    secret_sid = "supersecretsidvalue1234567890"
    app.logger.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        with app.flask_app.test_request_context(
            headers={'Cookie': f'sid={secret_sid}'}
        ):
            app._validate_session()

    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_sid not in full_log, "full sid leaked into logs"
    # The redacted debug line logs only a truncated prefix.
    assert secret_sid[:8] in full_log


# --- (B) 500 body gating ---------------------------------------------------

def _make_erroring_client(app):
    @app.flask_app.route('/boom')
    def boom():
        raise RuntimeError("SENSITIVE sql fragment tabUser.password")
    return app.flask_app.test_client()


def test_500_hides_exception_string_by_default(app):
    with patch.object(app, '_restore_frappe_local'):
        client = _make_erroring_client(app)
        resp = client.get('/boom')
    assert resp.status_code == 500
    body = resp.get_json()
    assert body['error'] is None
    assert body['details'] is None
    assert "SENSITIVE" not in resp.get_data(as_text=True)


def test_500_shows_exception_string_in_debug(app, monkeypatch):
    monkeypatch.setenv('DEBUG', '1')
    with patch.object(app, '_restore_frappe_local'):
        client = _make_erroring_client(app)
        resp = client.get('/boom')
    body = resp.get_json()
    assert body['error'] is not None
    assert "SENSITIVE" in body['error']
    assert body['details'] is not None


# --- (C) quiet per-request logs -------------------------------------------

def test_secure_route_no_info_logs_on_success(app, caplog):
    @app.secure_route('/ping', methods=['GET'])
    def ping(user):
        return {"ok": True}

    with app.flask_app.test_request_context('/ping'):
        with patch.object(app, '_validate_session', return_value=('u@e.com', None)):
            with patch('frappe_microservice.app.get_user_tenant_id', return_value='t1'):
                with caplog.at_level(logging.INFO):
                    ping()

    assert not any("secure_route" in r.getMessage().lower()
                   for r in caplog.records if r.levelno >= logging.INFO)


# --- (D) truthful health + readiness --------------------------------------

def test_health_reports_pending_then_connected(app):
    client = app.flask_app.test_client()

    app._db_connected = False
    resp = client.get('/health')
    assert resp.get_json()['database'] == 'pending'

    app._db_connected = True
    resp = client.get('/health')
    assert resp.get_json()['database'] == 'connected'


def test_health_ready_success(app):
    with patch.object(app, '_restore_frappe_local'):
        with patch('frappe.db.sql', return_value=((1,),)):
            resp = app.flask_app.test_client().get('/health/ready')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['status'] == 'ready'
    assert body['database'] == 'connected'


def test_health_ready_failure_hides_error(app):
    with patch.object(app, '_restore_frappe_local'):
        with patch('frappe.db.sql', side_effect=Exception("cannot connect to 10.0.0.5")):
            resp = app.flask_app.test_client().get('/health/ready')
    assert resp.status_code == 503
    body = resp.get_json()
    assert body['status'] == 'not_ready'
    assert body['database'] == 'error'
    assert body['error_type'] == 'Exception'
    assert "10.0.0.5" not in resp.get_data(as_text=True)
