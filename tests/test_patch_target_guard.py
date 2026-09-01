"""
Guard tests that verify unittest.mock.patch() targets are effective.

Python's mock.patch("mod.symbol") silently does nothing when the target
module is not the one where the symbol is actually looked up at runtime.
After modularization (splitting core.py into app.py, auth.py, tenant.py, etc.),
patch targets must point to the module that *imports and uses* the symbol,
not the re-export shim (core.py).

If any of these tests fail, it means a patch target has become stale --
likely because the import was moved to a different module.
"""

from unittest.mock import patch, MagicMock

SENTINEL = "GUARD_SENTINEL_VALUE_42"


class TestPatchTargetEffectiveness:
    """Every critical patch target used across the test suite is verified here."""

    def test_app_get_user_tenant_id_target(self):
        """
        secure_route calls get_user_tenant_id from app.py's namespace.
        Tests in test_microservice_auth.py patch 'frappe_microservice.app.get_user_tenant_id'.
        """
        with patch('frappe_microservice.app.get_user_tenant_id', return_value=SENTINEL) as mock_fn:
            from frappe_microservice import app as app_module
            result = app_module.get_user_tenant_id("test@test.com")
            assert result == SENTINEL, (
                f"Patch target 'frappe_microservice.app.get_user_tenant_id' is stale. "
                f"Expected sentinel '{SENTINEL}', got '{result}'. "
                f"Check where app.py imports get_user_tenant_id from."
            )
            mock_fn.assert_called_once_with("test@test.com")

    def test_app_create_site_config_target(self):
        """
        MicroserviceApp.run() calls create_site_config from app.py's namespace.
        Tests in test_additional_coverage.py patch 'frappe_microservice.app.create_site_config'.
        """
        with patch('frappe_microservice.app.create_site_config', return_value=SENTINEL) as mock_fn:
            from frappe_microservice import app as app_module
            result = app_module.create_site_config()
            assert result == SENTINEL, (
                f"Patch target 'frappe_microservice.app.create_site_config' is stale. "
                f"Check where app.py imports create_site_config from."
            )
            mock_fn.assert_called_once()

    def test_app_swagger_target(self):
        """
        MicroserviceApp.__init__ reads Swagger from app.py's namespace.
        Tests in test_swagger.py and test_additional_coverage.py patch 'frappe_microservice.app.Swagger'.
        """
        with patch('frappe_microservice.app.Swagger', SENTINEL):
            from frappe_microservice import app as app_module
            assert app_module.Swagger == SENTINEL, (
                f"Patch target 'frappe_microservice.app.Swagger' is stale. "
                f"Check where app.py imports Swagger from."
            )

    def test_auth_request_target(self):
        """
        AuthMixin._validate_session reads request from auth.py's namespace.
        Tests in test_additional_coverage.py patch 'frappe_microservice.auth.request'.
        """
        mock_request = MagicMock()
        with patch('frappe_microservice.auth.request', mock_request):
            from frappe_microservice import auth as auth_module
            assert auth_module.request is mock_request, (
                f"Patch target 'frappe_microservice.auth.request' is stale. "
                f"Check where auth.py imports request from."
            )

    def test_core_reexport_is_not_runtime_target(self):
        """
        Prove that patching the re-export shim does NOT affect app.py's namespace.
        This is the exact failure mode we're guarding against.

        If this test ever FAILS, it means app.py started importing from core.py
        instead of directly from the source module, and all patch targets in the
        test suite would need to be changed back to core.*.
        """
        with patch('frappe_microservice.core.get_user_tenant_id', return_value=SENTINEL):
            from frappe_microservice import app as app_module
            # Calling the function from app's namespace should NOT return the sentinel,
            # because app.py imports from tenant.py, not core.py.
            # If the mock DID take effect here, it means the isolation is broken.
            try:
                result = app_module.get_user_tenant_id("nobody@test.com")
            except Exception:
                result = "EXCEPTION_AS_EXPECTED"

            assert result != SENTINEL, (
                "CRITICAL: patching core.py affected app.py's namespace! "
                "This means app.py is importing from core.py instead of tenant.py. "
                "All patch targets in the test suite need updating."
            )

    def test_controller_frappe_throw_target(self):
        """
        DocumentController.throw calls frappe.throw from controller.py's namespace.
        Tests in test_controllers.py patch 'frappe_microservice.controller.frappe.throw'.
        """
        with patch('frappe_microservice.controller.frappe') as mock_frappe:
            from frappe_microservice import controller as ctrl_module
            assert ctrl_module.frappe is mock_frappe, (
                f"Patch target 'frappe_microservice.controller.frappe' is stale."
            )

    def test_import_file_by_path_target(self):
        """
        Doctype sync resolves
        frappe.modules.import_file.import_file_by_path via
        isolation._get_import_file_by_path. Guard the import path.

        NOTE: patch by OBJECT (the sys.modules entry), not by string target.
        String resolution is version-dependent: Python 3.12+ mock uses
        pkgutil.resolve_name (hits sys.modules — same module the code imports
        from), but <=3.11 walks getattr('frappe').modules.import_file, which on
        a MagicMock'd frappe auto-creates a DIFFERENT child mock than the
        sys.modules entry — the sentinel then lands where the code never looks
        and the test fails on 3.10 CI while passing on 3.12 locally.
        """
        import sys
        import_file_mod = sys.modules['frappe.modules.import_file']
        with patch.object(import_file_mod, 'import_file_by_path', SENTINEL):
            from frappe_microservice.isolation import _get_import_file_by_path
            assert _get_import_file_by_path() == SENTINEL, (
                "import_file_by_path patch target is stale — check "
                "isolation._get_import_file_by_path import path."
            )

    def test_db_has_column_and_db_type_referenced(self):
        """
        Doctype sync references frappe.db.has_column / frappe.db.table_exists and
        frappe.db.db_type. Guard that these attributes are accessed off frappe.db.
        """
        import frappe
        # These are the attributes the sync code depends on; ensure they are
        # addressable on frappe.db (real Frappe provides them on the Database).
        for attr in ("has_column", "table_exists", "db_type"):
            assert hasattr(frappe.db, attr), (
                f"frappe.db.{attr} missing — doctype sync depends on it."
            )

    def test_logger_create_handler_patch_target(self, monkeypatch):
        """
        patch_logger_fallback wraps
        frappe.utils.logger.create_handler. If Frappe renames/moves that
        function this guard fails so the fallback patch can be fixed.
        """
        import sys
        import types
        import frappe
        from frappe_microservice.isolation import patch_logger_fallback

        fake = types.ModuleType("frappe.utils.logger")
        sentinel_calls = {}

        def create_handler(module, site=None, *a, **k):
            sentinel_calls["called"] = True
            return ["original"]

        fake.create_handler = create_handler
        monkeypatch.setitem(sys.modules, "frappe.utils.logger", fake)
        monkeypatch.setattr(sys.modules["frappe.utils"], "logger", fake, raising=False)

        patch_logger_fallback()

        assert fake.create_handler is not create_handler, (
            "patch_logger_fallback did not replace "
            "frappe.utils.logger.create_handler — patch target is stale."
        )
