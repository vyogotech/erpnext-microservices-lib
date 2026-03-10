import pytest
import json
import logging
import sys
from unittest.mock import MagicMock, patch, call
from frappe_microservice import MicroserviceApp
import frappe


class TestDoctypesPathParameter:
    """Cycle 1: doctypes_path parameter on MicroserviceApp."""

    def test_default_is_none(self):
        app = MicroserviceApp("test-service")
        assert app.doctypes_path is None

    def test_stores_explicit_value(self):
        app = MicroserviceApp("test-service", doctypes_path="/tmp/doctypes")
        assert app.doctypes_path == "/tmp/doctypes"

    def test_service_doctype_names_initialized(self):
        app = MicroserviceApp("test-service")
        assert isinstance(app._service_doctype_names, set)
        assert len(app._service_doctype_names) == 0


class TestSyncNoopAndGuards:
    """Cycles 2-3: no-op when None, missing directory handling."""

    def test_noop_when_path_is_none(self):
        app = MicroserviceApp("test-service")
        app._sync_service_doctypes()
        assert len(app._service_doctype_names) == 0

    def test_missing_directory_logs_warning(self, caplog):
        app = MicroserviceApp("test-service", doctypes_path="/tmp/non_existent_dir_xyz")
        with caplog.at_level(logging.WARNING):
            app._sync_service_doctypes()
        assert "DocTypes directory not found" in caplog.text


def _make_doctype_dir(tmp_path, name="phone_auth_session",
                      doc_name="Phone Auth Session", module="Signup Service"):
    """Helper to create a doctype directory with a JSON file."""
    dt_dir = tmp_path / "doctypes"
    dt_dir.mkdir(exist_ok=True)
    sub_dir = dt_dir / name
    sub_dir.mkdir(exist_ok=True)
    json_file = sub_dir / f"{name}.json"
    json_file.write_text(json.dumps({
        "name": doc_name,
        "doctype": "DocType",
        "module": module,
    }))
    return str(dt_dir)


class TestSyncScanAndRegister:
    """Cycles 4-7: scan, exists check, import, module mapping.

    Note: All tests here mock frappe.db.exists. In production, the real
    db.exists() goes through the query builder, which calls
    get_additional_filters_from_hooks() -> get_attr(hook)(). That can
    re-enter our patched get_attr and cause RecursionError if hook code
    triggers another DB query. The re-entrancy guard in isolation.py fixes
    that; see test_e2e_simulation.test_get_attr_*reentrancy*.
    """

    def test_scan_discovers_doctype(self, tmp_path):
        """Cycle 4: discovered doctypes are added to _service_doctype_names."""
        dt_path = _make_doctype_dir(tmp_path)
        app = MicroserviceApp("test-service", doctypes_path=dt_path)

        frappe.db.exists = MagicMock(return_value=True)
        app._sync_service_doctypes()

        assert "Phone Auth Session" in app._service_doctype_names

    def test_skips_import_when_doctype_exists(self, tmp_path):
        """Cycle 5: when doctype exists in DB, import_doc is NOT called."""
        dt_path = _make_doctype_dir(tmp_path)
        app = MicroserviceApp("test-service", doctypes_path=dt_path)

        frappe.db.exists = MagicMock(return_value=True)
        frappe.modules.import_file.import_doc = MagicMock()
        app._sync_service_doctypes()

        frappe.modules.import_file.import_doc.assert_not_called()
        assert "Phone Auth Session" in app._service_doctype_names

    def test_creates_doctype_when_missing(self, tmp_path):
        """Cycle 6: when doctype does not exist, import_doc IS called."""
        dt_path = _make_doctype_dir(tmp_path)
        app = MicroserviceApp("test-service", doctypes_path=dt_path)

        frappe.db.exists = MagicMock(return_value=False)
        mock_import_doc = MagicMock()
        frappe.modules.import_file.import_doc = mock_import_doc
        app._sync_service_doctypes()

        mock_import_doc.assert_called_once()
        call_args = mock_import_doc.call_args
        assert call_args[0][0]["name"] == "Phone Auth Session"
        assert call_args[1]["ignore_version"] is True

    def test_registers_module_mapping_when_exists(self, tmp_path):
        """Cycle 7: module mapping is registered even when doctype already exists."""
        dt_path = _make_doctype_dir(tmp_path)
        app = MicroserviceApp("signup-service", doctypes_path=dt_path)

        frappe.local.module_app = {}
        frappe.local.app_modules = {}
        frappe.db.exists = MagicMock(return_value=True)
        app._sync_service_doctypes()

        assert frappe.local.module_app["signup_service"] == "signup_service"
        assert "signup_service" in frappe.local.app_modules["signup_service"]

    def test_registers_module_mapping_when_missing(self, tmp_path):
        """Cycle 7: module mapping is registered even when doctype is created."""
        dt_path = _make_doctype_dir(tmp_path)
        app = MicroserviceApp("signup-service", doctypes_path=dt_path)

        frappe.local.module_app = {}
        frappe.local.app_modules = {}
        frappe.db.exists = MagicMock(return_value=False)
        frappe.modules.import_file.import_doc = MagicMock()
        app._sync_service_doctypes()

        assert frappe.local.module_app["signup_service"] == "signup_service"
        assert "signup_service" in frappe.local.app_modules["signup_service"]

    def test_commits_only_when_imported(self, tmp_path):
        """DB commit only happens when at least one doctype was created."""
        dt_path = _make_doctype_dir(tmp_path)
        app = MicroserviceApp("test-service", doctypes_path=dt_path)

        frappe.db.exists = MagicMock(return_value=True)
        frappe.db.commit = MagicMock()
        app._sync_service_doctypes()

        frappe.db.commit.assert_not_called()

    def test_commits_after_creation(self, tmp_path):
        """DB commit happens after creating a new doctype."""
        dt_path = _make_doctype_dir(tmp_path)
        app = MicroserviceApp("test-service", doctypes_path=dt_path)

        frappe.db.exists = MagicMock(return_value=False)
        frappe.db.commit = MagicMock()
        frappe.modules.import_file.import_doc = MagicMock()
        app._sync_service_doctypes()

        frappe.db.commit.assert_called_once()


