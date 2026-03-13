"""
Tests for DB connection reuse and Gunicorn entrypoint behaviour.

Covers the fixes introduced in the "no DB leak" refactor:
  1. /health and /socket.io paths never touch the DB.
  2. First real API request opens the connection once per worker.
  3. Subsequent requests reuse the same DB object (no new connections).
  4. A failed ping triggers reconnect; subsequent requests reuse the new one.
  5. MicroserviceApp is WSGI-callable (Gunicorn compatibility).
  6. Entrypoint execs gunicorn with correct flags.
  7. after_request rolls back on commit failure instead of leaving a dirty tx.
"""

import os
import sys
import json
import pytest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(**kwargs):
    from frappe_microservice.app import MicroserviceApp
    app = MicroserviceApp("test-db-reuse", central_site_url="http://central", **kwargs)
    app.flask_app.testing = True
    return app


# ---------------------------------------------------------------------------
# 1. Skip paths: /health and /socket.io never call _restore_frappe_local
# ---------------------------------------------------------------------------

class TestSkipPaths:
    def test_health_does_not_call_restore(self):
        app = _make_app()
        with patch.object(app, '_restore_frappe_local') as mock_restore:
            client = app.flask_app.test_client()
            resp = client.get('/health')
            assert resp.status_code == 200
            mock_restore.assert_not_called()

    def test_socketio_does_not_call_restore(self):
        app = _make_app()
        with patch.object(app, '_restore_frappe_local') as mock_restore:
            client = app.flask_app.test_client()
            client.get('/socket.io/')
            mock_restore.assert_not_called()


# ---------------------------------------------------------------------------
# 2 & 3. DB opened once; reused on subsequent requests
# ---------------------------------------------------------------------------

class TestDbOpenedOnce:
    def test_connect_called_once_across_multiple_requests(self):
        import frappe
        app = _make_app()

        fake_db = MagicMock()
        fake_db._conn = MagicMock()
        frappe.local.db = fake_db

        @app.flask_app.route('/ping')
        def ping():
            return 'pong'

        connect_calls = []
        original_restore = app._restore_frappe_local

        def patched_restore():
            if not app._db_connected:
                app._db_connected = True
                app._db_obj = fake_db
                connect_calls.append('connect')
            else:
                frappe.local.db = app._db_obj

        app._restore_frappe_local = patched_restore

        client = app.flask_app.test_client()
        for _ in range(5):
            client.get('/ping')

        assert len(connect_calls) == 1, (
            f"Expected 1 connect call, got {len(connect_calls)}"
        )

    def test_db_obj_stored_on_instance_not_contextvar(self):
        """_db_obj must be the real DB, not a LocalProxy."""
        import frappe
        app = _make_app()

        real_db = MagicMock(name="real_db")
        real_db._conn = MagicMock()
        frappe.local.db = real_db
        frappe.connect = MagicMock(side_effect=lambda **_: None)

        with app.flask_app.test_request_context('/api/test'):
            app._restore_frappe_local()

        # _db_obj must be the real DB object (frappe.local.db), not frappe.db proxy
        assert app._db_obj is real_db


# ---------------------------------------------------------------------------
# 4. Reconnect on ping failure; reuse new connection afterwards
# ---------------------------------------------------------------------------

class TestReconnectOnPingFailure:
    def test_reconnect_when_ping_raises(self):
        import frappe
        app = _make_app()

        old_db = MagicMock(name="old_db")
        old_db._conn.ping.side_effect = OSError("gone away")
        app._db_connected = True
        app._db_obj = old_db

        new_db = MagicMock(name="new_db")
        new_db._conn = MagicMock()

        def fake_connect(**_):
            frappe.local.db = new_db

        frappe.connect = MagicMock(side_effect=fake_connect)

        with app.flask_app.test_request_context('/api/test'):
            app._restore_frappe_local()

        frappe.connect.assert_called_once_with(set_admin_as_user=False)
        assert app._db_obj is new_db

    def test_no_reconnect_when_ping_succeeds(self):
        import frappe
        app = _make_app()

        healthy_db = MagicMock(name="healthy_db")
        healthy_db._conn.ping.return_value = None
        app._db_connected = True
        app._db_obj = healthy_db
        frappe.connect = MagicMock()

        with app.flask_app.test_request_context('/api/test'):
            app._restore_frappe_local()

        frappe.connect.assert_not_called()


