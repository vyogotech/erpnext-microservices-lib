import os
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
        """DB commit happens after creating a new doctype.
        Note: _ensure_module_def also commits (for its own Module Def insert),
        so the total commit count may be >= 1.
        """
        dt_path = _make_doctype_dir(tmp_path)
        app = MicroserviceApp("test-service", doctypes_path=dt_path)

        frappe.db.exists = MagicMock(return_value=False)
        frappe.db.commit = MagicMock()
        frappe.modules.import_file.import_doc = MagicMock()
        app._sync_service_doctypes()

        frappe.db.commit.assert_called()


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

        assert "Error reading DocType JSON" in caplog.text or "Error syncing DocType" in caplog.text

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

        assert "Error reading DocType JSON" in caplog.text or "Error syncing DocType" in caplog.text

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
        if hasattr(frappe, "_microservice_registry"):
            delattr(frappe, "_microservice_registry")

    def test_fallback_to_base_document(self):
        """Cycle 9: service doctype falls back to base Document on ImportError."""
        app = MicroserviceApp("test-service")
        app._service_doctype_names = {"Service DT"}
        # __init__ sets the guard via _initialize_frappe; remove it so the test
        # can apply its own controlled patch scenario.
        if hasattr(frappe, "_microservice_controller_patched"):
            delattr(frappe, "_microservice_controller_patched")

        # Mock SERVICE_DOCTYPES to include our test DT
        with patch("frappe_microservice.isolation._SERVICE_DOCTYPES", {"Service DT"}):
            sentinel_document = type("Document", (), {"__name__": "Document"})

        def raise_import(doctype):
            raise ImportError(f"No module for {doctype}")

        frappe.model.base_document.import_controller = raise_import
        frappe.model.document.Document = sentinel_document

        # Reset the patch flag to ensure it actually runs the logic
        if hasattr(frappe, "_microservice_controller_patched"):
            delattr(frappe, "_microservice_controller_patched")

        # Mock SERVICE_DOCTYPES to include our test DT
        with patch("frappe_microservice.isolation._SERVICE_DOCTYPES", {"Service DT"}):
            # Patch it again with the new list
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
        if hasattr(frappe, "_microservice_controller_patched"):
            delattr(frappe, "_microservice_controller_patched")

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

        sentinel = type("OriginalController", (), {"__name__": "OriginalController"})

        frappe.model.base_document.import_controller = lambda dt: sentinel

        app._patch_controller_resolution()

        result = frappe.model.base_document.import_controller("Service DT")
        assert result is sentinel

    def test_guard_prevents_double_patching(self):
        """Controller patch only applies once."""
        app = MicroserviceApp("test-service")
        app._service_doctype_names = {"DT1"}
        if hasattr(frappe, "_microservice_controller_patched"):
            delattr(frappe, "_microservice_controller_patched")

        first_original = MagicMock(side_effect=ImportError("first"))
        frappe.model.base_document.import_controller = first_original
        frappe.model.document.Document = type("FirstDocument", (), {"__name__": "FirstDocument"})

        app._patch_controller_resolution()

        # Guard is now set; a second call must not re-wrap the already-patched fn.
        app._patch_controller_resolution()

        result = frappe.model.base_document.import_controller("DT1")
        assert result.__name__ == "FirstDocument"