class TestSyncErrorHandling:
    """Cycles 8/8b: graceful error handling."""

    def test_invalid_json_logs_error(self, tmp_path, caplog):
        dt_dir = tmp_path / "doctypes"
        dt_dir.mkdir()
        sub = dt_dir / "bad"
        sub.mkdir()
        (sub / "bad.json").write_text("{invalid json")

        app = MicroserviceApp("test-service", doctypes_path=str(dt_dir))
        with caplog.at_level(logging.ERROR):
            app._sync_service_doctypes()

        assert "Error reading/syncing DocType JSON" in caplog.text

    def test_import_doc_failure_logs_error(self, tmp_path, caplog):
        dt_dir = tmp_path / "doctypes"
        dt_dir.mkdir()
        sub = dt_dir / "dt1"
        sub.mkdir()
        (sub / "dt1.json").write_text(json.dumps({
            "name": "DT1", "doctype": "DocType", "module": "M1"
        }))

        app = MicroserviceApp("test-service", doctypes_path=str(dt_dir))
        frappe.db.exists = MagicMock(return_value=False)
        frappe.modules.import_file.import_doc = MagicMock(
            side_effect=Exception("DB error")
        )
        with caplog.at_level(logging.ERROR):
            app._sync_service_doctypes()

        assert "Error reading/syncing DocType JSON" in caplog.text

    def test_continues_after_error(self, tmp_path):
        """After one bad JSON, subsequent valid JSONs are still processed."""
        dt_dir = tmp_path / "doctypes"
        dt_dir.mkdir()
        bad = dt_dir / "aaa_bad"
        bad.mkdir()
        (bad / "bad.json").write_text("{invalid")
        good = dt_dir / "zzz_good"
        good.mkdir()
        (good / "good.json").write_text(json.dumps({
            "name": "Good DT", "doctype": "DocType", "module": "M1"
        }))

        app = MicroserviceApp("test-service", doctypes_path=str(dt_dir))
        frappe.db.exists = MagicMock(return_value=True)
        app._sync_service_doctypes()

        assert "Good DT" in app._service_doctype_names


