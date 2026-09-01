"""
Unit tests for presync runtime preparation, fixture ordering, and post-presync hooks.

Covers regressions seen booting real services:
- Module map registration before DocType import (orphan SQL tables)
- module_def.json imported before other fixtures
- SERVICE_POST_PRESYNC resolved via importlib on SERVICE_PATH (not frappe.get_attr)
- Controller auto-discovery under $SERVICE_PATH/controllers/
"""

import json
import os
import sys

from unittest.mock import MagicMock, patch

import frappe

import frappe_microservice.presync as iso


def _reset_frappe_local():
    """Real dicts on mock frappe.local (MagicMock hasattr always True)."""
    frappe.local.module_app = {}
    frappe.local.app_modules = {}


def _clear_bootstrap_module():
    sys.modules.pop("bootstrap", None)


# --- register_module_for_service -------------------------------------------


def test_register_module_for_service_populates_maps():
    _reset_frappe_local()

    iso.register_module_for_service("Signup Service", "signup_service")

    assert frappe.local.module_app["Signup Service"] == "signup_service"
    assert frappe.local.module_app["signup_service"] == "signup_service"
    assert "Signup Service" in frappe.local.app_modules["signup_service"]
    assert "signup_service" in frappe.local.app_modules["signup_service"]


def test_register_module_for_service_idempotent():
    _reset_frappe_local()
    frappe.local.module_app["Signup Service"] = "signup_service"
    frappe.local.app_modules["signup_service"] = ["Signup Service"]

    iso.register_module_for_service("Signup Service", "signup_service")

    assert frappe.local.app_modules["signup_service"].count("Signup Service") == 1


def test_register_module_for_service_noop_on_empty_module():
    _reset_frappe_local()

    iso.register_module_for_service(None, "signup_service")
    iso.register_module_for_service("", "signup_service")

    assert frappe.local.module_app == {}
    assert frappe.local.app_modules == {}


# --- _prepare_presync_runtime ----------------------------------------------


def test_prepare_presync_runtime_appends_service_path_and_registers_modules(tmp_path):
    service = tmp_path / "service"
    service.mkdir()
    doctypes = service / "signup_service" / "signup_service" / "doctype"
    doctypes.mkdir(parents=True)
    folder = doctypes / "phone_auth_session"
    folder.mkdir()
    (folder / "phone_auth_session.json").write_text(
        json.dumps(
            {
                "name": "Phone Auth Session",
                "module": "Signup Service",
                "doctype": "DocType",
            }
        )
    )

    _reset_frappe_local()
    sys.path[:] = [p for p in sys.path if str(service) not in p]
    logger = MagicMock()

    with patch.object(
        iso, "register_service_doctypes", wraps=iso.register_service_doctypes
    ) as reg:
        iso._prepare_presync_runtime(
            str(service), "signup-service", str(doctypes), logger
        )

    reg.assert_called_once()
    assert str(service) in sys.path
    assert frappe.local.module_app.get("Signup Service") == "signup_service"


def test_prepare_presync_runtime_discovers_controllers(tmp_path):
    service = tmp_path / "service"
    controllers = service / "controllers"
    controllers.mkdir(parents=True)
    (controllers / "phone_auth_session.py").write_text(
        "from frappe_microservice.controller import DocumentController\n"
        "class PhoneAuthSession(DocumentController):\n"
        "    pass\n"
    )

    _reset_frappe_local()
    sys.path[:] = [p for p in sys.path if str(service) not in p]
    logger = MagicMock()

    from frappe_microservice.controller import ControllerRegistry

    registry = ControllerRegistry()
    with patch(
        "frappe_microservice.controller.get_controller_registry",
        return_value=registry,
    ):
        iso._prepare_presync_runtime(str(service), "signup-service", None, logger)

    assert "Phone Auth Session" in registry.list_controllers()


# --- _sync_fixtures_from_path ----------------------------------------------


def test_sync_fixtures_imports_module_def_first(tmp_path):
    fix_dir = tmp_path / "fixtures"
    fix_dir.mkdir()
    (fix_dir / "module_def.json").write_text(
        json.dumps(
            [
                {
                    "doctype": "Module Def",
                    "module_name": "Signup Service",
                    "app_name": "signup_service",
                }
            ]
        )
    )
    (fix_dir / "sms_settings.json").write_text(
        json.dumps(
            [{"doctype": "SMS Settings", "sms_gateway_url": "https://example.com"}]
        )
    )

    order = []
    mock_import = MagicMock(
        side_effect=lambda path, **kw: order.append(os.path.basename(path))
    )

    with patch.object(iso, "_get_import_file_by_path", return_value=mock_import):
        iso._sync_fixtures_from_path(str(fix_dir), MagicMock())

    assert order[0] == "module_def.json"
    assert "sms_settings.json" in order
    assert mock_import.call_count == 2


def test_sync_fixtures_continues_after_module_def_error(tmp_path):
    fix_dir = tmp_path / "fixtures"
    fix_dir.mkdir()
    (fix_dir / "module_def.json").write_text("[]")
    (fix_dir / "email_account.json").write_text(
        json.dumps([{"doctype": "Email Account", "email_id": "otp@example.com"}])
    )

    calls = {"n": 0}

    def _import(path, **kw):
        calls["n"] += 1
        if "module_def" in path:
            raise RuntimeError("module def import failed")
        return True

    logger = MagicMock()
    with patch.object(iso, "_get_import_file_by_path", return_value=_import):
        iso._sync_fixtures_from_path(str(fix_dir), logger)

    assert calls["n"] == 2
    frappe.db.rollback.assert_called()


