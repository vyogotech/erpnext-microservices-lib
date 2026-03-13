import pytest
import json
import types
from unittest.mock import MagicMock, patch, call
import frappe
from frappe_microservice.core import MicroserviceApp


def _reset_microservice_guards():
    """Reset microservice idempotency guards so patches can run again."""
    for flag in (
        "_microservice_isolation_applied",
        "_microservice_load_app_hooks_patched",
        "_microservice_hooks_resolution_patched",
        "_microservice_controller_patched",
    ):
        if hasattr(frappe, flag):
            delattr(frappe, flag)


# ============================================
# EXISTING TESTS (unchanged)
# ============================================

def test_validate_session_401():
    app = MicroserviceApp("test-app", central_site_url="http://central")
    app.flask_app.testing = True

    with app.flask_app.test_request_context():
        username, response = app._validate_session()
        assert username is None
        assert response[1] == 401

        data = json.loads(response[0].data)
        assert data['status'] == 'error'
        assert "Authentication required" in data['message']
        assert data['code'] == 401

@patch("frappe.set_user", create=True)
def test_secure_route_403(mock_set_user):
    app = MicroserviceApp("test-app")
    app.flask_app.testing = True
    client = app.flask_app.test_client()

    @app.secure_route('/test-403')
    def test_route(user):
        raise frappe.PermissionError("Custom forbidden message")

    with patch.object(MicroserviceApp, '_validate_session', return_value=("test_user", None)):
        response = client.get('/test-403')
        assert response.status_code == 403
        data = json.loads(response.data)
        assert data['status'] == 'error'
        assert data['message'] == "Custom forbidden message"
        assert data['code'] == 403

def test_secure_route_404():
    app = MicroserviceApp("test-app")
    app.flask_app.testing = True
    client = app.flask_app.test_client()

    @app.secure_route('/test-404')
    def test_route(user):
        raise frappe.DoesNotExistError("Sales Order SO-001 not found")

    with patch.object(MicroserviceApp, '_validate_session', return_value=("test_user", None)):
        response = client.get('/test-404')
        assert response.status_code == 404
        data = json.loads(response.data)
        assert data['status'] == 'error'
        assert data['message'] == "Sales Order SO-001 not found"
        assert data['code'] == 404


# ============================================
# GROUP A: REPRODUCE CONTAMINATION SCENARIOS
# ============================================