class TestSetupFrappeContextWiring:
    """Cycle 12: startup call order — new per-worker Gunicorn architecture.

    frappe.init + all patches run at __init__ time (_initialize_frappe).
    frappe.connect + sync_doctypes + patch_hooks run lazily on the first
    request per worker (_restore_frappe_local, gated by _db_connected).
    """

    def test_correct_call_order(self):
        """Startup sequence: init/patches at __init__, connect/sync on first request."""
        startup_calls = []

        with patch.object(MicroserviceApp, '_patch_app_resolution',
                          lambda self: startup_calls.append("patch_app")), \
             patch.object(MicroserviceApp, '_filter_module_maps',
                          lambda self: startup_calls.append("filter_maps")), \
             patch.object(MicroserviceApp, '_patch_controller_resolution',
                          lambda self: startup_calls.append("patch_controller")):
            frappe.init = MagicMock(side_effect=lambda **kw: startup_calls.append("frappe_init"))
            app = MicroserviceApp("test-service")

        assert "patch_app" in startup_calls
        assert "frappe_init" in startup_calls
        assert "filter_maps" in startup_calls
        assert "patch_controller" in startup_calls
        assert startup_calls.index("patch_app") < startup_calls.index("frappe_init")
        assert startup_calls.index("frappe_init") < startup_calls.index("filter_maps")

    def test_sync_after_connect(self):
        """In-memory registration and (in dev mode) DB sync run after frappe.connect."""
        app = MicroserviceApp("test-service")
        app._db_connected = False

        calls = []
        app._register_service_doctypes_from_json = lambda: calls.append("register")
        app._sync_service_doctypes_to_db = lambda: calls.append("db_sync")
        frappe.connect = MagicMock(side_effect=lambda **kw: calls.append("connect"))
        frappe.local.db = MagicMock()

        # No _DOCTYPES_PRESYNCED => dev mode, both register + db_sync run
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("_DOCTYPES_PRESYNCED", None)
            with app.flask_app.test_request_context("/"):
                app._restore_frappe_local()

        assert "connect" in calls
        assert "register" in calls
        assert "db_sync" in calls
        assert calls.index("connect") < calls.index("register")

    def test_startup_ops_run_only_once_across_multiple_inits(self):
        """Architecture: _patch_hooks_resolution runs once at startup (__init__),
        in-memory registration runs on first request only.
        Subsequent requests must not re-run either.
        """
        hooks_calls = []

        with patch.object(MicroserviceApp, '_patch_hooks_resolution',
                          lambda self: hooks_calls.append(1)):
            app = MicroserviceApp("test-service")
        assert len(hooks_calls) == 1, "_patch_hooks_resolution must run exactly once at startup"

        app._db_connected = False
        register_calls = []
        app._register_service_doctypes_from_json = lambda: register_calls.append(1)
        app._sync_service_doctypes_to_db = lambda: None
        frappe.connect = MagicMock()
        frappe.local.db = MagicMock()

        with app.flask_app.test_request_context("/"):
            app._restore_frappe_local()
        assert len(register_calls) == 1

        # Second request — same worker, _db_connected is True now.
        with app.flask_app.test_request_context("/"):
            app._restore_frappe_local()
        assert len(register_calls) == 1, \
            "in-memory registration must not run again on subsequent requests"


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

        # frappe.db.exists("DocType", ...) is called TWICE per sync iteration:
        # once to check if the module is already in DB (for module registration),
        # once for the actual doctype existence check. Use a call counter:
        #   calls 1-2 (first sync): False → False → import happens
        #   calls 3-4 (second sync): True  → True  → skipped
        doctype_call_count = [0]

        def mock_exists(doctype, name):
            if doctype == "Module Def":
                return True  # always pretend Module Def already exists
            doctype_call_count[0] += 1
            return doctype_call_count[0] > 2

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


# ---------------------------------------------------------------------------
# Fixture syncing tests (TDD)
# ---------------------------------------------------------------------------

def _make_fixture_dir(tmp_path, fixtures=None):
    """Helper to create a fixtures directory with JSON files."""
    fix_dir = tmp_path / "fixtures"
    fix_dir.mkdir(exist_ok=True)
    if fixtures:
        for filename, content in fixtures.items():
            (fix_dir / filename).write_text(json.dumps(content))
    return str(fix_dir)