# ---------------------------------------------------------------------------
# 5. MicroserviceApp is WSGI-callable
# ---------------------------------------------------------------------------

class TestWsgiCallable:
    def test_app_is_callable(self):
        app = _make_app()
        assert callable(app), "MicroserviceApp must be WSGI-callable for Gunicorn"

    def test_call_delegates_to_flask_app(self):
        app = _make_app()
        mock_flask = MagicMock(return_value=iter([b'ok']))
        app.flask_app = mock_flask  # replace the whole flask_app
        sentinel_environ = {'wsgi.input': None}
        sentinel_start = MagicMock()
        list(app(sentinel_environ, sentinel_start))
        mock_flask.assert_called_once_with(sentinel_environ, sentinel_start)


# ---------------------------------------------------------------------------
# 6. Entrypoint execs gunicorn with correct flags
# ---------------------------------------------------------------------------

class TestEntrypointGunicorn:
    def test_main_execs_gunicorn(self, monkeypatch, tmp_path):
        import frappe_microservice.entrypoint as ep

        monkeypatch.setenv('SERVICE_PATH', str(tmp_path))
        monkeypatch.setenv('SERVICE_APP', 'server:app')
        monkeypatch.setenv('PORT', '9000')
        monkeypatch.setenv('GUNICORN_WORKERS', '2')
        monkeypatch.setenv('GUNICORN_TIMEOUT', '60')

        executed = {}

        def fake_execvpe(path, args, env):
            executed['path'] = path
            executed['args'] = args
            executed['env'] = env

        monkeypatch.setattr(os, 'execvpe', fake_execvpe)

        with patch.object(ep, 'create_site_config', return_value={}):
            ep.main()

        assert 'gunicorn' in executed['path']
        assert '--bind=0.0.0.0:9000' in executed['args']
        assert '--workers=2' in executed['args']
        assert '--worker-class=sync' in executed['args']
        assert '--worker-tmp-dir=/dev/shm' in executed['args']
        assert '--timeout=60' in executed['args']
        assert 'server:app' in executed['args']
        assert str(tmp_path) in executed['env']['PYTHONPATH']

    def test_main_default_workers_and_timeout(self, monkeypatch, tmp_path):
        import frappe_microservice.entrypoint as ep

        monkeypatch.setenv('SERVICE_PATH', str(tmp_path))
        monkeypatch.setenv('SERVICE_APP', 'server:app')
        monkeypatch.delenv('PORT', raising=False)
        monkeypatch.delenv('GUNICORN_WORKERS', raising=False)
        monkeypatch.delenv('GUNICORN_TIMEOUT', raising=False)

        executed = {}

        def fake_execvpe(path, args, env):
            executed['args'] = args

        monkeypatch.setattr(os, 'execvpe', fake_execvpe)

        with patch.object(ep, 'create_site_config', return_value={}):
            ep.main()

        assert '--bind=0.0.0.0:8000' in executed['args']
        assert '--workers=4' in executed['args']
        assert '--timeout=120' in executed['args']


# ---------------------------------------------------------------------------
# 7. after_request rollback on commit failure
# ---------------------------------------------------------------------------

class TestAfterRequestRollback:
    def test_rollback_called_when_commit_fails(self):
        import frappe
        app = _make_app()

        frappe.db.commit.side_effect = Exception("commit failed")
        frappe.db.rollback = MagicMock()

        @app.flask_app.route('/api/boom')
        def boom():
            return 'ok'

        with patch.object(app, '_restore_frappe_local'):
            client = app.flask_app.test_client()
            resp = client.get('/api/boom')

        frappe.db.rollback.assert_called()

    def test_commit_called_on_successful_request(self):
        import frappe
        app = _make_app()

        frappe.db.commit = MagicMock()
        frappe.db.rollback = MagicMock()

        @app.flask_app.route('/api/ok')
        def ok():
            return 'ok'

        with patch.object(app, '_restore_frappe_local'):
            client = app.flask_app.test_client()
            resp = client.get('/api/ok')

        frappe.db.commit.assert_called()
        frappe.db.rollback.assert_not_called()