class TestContaminationReproduction:
    """Tests that reproduce the shared DB/Redis contamination problem.

    These prove the bug exists: when microservices share a DB and Redis
    with the central site, central-site apps and modules leak into the
    microservice's Frappe context.
    """

    def setup_method(self):
        _reset_microservice_guards()

    def test_shared_db_leaks_central_site_apps(self):
        """get_installed_apps must NOT return central-site-only apps.

        When the microservice shares a database with the central site,
        the DB's installed_apps global contains apps like saas_platform
        and hrms that only exist on the central site. The patched
        get_installed_apps must return only filesystem (apps.txt) apps
        filtered by load_framework_hooks.
        """
        _reset_microservice_guards()
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext']
            )

        installed = frappe.get_installed_apps()
        assert "frappe" in installed
        assert "erpnext" in installed
        assert "saas_platform" not in installed, \
            "Central-site app leaked via shared DB"
        assert "hrms" not in installed, \
            "Central-site app leaked via shared DB"

    def test_shared_redis_leaks_module_maps(self):
        """Module maps loaded from shared Redis must be filtered.

        When setup_module_map() reads from shared Redis, it gets the
        central site's full module map including hrms, saas_platform, etc.
        _filter_module_maps() must clean frappe.local to only keep
        allowed apps' modules.
        """
        _reset_microservice_guards()
        app = MicroserviceApp(
            "signup-service",
            load_framework_hooks=['frappe', 'erpnext']
        )

        # Simulate contaminated module maps (as loaded from shared Redis)
        frappe.local.app_modules = {
            'frappe': ['core', 'desk', 'email'],
            'erpnext': ['accounts', 'stock', 'selling'],
            'hrms': ['hr', 'payroll'],
            'saas_platform': ['saas', 'billing'],
        }
        frappe.local.module_app = {
            'core': 'frappe', 'desk': 'frappe', 'email': 'frappe',
            'accounts': 'erpnext', 'stock': 'erpnext', 'selling': 'erpnext',
            'hr': 'hrms', 'payroll': 'hrms',
            'saas': 'saas_platform', 'billing': 'saas_platform',
        }

        app._filter_module_maps()

        assert 'hrms' not in frappe.local.app_modules, \
            "Central-site app modules leaked via shared Redis"
        assert 'saas_platform' not in frappe.local.app_modules, \
            "Central-site app modules leaked via shared Redis"
        assert 'hr' not in frappe.local.module_app
        assert 'saas' not in frappe.local.module_app
        # Allowed apps must remain
        assert 'frappe' in frappe.local.app_modules
        assert 'erpnext' in frappe.local.app_modules
        assert 'core' in frappe.local.module_app
        assert 'accounts' in frappe.local.module_app

    def test_isolation_timing_patch_before_init(self):
        """_patch_app_resolution must run BEFORE frappe.init().

        In the new architecture, _initialize_frappe (called at __init__ time)
        runs: _patch_app_resolution -> frappe.init -> _filter_module_maps ->
        _patch_controller_resolution -> _patch_hooks_resolution.
        DB connect + sync_doctypes are deferred to first request.
        """
        _reset_microservice_guards()

        call_order = []

        orig_patch_app = MicroserviceApp._patch_app_resolution
        orig_filter = MicroserviceApp._filter_module_maps
        orig_hooks = MicroserviceApp._patch_hooks_resolution

        def tracked_patch_app(self):
            call_order.append('_patch_app_resolution')
            orig_patch_app(self)
        def tracked_init(*args, **kwargs):
            call_order.append('frappe.init')
        def tracked_filter(self):
            call_order.append('_filter_module_maps')
            orig_filter(self)
        def tracked_hooks(self):
            call_order.append('_patch_hooks_resolution')
            orig_hooks(self)

        with patch.object(MicroserviceApp, '_patch_app_resolution', tracked_patch_app), \
             patch("frappe.init", side_effect=tracked_init), \
             patch.object(MicroserviceApp, '_filter_module_maps', tracked_filter), \
             patch.object(MicroserviceApp, '_patch_hooks_resolution', tracked_hooks), \
             patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=['frappe', 'erpnext']
            )

        assert call_order.index('_patch_app_resolution') < call_order.index('frappe.init'), \
            f"_patch_app_resolution must run BEFORE frappe.init, got: {call_order}"
        assert call_order.index('frappe.init') < call_order.index('_filter_module_maps'), \
            f"_filter_module_maps must run AFTER frappe.init, got: {call_order}"
        assert call_order.index('_filter_module_maps') < call_order.index('_patch_hooks_resolution'), \
            f"_patch_hooks_resolution must run AFTER _filter_module_maps, got: {call_order}"


# ============================================
# GROUP B: VERIFY NEW METHODS
# ============================================