class TestFixturesPathParameter:
    """Cycle F1: fixtures_path parameter on MicroserviceApp."""

    def test_default_is_none_without_service_path(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SERVICE_PATH", None)
            app = MicroserviceApp("test-service")
            assert app.fixtures_path is None

    def test_stores_explicit_value(self):
        app = MicroserviceApp("test-service", fixtures_path="/tmp/fixtures")
        assert app.fixtures_path == "/tmp/fixtures"

    def test_auto_discovers_from_service_path(self, tmp_path):
        """Convention: <SERVICE_PATH>/fixtures/ is auto-discovered."""
        fix_dir = tmp_path / "fixtures"
        fix_dir.mkdir()
        with patch.dict(os.environ, {"SERVICE_PATH": str(tmp_path)}):
            app = MicroserviceApp("test-service")
            assert app.fixtures_path == str(fix_dir)

    def test_no_auto_discover_when_dir_missing(self, tmp_path):
        """If <SERVICE_PATH>/fixtures/ doesn't exist, fixtures_path stays None."""
        with patch.dict(os.environ, {"SERVICE_PATH": str(tmp_path)}):
            app = MicroserviceApp("test-service")
            assert app.fixtures_path is None


class TestFixturesSyncGuards:
    """Cycle F2: no-op and directory guards."""

    def test_noop_when_path_is_none(self):
        app = MicroserviceApp("test-service")
        app._sync_fixtures_to_db()

    def test_missing_directory_logs_warning(self, caplog):
        app = MicroserviceApp("test-service", fixtures_path="/tmp/non_existent_fixtures_xyz")
        with caplog.at_level(logging.WARNING):
            app._sync_fixtures_to_db()
        assert "Fixtures directory not found" in caplog.text


class TestFixturesSyncImport:
    """Cycle F3-F5: scan, import, idempotency."""

    def test_imports_json_files(self, tmp_path):
        """Each .json file in fixtures dir is imported via import_file_by_path."""
        fixtures = {
            "sms_settings.json": [{"doctype": "SMS Settings", "sms_gateway_url": "https://test.example.com/api"}],
        }
        fix_path = _make_fixture_dir(tmp_path, fixtures)
        app = MicroserviceApp("test-service", fixtures_path=fix_path)

        mock_import = MagicMock()
        with patch("frappe_microservice.isolation._get_import_file_by_path", return_value=mock_import):
            app._sync_fixtures_to_db()

        mock_import.assert_called_once()
        call_args = mock_import.call_args
        assert call_args[0][0].endswith("sms_settings.json")
        assert call_args[1]["data_import"] is True
        assert call_args[1]["force"] is True

    def test_imports_multiple_fixtures(self, tmp_path):
        """All .json files in the fixtures directory are imported."""
        fixtures = {
            "sms_settings.json": [{"doctype": "SMS Settings"}],
            "email_template.json": [{"doctype": "Email Template", "name": "OTP Email"}],
        }
        fix_path = _make_fixture_dir(tmp_path, fixtures)
        app = MicroserviceApp("test-service", fixtures_path=fix_path)

        mock_import = MagicMock()
        with patch("frappe_microservice.isolation._get_import_file_by_path", return_value=mock_import):
            app._sync_fixtures_to_db()

        assert mock_import.call_count == 2

    def test_skips_non_json_files(self, tmp_path):
        """Only .json files are imported; other files are ignored."""
        fix_dir = tmp_path / "fixtures"
        fix_dir.mkdir()
        (fix_dir / "readme.txt").write_text("not a fixture")
        (fix_dir / "data.csv").write_text("a,b,c")
        (fix_dir / "real_fixture.json").write_text(json.dumps([{"doctype": "SMS Settings"}]))

        app = MicroserviceApp("test-service", fixtures_path=str(fix_dir))

        mock_import = MagicMock()
        with patch("frappe_microservice.isolation._get_import_file_by_path", return_value=mock_import):
            app._sync_fixtures_to_db()

        mock_import.assert_called_once()

    def test_error_in_one_fixture_does_not_block_others(self, tmp_path):
        """If one fixture fails, the rest still import."""
        fixtures = {
            "bad.json": [{"doctype": "SMS Settings"}],
            "good.json": [{"doctype": "Email Template", "name": "OTP Email"}],
        }
        fix_path = _make_fixture_dir(tmp_path, fixtures)
        app = MicroserviceApp("test-service", fixtures_path=fix_path)

        call_count = [0]
        def mock_import_fn(path, **kwargs):
            call_count[0] += 1
            if "bad.json" in path:
                raise Exception("bad fixture")

        with patch("frappe_microservice.isolation._get_import_file_by_path", return_value=mock_import_fn):
            with patch.object(frappe.db, "rollback", MagicMock()):
                app._sync_fixtures_to_db()

        assert call_count[0] == 2


class TestPresyncFixtures:
    """Cycle F6: presync_service_doctypes auto-discovers fixtures from SERVICE_PATH."""

    def test_presync_imports_fixtures_from_service_path(self, tmp_path):
        """presync_service_doctypes auto-discovers <SERVICE_PATH>/fixtures/."""
        fix_dir = tmp_path / "fixtures"
        fix_dir.mkdir()
        (fix_dir / "sms_settings.json").write_text(
            json.dumps([{"doctype": "SMS Settings", "sms_gateway_url": "https://test.example.com"}])
        )

        dt_dir = tmp_path / "doctypes"
        dt_dir.mkdir()

        from frappe_microservice.isolation import presync_service_doctypes

        mock_import = MagicMock()
        with patch.dict(os.environ, {
            "DOCTYPES_PATH": str(dt_dir),
            "SERVICE_PATH": str(tmp_path),
            "SERVICE_NAME": "test-service",
            "FRAPPE_SITE": "test.localhost",
            "FRAPPE_SITES_PATH": "/app/sites",
        }):
            with patch("frappe_microservice.isolation.frappe") as mock_frappe, \
                 patch("frappe_microservice.isolation._get_import_file_by_path", return_value=mock_import):
                mock_frappe.db = MagicMock()
                mock_frappe.flags = MagicMock()
                presync_service_doctypes()

        mock_import.assert_called_once()
        call_args = mock_import.call_args
        assert call_args[0][0].endswith("sms_settings.json")

    def test_presync_no_fixtures_dir_skips(self, tmp_path):
        """When <SERVICE_PATH>/fixtures/ doesn't exist, fixture sync is a no-op."""
        dt_dir = tmp_path / "doctypes"
        dt_dir.mkdir()

        from frappe_microservice.isolation import presync_service_doctypes

        mock_import = MagicMock()
        with patch.dict(os.environ, {
            "DOCTYPES_PATH": str(dt_dir),
            "SERVICE_PATH": str(tmp_path),
            "SERVICE_NAME": "test-service",
            "FRAPPE_SITE": "test.localhost",
            "FRAPPE_SITES_PATH": "/app/sites",
        }):
            with patch("frappe_microservice.isolation.frappe") as mock_frappe, \
                 patch("frappe_microservice.isolation._get_import_file_by_path", return_value=mock_import):
                mock_frappe.db = MagicMock()
                mock_frappe.flags = MagicMock()
                presync_service_doctypes()

        mock_import.assert_not_called()

    def test_presync_explicit_fixtures_path_overrides(self, tmp_path):
        """Explicit fixtures_path parameter takes precedence over SERVICE_PATH."""
        explicit_dir = tmp_path / "custom_fixtures"
        explicit_dir.mkdir()
        (explicit_dir / "template.json").write_text(
            json.dumps([{"doctype": "Email Template", "name": "OTP Email"}])
        )

        dt_dir = tmp_path / "doctypes"
        dt_dir.mkdir()

        from frappe_microservice.isolation import presync_service_doctypes

        mock_import = MagicMock()
        with patch.dict(os.environ, {
            "DOCTYPES_PATH": str(dt_dir),
            "SERVICE_PATH": str(tmp_path),
            "SERVICE_NAME": "test-service",
            "FRAPPE_SITE": "test.localhost",
            "FRAPPE_SITES_PATH": "/app/sites",
        }):
            with patch("frappe_microservice.isolation.frappe") as mock_frappe, \
                 patch("frappe_microservice.isolation._get_import_file_by_path", return_value=mock_import):
                mock_frappe.db = MagicMock()
                mock_frappe.flags = MagicMock()
                presync_service_doctypes(fixtures_path=str(explicit_dir))

        mock_import.assert_called_once()
        call_args = mock_import.call_args
        assert call_args[0][0].endswith("template.json")
