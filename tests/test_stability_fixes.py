"""
Unit tests for stability fixes.

- (A) Unhandled exceptions on a plain route roll back before the 500; success
  still commits; HTTPExceptions do not roll back.
- (B) enqueue_task honors RQ_DEFAULT_JOB_TIMEOUT; explicit wins; unset = -1.
- (C) microservice_get_attr: allowed-app import errors propagate; non-allowed
  are converted to AttributeError; AppNotInstalledError always converts.
"""

import os

import pytest
from unittest.mock import MagicMock, patch
import frappe
from werkzeug.exceptions import NotFound
from frappe_microservice.core import MicroserviceApp


def _reset_guards():
    for flag in (
        "_microservice_isolation_applied",
        "_microservice_load_app_hooks_patched",
        "_microservice_hooks_resolution_patched",
        "_microservice_controller_patched",
    ):
        if hasattr(frappe, flag):
            delattr(frappe, flag)
    try:
        import frappe.utils.jinja as jinja_module

        if hasattr(jinja_module, "_microservice_jinja_hooks_patched"):
            delattr(jinja_module, "_microservice_jinja_hooks_patched")
        jinja_module.get_jinja_hooks = jinja_module.__dict__.get(
            "get_jinja_hooks",
            jinja_module.get_jinja_hooks,
        )
    except Exception:
        pass


@pytest.fixture
def app():
    with patch('frappe.init'), patch('frappe.connect'):
        return MicroserviceApp("test-service", central_site_url="http://central")


# --- (A) rollback in generic error handler --------------------------------

def test_unhandled_exception_rolls_back_and_no_commit(app):
    @app.flask_app.route('/write-then-boom')
    def write_then_boom():
        frappe.db.sql("INSERT INTO x VALUES (1)")
        raise RuntimeError("boom after write")

    with patch.object(app, '_restore_frappe_local'), \
            patch('frappe.db.rollback') as mock_rollback, \
            patch('frappe.db.commit') as mock_commit:
        resp = app.flask_app.test_client().get('/write-then-boom')

    assert resp.status_code == 500
    mock_rollback.assert_called()
    mock_commit.assert_not_called()


def test_http_exception_does_not_rollback(app):
    @app.flask_app.route('/not-found')
    def not_found():
        raise NotFound()

    with patch.object(app, '_restore_frappe_local'), \
            patch('frappe.db.rollback') as mock_rollback:
        resp = app.flask_app.test_client().get('/not-found')

    assert resp.status_code == 404
    mock_rollback.assert_not_called()


def test_successful_request_commits_once(app):
    @app.flask_app.route('/ok')
    def ok():
        return {"ok": True}

    with patch.object(app, '_restore_frappe_local'), \
            patch('frappe.db.commit') as mock_commit:
        # frappe.db must be truthy for after_request to attempt commit
        resp = app.flask_app.test_client().get('/ok')

    assert resp.status_code == 200
    assert mock_commit.call_count == 1


# --- (B) RQ default job timeout -------------------------------------------

@patch.dict(os.environ, {"ENABLE_RQ": "1", "REDIS_URL": "redis://localhost:6379"})
@patch("frappe_microservice.background.SimpleWorker")
@patch("frappe_microservice.background.Queue")
@patch("frappe_microservice.background.Redis.from_url")
def _make_rq_app(mock_redis, mock_queue_cls, mock_worker):
    _reset_guards()
    mock_queue_instance = MagicMock()
    mock_queue_cls.return_value = mock_queue_instance
    mock_worker.return_value = MagicMock()
    app = MicroserviceApp("test-service")
    return app, mock_queue_instance


def test_enqueue_default_timeout_minus_one(monkeypatch):
    monkeypatch.delenv("RQ_DEFAULT_JOB_TIMEOUT", raising=False)
    app, queue = _make_rq_app()
    app.enqueue_task(lambda: None)
    assert queue.enqueue.call_args[1]["job_timeout"] == -1


def test_enqueue_env_default_timeout(monkeypatch):
    monkeypatch.setenv("RQ_DEFAULT_JOB_TIMEOUT", "300")
    app, queue = _make_rq_app()
    app.enqueue_task(lambda: None)
    assert queue.enqueue.call_args[1]["job_timeout"] == 300


def test_enqueue_explicit_timeout_wins(monkeypatch):
    monkeypatch.setenv("RQ_DEFAULT_JOB_TIMEOUT", "300")
    app, queue = _make_rq_app()
    app.enqueue_task(lambda: None, job_timeout=60)
    assert queue.enqueue.call_args[1]["job_timeout"] == 60


# --- (C) narrowed get_attr patch ------------------------------------------

class _FakeAppNotInstalled(Exception):
    pass


def _build_patched_get_attr(app, original):
    """Install microservice_get_attr with `original` as the captured original.

    allowed_apps for "test-service" (default hooks) = {frappe, erpnext, test_service}.
    """
    _reset_guards()
    frappe.get_attr = original
    frappe.AppNotInstalledError = _FakeAppNotInstalled
    with patch('frappe.connect'):
        app._patch_hooks_resolution()
    return frappe.get_attr


def test_get_attr_allowed_app_import_error_propagates(app):
    def original(method_string):
        raise ModuleNotFoundError("broken import in our own app")

    patched = _build_patched_get_attr(app, original)
    with pytest.raises(ModuleNotFoundError):
        patched("test_service.hooks.broken")


def test_get_attr_non_allowed_app_converts_to_attributeerror(app):
    def original(method_string):
        raise ModuleNotFoundError("should not even be called")

    patched = _build_patched_get_attr(app, original)
    # Non-allowed app is short-circuited before import → AttributeError.
    with pytest.raises(AttributeError):
        patched("some_other_app.hooks.func")


def test_get_attr_app_not_installed_always_converts(app):
    def original(method_string):
        raise _FakeAppNotInstalled("erpnext not installed")

    patched = _build_patched_get_attr(app, original)
    # Even for an allowed app, AppNotInstalledError is a legitimate skip.
    with pytest.raises(AttributeError):
        patched("erpnext.stock.utils.func")


def test_get_attr_allowed_app_success(app):
    sentinel = object()

    def original(method_string):
        return sentinel

    patched = _build_patched_get_attr(app, original)
    assert patched("erpnext.stock.utils.func") is sentinel


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