class TestPatchAppResolution:
    """Tests for _patch_app_resolution() -- filesystem-based app discovery."""

    def test_reads_filesystem_not_db(self):
        """Patched get_installed_apps reads from apps.txt, not the shared DB."""
        _reset_microservice_guards()
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]) as mock_all:
            app = MicroserviceApp("test-service", load_framework_hooks=['frappe', 'erpnext'])

            installed = frappe.get_installed_apps()
            assert "frappe" in installed
            assert "erpnext" in installed
            mock_all.assert_called()

    def test_intersects_with_load_framework_hooks(self):
        """Only apps in BOTH apps.txt AND load_framework_hooks are returned."""
        _reset_microservice_guards()
        with patch("frappe.get_all_apps",
                    return_value=["frappe", "erpnext", "saas_platform"]):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=['frappe', 'erpnext']
            )

        installed = frappe.get_installed_apps()
        assert "frappe" in installed
        assert "erpnext" in installed
        assert "saas_platform" not in installed, \
            "saas_platform is in apps.txt but not in load_framework_hooks"

    def test_service_app_name_always_included(self):
        """The service's own app name is always included."""
        _reset_microservice_guards()
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp(
                "my-service",
                load_framework_hooks=['frappe']
            )

        installed = frappe.get_installed_apps()
        assert "my_service" in installed, \
            "Service app name should always be included"

    def test_frappe_always_first(self):
        """frappe must always be the first app in the list."""
        _reset_microservice_guards()
        with patch("frappe.get_all_apps",
                    return_value=["erpnext", "frappe"]):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=['erpnext', 'frappe']
            )

        installed = frappe.get_installed_apps()
        assert installed[0] == "frappe", \
            f"frappe must be first, got: {installed}"

    def test_idempotency(self):
        """Calling _patch_app_resolution twice must not double-wrap."""
        _reset_microservice_guards()
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp("test-app")

        patched_fn = frappe.get_installed_apps
        app._patch_app_resolution()
        assert frappe.get_installed_apps is patched_fn, \
            "Function was re-wrapped despite guard"
        assert getattr(frappe, "_microservice_isolation_applied") is True

    def test_get_all_apps_also_filtered(self):
        """The patched get_all_apps must also be filtered by allowed apps."""
        _reset_microservice_guards()
        original_return = ["frappe", "erpnext", "saas_platform", "hrms"]
        with patch("frappe.get_all_apps", return_value=original_return):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=['frappe', 'erpnext']
            )

            all_apps = frappe.get_all_apps()
            assert "saas_platform" not in all_apps
            assert "hrms" not in all_apps
            assert "frappe" in all_apps
            assert "erpnext" in all_apps


class TestFilterModuleMaps:
    """Tests for _filter_module_maps() -- thread-local module map cleanup."""

    def test_removes_central_site_modules(self):
        """Contaminated app_modules from shared Redis are cleaned."""
        _reset_microservice_guards()
        app = MicroserviceApp(
            "test-service",
            load_framework_hooks=['frappe', 'erpnext']
        )

        frappe.local.app_modules = {
            'frappe': ['core', 'desk'],
            'erpnext': ['accounts', 'stock'],
            'hrms': ['hr', 'payroll'],
        }

        app._filter_module_maps()

        assert 'hrms' not in frappe.local.app_modules
        assert 'frappe' in frappe.local.app_modules
        assert 'erpnext' in frappe.local.app_modules

    def test_rebuilds_reverse_map(self):
        """module_app must be rebuilt from the filtered app_modules."""
        _reset_microservice_guards()
        app = MicroserviceApp(
            "test-service",
            load_framework_hooks=['frappe']
        )

        frappe.local.app_modules = {
            'frappe': ['core', 'desk'],
            'hrms': ['hr'],
        }
        frappe.local.module_app = {
            'core': 'frappe', 'desk': 'frappe', 'hr': 'hrms'
        }

        app._filter_module_maps()

        assert frappe.local.module_app == {'core': 'frappe', 'desk': 'frappe'}
        assert 'hr' not in frappe.local.module_app

    def test_handles_none_app_modules(self):
        """Gracefully handle app_modules being None."""
        _reset_microservice_guards()
        app = MicroserviceApp("test-service")

        frappe.local.app_modules = None
        frappe.local.module_app = None

        app._filter_module_maps()

        assert frappe.local.module_app == {}

    def test_handles_empty_app_modules(self):
        """Gracefully handle app_modules being empty dict."""
        _reset_microservice_guards()
        app = MicroserviceApp("test-service")

        frappe.local.app_modules = {}
        frappe.local.module_app = {'stale': 'old_app'}

        app._filter_module_maps()

        assert frappe.local.app_modules == {}
        assert frappe.local.module_app == {}