class TestControllerResolution:
    """Cycles 9-11: _patch_controller_resolution."""

    def setup_method(self):
        for flag in ("_microservice_isolation_applied", "_microservice_load_app_hooks_patched", "_microservice_hooks_resolution_patched", "_microservice_controller_patched"):
            if hasattr(frappe, flag):
                delattr(frappe, flag)

    def test_fallback_to_base_document(self):
        """Cycle 9: service doctype falls back to base Document on ImportError."""
        app = MicroserviceApp("test-service")
        app._service_doctype_names = {"Service DT"}

        sentinel_document = type("Document", (), {})

        def raise_import(doctype):
            raise ImportError(f"No module for {doctype}")

        frappe.model.base_document.import_controller = raise_import
        frappe.model.document.Document = sentinel_document

        app._patch_controller_resolution()

        result = frappe.model.base_document.import_controller("Service DT")
        assert result is sentinel_document

    def test_non_service_doctype_still_raises(self):
        """Cycle 11: non-service doctype ImportError propagates normally."""
        app = MicroserviceApp("test-service")
        app._service_doctype_names = {"Service DT"}

        def raise_import(doctype):
            raise ImportError(f"No module for {doctype}")

        frappe.model.base_document.import_controller = raise_import

        app._patch_controller_resolution()

        with pytest.raises(ImportError, match="No module for Unknown"):
            frappe.model.base_document.import_controller("Unknown DT")

    def test_controller_registry_takes_precedence(self):
        """Cycle 10: registered controller used instead of base Document."""
        from frappe_microservice.controller import DocumentController, get_controller_registry

        app = MicroserviceApp("test-service")
        app._service_doctype_names = {"Service DT"}

        class MyController(DocumentController):
            pass

        registry = get_controller_registry()
        registry.register("Service DT", MyController)

        def raise_import(doctype):
            raise ImportError(f"No module for {doctype}")

        frappe.model.base_document.import_controller = raise_import

        try:
            app._patch_controller_resolution()
            result = frappe.model.base_document.import_controller("Service DT")
            assert result is MyController
        finally:
            registry._controllers.pop("Service DT", None)

    def test_successful_import_passes_through(self):
        """When original import_controller succeeds, the patch doesn't interfere."""
        app = MicroserviceApp("test-service")
        app._service_doctype_names = {"Service DT"}

        sentinel = type("OriginalController", (), {})

        frappe.model.base_document.import_controller = lambda dt: sentinel

        app._patch_controller_resolution()

        result = frappe.model.base_document.import_controller("Service DT")
        assert result is sentinel

    def test_guard_prevents_double_patching(self):
        """Controller patch only applies once."""
        app = MicroserviceApp("test-service")
        app._service_doctype_names = {"DT1"}

        first_original = MagicMock(side_effect=ImportError("first"))
        frappe.model.base_document.import_controller = first_original
        frappe.model.document.Document = "FirstDocument"

        app._patch_controller_resolution()

        second_original = MagicMock(side_effect=ImportError("second"))
        frappe.model.base_document.import_controller_backup = second_original
        app._patch_controller_resolution()

        result = frappe.model.base_document.import_controller("DT1")
        assert result == "FirstDocument"


class TestSetupFrappeContextWiring:
    """Cycle 12: startup call order."""

    def test_correct_call_order(self):
        app = MicroserviceApp("test-service")

        frappe.local.site = None

        calls = []
        app._patch_app_resolution = lambda: calls.append("patch_app")
        app._filter_module_maps = lambda: calls.append("filter_maps")
        app._patch_controller_resolution = lambda: calls.append("patch_controller")
        app._sync_service_doctypes = lambda: calls.append("sync_doctypes")
        app._patch_hooks_resolution = lambda: calls.append("patch_hooks")
        frappe.init = MagicMock(side_effect=lambda **kw: calls.append("frappe_init"))
        frappe.connect = MagicMock(side_effect=lambda **kw: calls.append("frappe_connect"))

        with app.flask_app.test_request_context("/"):
            app.setup_frappe_context()

        expected_order = [
            "patch_app",
            "frappe_init",
            "filter_maps",
            "patch_controller",
            "frappe_connect",
            "sync_doctypes",
            "patch_hooks",
        ]
        assert calls == expected_order

    def test_sync_after_connect(self):
        """sync_doctypes must run after frappe.connect (needs DB)."""
        app = MicroserviceApp("test-service")

        frappe.local.site = None

        calls = []
        app._patch_app_resolution = lambda: None
        app._filter_module_maps = lambda: None
        app._patch_controller_resolution = lambda: None
        app._sync_service_doctypes = lambda: calls.append("sync")
        app._patch_hooks_resolution = lambda: None
        frappe.init = MagicMock()
        frappe.connect = MagicMock(side_effect=lambda **kw: calls.append("connect"))

        with app.flask_app.test_request_context("/"):
            app.setup_frappe_context()

        assert calls.index("connect") < calls.index("sync")

    def test_startup_ops_run_only_once_across_multiple_inits(self):
        """sync_doctypes and patch_hooks run once globally, not per-thread."""
        app = MicroserviceApp("test-service")

        sync_calls = []
        hooks_calls = []
        app._patch_app_resolution = lambda: None
        app._filter_module_maps = lambda: None
        app._patch_controller_resolution = lambda: None
        app._sync_service_doctypes = lambda: sync_calls.append(1)
        app._patch_hooks_resolution = lambda: hooks_calls.append(1)
        frappe.init = MagicMock()
        frappe.connect = MagicMock()

        with app.flask_app.test_request_context("/"):
            frappe.local.site = None
            app.setup_frappe_context()
            assert len(sync_calls) == 1
            assert len(hooks_calls) == 1

            frappe.local.site = None
            app.setup_frappe_context()
            assert len(sync_calls) == 1, (
                "_sync_service_doctypes should not run again on subsequent inits"
            )
            assert len(hooks_calls) == 1, (
                "_patch_hooks_resolution should not run again on subsequent inits"
            )