def test_sync_fixtures_skips_missing_directory():
    logger = MagicMock()
    with patch.object(iso, "_get_import_file_by_path") as imp:
        iso._sync_fixtures_from_path("/nonexistent/fixtures", logger)
    imp.assert_not_called()
    logger.warning.assert_called_once()


# --- presync post-hook integration (mocked frappe) -------------------------


def _presync_env(tmp_path, monkeypatch, *, hook="", extra_env=None):
    service = tmp_path / "service"
    doctypes = service / "test_service" / "test_service" / "doctype"
    doctypes.mkdir(parents=True)
    folder = doctypes / "widget"
    folder.mkdir()
    (folder / "widget.json").write_text(
        json.dumps(
            {"name": "Presync Widget", "module": "Test Service", "doctype": "DocType"}
        )
    )

    env = {
        "SERVICE_PATH": str(service),
        "SERVICE_NAME": "test-service",
        "FRAPPE_SITE": "dev.localhost",
        "FRAPPE_SITES_PATH": "/tmp/sites",
    }
    if hook:
        env["SERVICE_POST_PRESYNC"] = hook
    if extra_env:
        env.update(extra_env)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return service, doctypes


def test_presync_runs_post_presync_hook(tmp_path, monkeypatch):
    service, doctypes = _presync_env(tmp_path, monkeypatch)
    (service / "bootstrap.py").write_text(
        "MARKER = []\n" "def run_post_presync():\n" "    MARKER.append('ran')\n"
    )
    monkeypatch.setenv("SERVICE_POST_PRESYNC", "bootstrap.run_post_presync")
    monkeypatch.delitem(sys.modules, "bootstrap", raising=False)
    monkeypatch.syspath_prepend(str(service))

    monkeypatch.setattr(frappe, "init", MagicMock())
    monkeypatch.setattr(frappe, "connect", MagicMock())
    monkeypatch.setattr(frappe, "destroy", MagicMock())
    db = MagicMock()
    db.db_type = "mariadb"
    db.exists = MagicMock(return_value=False)
    db.has_column = MagicMock(return_value=True)
    db.table_exists = MagicMock(return_value=True)
    db.commit = MagicMock()
    db.rollback = MagicMock()
    db.sql = MagicMock(return_value=[[1]])
    monkeypatch.setattr(frappe, "db", db)

    fake_import = MagicMock(return_value=True)
    with patch.object(iso, "_get_import_file_by_path", return_value=fake_import):
        iso.presync_service_doctypes()

    import bootstrap

    assert bootstrap.MARKER == ["ran"]
    assert db.commit.call_count >= 2


def test_presync_post_hook_failure_does_not_abort_fixture_sync(tmp_path, monkeypatch):
    service, _ = _presync_env(tmp_path, monkeypatch)
    fixtures = service / "fixtures"
    fixtures.mkdir()
    (fixtures / "module_def.json").write_text("[]")

    (service / "bootstrap.py").write_text(
        "def run_post_presync():\n" "    raise RuntimeError('hook failed')\n"
    )
    monkeypatch.setenv("SERVICE_POST_PRESYNC", "bootstrap.run_post_presync")
    monkeypatch.delitem(sys.modules, "bootstrap", raising=False)
    monkeypatch.syspath_prepend(str(service))

    monkeypatch.setattr(frappe, "init", MagicMock())
    monkeypatch.setattr(frappe, "connect", MagicMock())
    monkeypatch.setattr(frappe, "destroy", MagicMock())
    db = MagicMock()
    db.db_type = "mariadb"
    db.exists = MagicMock(return_value=False)
    db.has_column = MagicMock(return_value=True)
    db.table_exists = MagicMock(return_value=True)
    db.commit = MagicMock()
    db.rollback = MagicMock()
    db.sql = MagicMock(return_value=[[1]])
    monkeypatch.setattr(frappe, "db", db)

    fake_import = MagicMock(return_value=True)
    with patch.object(iso, "_get_import_file_by_path", return_value=fake_import):
        iso.presync_service_doctypes()

    assert fake_import.call_count >= 2
    db.rollback.assert_called()


def test_presync_defaults_doctypes_path_from_service_name(tmp_path, monkeypatch):
    service = tmp_path / "service"
    doctypes = service / "signup_service" / "signup_service" / "doctype"
    doctypes.mkdir(parents=True)
    folder = doctypes / "phone_auth_session"
    folder.mkdir()
    (folder / "phone_auth_session.json").write_text(
        json.dumps(
            {
                "name": "Phone Auth Session",
                "module": "Signup Service",
                "doctype": "DocType",
            }
        )
    )

    monkeypatch.setenv("SERVICE_PATH", str(service))
    monkeypatch.setenv("SERVICE_NAME", "signup-service")
    monkeypatch.delenv("DOCTYPES_PATH", raising=False)

    monkeypatch.setattr(frappe, "init", MagicMock())
    monkeypatch.setattr(frappe, "connect", MagicMock())
    monkeypatch.setattr(frappe, "destroy", MagicMock())
    db = MagicMock()
    db.db_type = "mariadb"
    db.exists = MagicMock(return_value=False)
    db.has_column = MagicMock(return_value=True)
    db.table_exists = MagicMock(return_value=True)
    db.commit = MagicMock()
    db.rollback = MagicMock()
    db.sql = MagicMock(return_value=[[1]])
    monkeypatch.setattr(frappe, "db", db)

    fake_import = MagicMock(return_value=True)
    with patch.object(iso, "_get_import_file_by_path", return_value=fake_import):
        iso.presync_service_doctypes(fixtures_path="/nonexistent")

    assert fake_import.call_count == 1
    path_arg = fake_import.call_args_list[0][0][0]
    assert path_arg.endswith("phone_auth_session.json")