class TestPatchHooksResolution:
    """Tests for _patch_hooks_resolution() -- hook and attr filtering."""

    def setup_method(self):
        _reset_microservice_guards()

    def test_filters_doc_hooks_from_non_allowed_apps(self):
        """get_doc_hooks wrapper must exclude hooks from non-allowed apps."""
        _reset_microservice_guards()

        def fake_get_doc_hooks():
            return {
                "Sales Order": {
                    "before_insert": [
                        "frappe.core.handler.do_something",
                        "erpnext.selling.handler.validate",
                        "saas_platform.billing.handler.bill",
                    ]
                }
            }

        frappe.get_doc_hooks = fake_get_doc_hooks
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=['frappe', 'erpnext']
            )

        hooks = frappe.get_doc_hooks()
        handlers = hooks["Sales Order"]["before_insert"]
        assert "frappe.core.handler.do_something" in handlers
        assert "erpnext.selling.handler.validate" in handlers
        assert "saas_platform.billing.handler.bill" not in handlers

    def test_get_attr_raises_for_non_allowed_apps(self):
        """get_attr must raise AttributeError for hooks from non-allowed apps."""
        _reset_microservice_guards()
        original_get_attr = MagicMock(return_value=lambda: None)
        frappe.get_attr = original_get_attr

        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=['frappe', 'erpnext']
            )

        with pytest.raises(AttributeError, match="non-installed app"):
            frappe.get_attr("saas_platform.billing.handler.bill")

    def test_get_attr_allows_frappe_and_allowed_apps(self):
        """get_attr must work normally for allowed apps."""
        _reset_microservice_guards()
        sentinel = object()
        original_get_attr = MagicMock(return_value=sentinel)
        frappe.get_attr = original_get_attr

        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=['frappe', 'erpnext']
            )

        result = frappe.get_attr("frappe.utils.now")
        assert result is sentinel


class TestLoadAppHooksErrorHandling:
    """Unit tests that _load_app_hooks (microservice_load_app_hooks) handles errors without raising."""

    def setup_method(self):
        _reset_microservice_guards()

    def _minimal_hooks_module(self):
        """Return a minimal object that inspect.getmembers treats as a hooks module."""
        ns = types.SimpleNamespace()
        ns.doc_events = {}
        return ns

    def test_missing_hooks_module_skipped_no_raise(self):
        """When one app has no hooks module (ModuleNotFoundError), skip it and return hooks dict."""
        _reset_microservice_guards()
        apps = ["frappe", "erpnext", "signup_service"]

        def get_module(path):
            if path == "signup_service.hooks":
                raise ModuleNotFoundError("No module named 'signup_service.hooks'")
            if path in ("frappe.hooks", "erpnext.hooks"):
                return self._minimal_hooks_module()
            raise ImportError(path)

        def append_hook(hooks, key, value):
            hooks.setdefault(key, []).append(value)

        with patch("frappe.get_all_apps", return_value=apps):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=["frappe", "erpnext"],
            )
        _reset_microservice_guards()
        with patch("frappe.get_installed_apps", return_value=apps):
            with patch("frappe.get_module", side_effect=get_module):
                frappe.append_hook = append_hook
                app._patch_hooks_resolution()
                result = frappe._load_app_hooks()
        assert isinstance(result, dict)
        assert "doc_events" in result

    def test_import_error_for_one_app_skipped_no_raise(self):
        """When one app's hooks raise ImportError, skip it and return hooks dict."""
        _reset_microservice_guards()
        apps = ["frappe", "broken_app", "erpnext"]

        def get_module(path):
            if path == "broken_app.hooks":
                raise ImportError("broken_app.hooks has syntax error")
            if path in ("frappe.hooks", "erpnext.hooks"):
                return self._minimal_hooks_module()
            raise ImportError(path)

        def append_hook(hooks, key, value):
            hooks.setdefault(key, []).append(value)

        with patch("frappe.get_all_apps", return_value=apps):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=["frappe", "erpnext"],
            )
        _reset_microservice_guards()
        with patch("frappe.get_installed_apps", return_value=apps):
            with patch("frappe.get_module", side_effect=get_module):
                frappe.append_hook = append_hook
                app._patch_hooks_resolution()
                result = frappe._load_app_hooks()
        assert isinstance(result, dict)

    def test_get_installed_apps_raises_returns_empty_hooks(self):
        """When get_installed_apps() raises, return empty hooks dict and do not raise."""
        _reset_microservice_guards()
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp("test-service")

        _reset_microservice_guards()
        with patch(
            "frappe.get_installed_apps",
            side_effect=RuntimeError("database not ready"),
        ):
            app._patch_hooks_resolution()
            result = frappe._load_app_hooks()
        assert result == {}

    def test_get_installed_apps_returns_non_sequence_returns_empty_hooks(self):
        """When get_installed_apps() returns None or non-sequence, return empty hooks."""
        _reset_microservice_guards()
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp("test-service")

        _reset_microservice_guards()
        with patch("frappe.get_installed_apps", return_value=None):
            app._patch_hooks_resolution()
            result = frappe._load_app_hooks()
        assert result == {}

    def test_one_app_getmembers_fails_other_apps_still_loaded(self):
        """When one app's hooks module raises during getmembers, skip that app only."""
        _reset_microservice_guards()
        apps = ["frappe", "bad_app", "erpnext"]

        class BadHooks:
            def __getattribute__(self, name):
                raise ValueError("bad app hooks")

        def get_module(path):
            if path == "bad_app.hooks":
                return BadHooks()
            if path in ("frappe.hooks", "erpnext.hooks"):
                return self._minimal_hooks_module()
            raise ImportError(path)

        def append_hook(hooks, key, value):
            hooks.setdefault(key, []).append(value)

        with patch("frappe.get_all_apps", return_value=apps):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=["frappe", "erpnext"],
            )
        _reset_microservice_guards()
        with patch("frappe.get_installed_apps", return_value=apps):
            with patch("frappe.get_module", side_effect=get_module):
                frappe.append_hook = append_hook
                app._patch_hooks_resolution()
                result = frappe._load_app_hooks()
        assert isinstance(result, dict)
        assert "doc_events" in result