class TestSyncIdempotencyAndMultiple:
    """Cycles 13-14: idempotent sync, multiple doctypes."""

    def test_idempotent_second_call(self, tmp_path):
        """Cycle 13: second sync call skips import (doctype now exists)."""
        dt_dir = tmp_path / "doctypes"
        dt_dir.mkdir()
        sub = dt_dir / "dt1"
        sub.mkdir()
        (sub / "dt1.json").write_text(json.dumps({
            "name": "DT1", "doctype": "DocType", "module": "M1"
        }))

        app = MicroserviceApp("test-service", doctypes_path=str(dt_dir))

        call_count = [0]
        exists_returns = iter([False, True])

        def mock_exists(doctype, name):
            return next(exists_returns, True)

        mock_import_doc = MagicMock()
        frappe.db.exists = mock_exists
        frappe.db.commit = MagicMock()
        frappe.modules.import_file.import_doc = mock_import_doc

        app._sync_service_doctypes()
        app._sync_service_doctypes()

        mock_import_doc.assert_called_once()
        assert len(app._service_doctype_names) == 1

    def test_multiple_doctypes_same_module(self, tmp_path):
        """Cycle 14: multiple doctypes registered, same module not duplicated."""
        dt_dir = tmp_path / "doctypes"
        dt_dir.mkdir()

        for name, dt_name in [("dt1", "DT1"), ("dt2", "DT2")]:
            sub = dt_dir / name
            sub.mkdir()
            (sub / f"{name}.json").write_text(json.dumps({
                "name": dt_name, "doctype": "DocType", "module": "Shared Module"
            }))

        app = MicroserviceApp("test-service", doctypes_path=str(dt_dir))
        frappe.local.module_app = {}
        frappe.local.app_modules = {}
        frappe.db.exists = MagicMock(return_value=True)

        app._sync_service_doctypes()

        assert "DT1" in app._service_doctype_names
        assert "DT2" in app._service_doctype_names
        assert len(app._service_doctype_names) == 2
        modules_list = frappe.local.app_modules["test_service"]
        assert modules_list.count("shared_module") == 1

    def test_multiple_doctypes_different_modules(self, tmp_path):
        """Multiple doctypes from different modules all get registered."""
        dt_dir = tmp_path / "doctypes"
        dt_dir.mkdir()

        for name, dt_name, mod in [("dt1", "DT1", "M1"), ("dt2", "DT2", "M2"), ("dt3", "DT3", "M2")]:
            sub = dt_dir / name
            sub.mkdir()
            (sub / f"{name}.json").write_text(json.dumps({
                "name": dt_name, "doctype": "DocType", "module": mod
            }))

        app = MicroserviceApp("test-service", doctypes_path=str(dt_dir))
        frappe.local.module_app = {}
        frappe.local.app_modules = {}
        frappe.db.exists = MagicMock(return_value=True)

        app._sync_service_doctypes()

        assert len(app._service_doctype_names) == 3
        assert frappe.local.module_app["m1"] == "test_service"
        assert frappe.local.module_app["m2"] == "test_service"