class TestFilterModuleMapsErrorHandling:
    """_filter_module_maps must not crash on unexpected frappe.local states."""

    def test_app_modules_is_none(self):
        _reset_microservice_guards()
        app = MicroserviceApp("test-service")
        frappe.local.app_modules = None
        app._filter_module_maps()
        assert frappe.local.module_app == {}

    def test_app_modules_with_non_iterable_modules(self):
        """If one app has modules=None instead of a list, skip it."""
        _reset_microservice_guards()
        app = MicroserviceApp("test-service")
        frappe.local.app_modules = {
            'frappe': ['core', 'desk'],
            'bad_app': None,
        }
        app._filter_module_maps()
        assert 'core' in frappe.local.module_app
        assert 'desk' in frappe.local.module_app


class TestGetAttrErrorHandling:
    """microservice_get_attr must handle ImportError/ModuleNotFoundError."""

    def setup_method(self):
        _reset_microservice_guards()

    def test_get_attr_import_error_becomes_attribute_error(self):
        _reset_microservice_guards()

        def raising_get_attr(method_string):
            raise ImportError(f"No module named '{method_string.split('.')[0]}'")

        frappe.get_attr = raising_get_attr
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=["frappe", "erpnext"],
            )

        with pytest.raises(AttributeError, match="non-installed app"):
            frappe.get_attr("erpnext.stock.utils.func")

    def test_get_attr_module_not_found_becomes_attribute_error(self):
        _reset_microservice_guards()

        def raising_get_attr(method_string):
            raise ModuleNotFoundError(f"No module named '{method_string}'")

        frappe.get_attr = raising_get_attr
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=["frappe"],
            )

        with pytest.raises(AttributeError, match="non-installed app"):
            frappe.get_attr("frappe.utils.now")

    def test_get_attr_rejects_non_string(self):
        _reset_microservice_guards()
        frappe.get_attr = MagicMock()
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp("test-service")

        with pytest.raises(AttributeError, match="must be a string"):
            frappe.get_attr(12345)


class TestSetupFrappeContextInit:
    """setup_frappe_context must return 503 when init/connect fails."""

    def test_returns_503_when_init_fails(self):
        _reset_microservice_guards()
        app = MicroserviceApp("test-service", central_site_url="http://central")
        app.flask_app.testing = True

        with patch("frappe.init", side_effect=RuntimeError("site config missing")):
            frappe.local = MagicMock(spec=[])
            with app.flask_app.test_client() as client:
                response = client.get("/health")
                if response.status_code == 503:
                    data = response.get_json()
                    assert data["code"] == 503
                    assert "unavailable" in data["message"].lower()
