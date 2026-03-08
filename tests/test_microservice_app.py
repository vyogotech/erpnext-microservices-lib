import pytest
import json
from unittest.mock import MagicMock, patch, call
import frappe
from frappe_microservice.core import MicroserviceApp


def _reset_isolation_guard():
    """Helper to reset the idempotency guard between tests."""
    if hasattr(frappe, "_microservice_isolation_applied"):
        delattr(frappe, "_microservice_isolation_applied")


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

    def test_shared_db_leaks_central_site_apps(self):
        """get_installed_apps must NOT return central-site-only apps.

        When the microservice shares a database with the central site,
        the DB's installed_apps global contains apps like saas_platform
        and hrms that only exist on the central site. The patched
        get_installed_apps must return only filesystem (apps.txt) apps
        filtered by load_framework_hooks.
        """
        _reset_isolation_guard()
        app = MicroserviceApp(
            "signup-service",
            load_framework_hooks=['frappe', 'erpnext']
        )

        # Simulate: original get_all_apps reads apps.txt -> frappe, erpnext
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app._patch_app_resolution()

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
        _reset_isolation_guard()
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

        The old code ran _isolate_microservice_apps() AFTER frappe.init()
        and frappe.connect(), meaning setup_module_map() inside
        frappe.init() used unpatched functions and loaded central-site
        data. The new sequence must patch first, then init.
        """
        _reset_isolation_guard()
        app = MicroserviceApp(
            "test-service",
            load_framework_hooks=['frappe', 'erpnext']
        )

        call_order = []

        original_patch = app._patch_app_resolution
        original_filter = app._filter_module_maps
        original_hooks = app._patch_hooks_resolution

        def tracked_patch():
            call_order.append('_patch_app_resolution')
            original_patch()

        def tracked_init(*args, **kwargs):
            call_order.append('frappe.init')

        def tracked_filter():
            call_order.append('_filter_module_maps')
            original_filter()

        def tracked_connect(*args, **kwargs):
            call_order.append('frappe.connect')

        def tracked_hooks():
            call_order.append('_patch_hooks_resolution')
            original_hooks()

        with patch.object(app, '_patch_app_resolution', side_effect=tracked_patch), \
             patch("frappe.init", side_effect=tracked_init), \
             patch.object(app, '_filter_module_maps', side_effect=tracked_filter), \
             patch("frappe.connect", side_effect=tracked_connect), \
             patch.object(app, '_patch_hooks_resolution', side_effect=tracked_hooks), \
             patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):

            # Simulate needs_init = True by clearing frappe.local.site
            if hasattr(frappe, 'local'):
                frappe.local.site = None

            with app.flask_app.test_request_context():
                app.flask_app.preprocess_request()

        assert call_order.index('_patch_app_resolution') < call_order.index('frappe.init'), \
            f"_patch_app_resolution must run BEFORE frappe.init, got: {call_order}"
        assert call_order.index('frappe.init') < call_order.index('_filter_module_maps'), \
            f"_filter_module_maps must run AFTER frappe.init, got: {call_order}"
        assert call_order.index('_filter_module_maps') < call_order.index('frappe.connect'), \
            f"frappe.connect must run AFTER _filter_module_maps, got: {call_order}"
        assert call_order.index('frappe.connect') < call_order.index('_patch_hooks_resolution'), \
            f"_patch_hooks_resolution must run AFTER frappe.connect, got: {call_order}"


# ============================================
# GROUP B: VERIFY NEW METHODS
# ============================================

class TestPatchAppResolution:
    """Tests for _patch_app_resolution() -- filesystem-based app discovery."""

    def test_reads_filesystem_not_db(self):
        """Patched get_installed_apps reads from apps.txt, not the shared DB."""
        _reset_isolation_guard()
        app = MicroserviceApp("test-service", load_framework_hooks=['frappe', 'erpnext'])

        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]) as mock_all:
            app._patch_app_resolution()

            installed = frappe.get_installed_apps()
            assert "frappe" in installed
            assert "erpnext" in installed
            # The patched version should use get_all_apps (filesystem),
            # which we mocked. It should NOT call db.get_global.
            mock_all.assert_called()

    def test_intersects_with_load_framework_hooks(self):
        """Only apps in BOTH apps.txt AND load_framework_hooks are returned."""
        _reset_isolation_guard()
        app = MicroserviceApp(
            "test-service",
            load_framework_hooks=['frappe', 'erpnext']
        )

        # apps.txt has saas_platform, but load_framework_hooks doesn't
        with patch("frappe.get_all_apps",
                    return_value=["frappe", "erpnext", "saas_platform"]):
            app._patch_app_resolution()

        installed = frappe.get_installed_apps()
        assert "frappe" in installed
        assert "erpnext" in installed
        assert "saas_platform" not in installed, \
            "saas_platform is in apps.txt but not in load_framework_hooks"

    def test_service_app_name_always_included(self):
        """The service's own app name is always included."""
        _reset_isolation_guard()
        app = MicroserviceApp(
            "my-service",
            load_framework_hooks=['frappe']
        )

        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app._patch_app_resolution()

        installed = frappe.get_installed_apps()
        assert "my_service" in installed, \
            "Service app name should always be included"

    def test_frappe_always_first(self):
        """frappe must always be the first app in the list."""
        _reset_isolation_guard()
        app = MicroserviceApp(
            "test-service",
            load_framework_hooks=['erpnext', 'frappe']
        )

        with patch("frappe.get_all_apps",
                    return_value=["erpnext", "frappe"]):
            app._patch_app_resolution()

        installed = frappe.get_installed_apps()
        assert installed[0] == "frappe", \
            f"frappe must be first, got: {installed}"

    def test_idempotency(self):
        """Calling _patch_app_resolution twice must not double-wrap."""
        _reset_isolation_guard()
        app = MicroserviceApp("test-app")

        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app._patch_app_resolution()
            patched_fn = frappe.get_installed_apps

            app._patch_app_resolution()
            assert frappe.get_installed_apps is patched_fn, \
                "Function was re-wrapped despite guard"
            assert getattr(frappe, "_microservice_isolation_applied") is True

    def test_get_all_apps_also_filtered(self):
        """The patched get_all_apps must also be filtered by allowed apps."""
        _reset_isolation_guard()
        app = MicroserviceApp(
            "test-service",
            load_framework_hooks=['frappe', 'erpnext']
        )

        original_return = ["frappe", "erpnext", "saas_platform", "hrms"]
        with patch("frappe.get_all_apps", return_value=original_return):
            app._patch_app_resolution()

            # Call inside the patch context so the wrapped original works
            all_apps = frappe.get_all_apps()
            assert "saas_platform" not in all_apps
            assert "hrms" not in all_apps
            assert "frappe" in all_apps
            assert "erpnext" in all_apps


class TestFilterModuleMaps:
    """Tests for _filter_module_maps() -- thread-local module map cleanup."""

    def test_removes_central_site_modules(self):
        """Contaminated app_modules from shared Redis are cleaned."""
        _reset_isolation_guard()
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
        _reset_isolation_guard()
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
        _reset_isolation_guard()
        app = MicroserviceApp("test-service")

        frappe.local.app_modules = None
        frappe.local.module_app = None

        app._filter_module_maps()

        assert frappe.local.module_app == {}

    def test_handles_empty_app_modules(self):
        """Gracefully handle app_modules being empty dict."""
        _reset_isolation_guard()
        app = MicroserviceApp("test-service")

        frappe.local.app_modules = {}
        frappe.local.module_app = {'stale': 'old_app'}

        app._filter_module_maps()

        assert frappe.local.app_modules == {}
        assert frappe.local.module_app == {}


class TestPatchHooksResolution:
    """Tests for _patch_hooks_resolution() -- hook and attr filtering."""

    def test_filters_doc_hooks_from_non_allowed_apps(self):
        """get_doc_hooks wrapper must exclude hooks from non-allowed apps."""
        _reset_isolation_guard()
        app = MicroserviceApp(
            "test-service",
            load_framework_hooks=['frappe', 'erpnext']
        )

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

        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app._patch_app_resolution()

        frappe.get_doc_hooks = fake_get_doc_hooks
        app._patch_hooks_resolution()

        hooks = frappe.get_doc_hooks()
        handlers = hooks["Sales Order"]["before_insert"]
        assert "frappe.core.handler.do_something" in handlers
        assert "erpnext.selling.handler.validate" in handlers
        assert "saas_platform.billing.handler.bill" not in handlers

    def test_get_attr_raises_for_non_allowed_apps(self):
        """get_attr must raise AttributeError for hooks from non-allowed apps."""
        _reset_isolation_guard()
        app = MicroserviceApp(
            "test-service",
            load_framework_hooks=['frappe', 'erpnext']
        )

        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app._patch_app_resolution()

        original_get_attr = MagicMock(return_value=lambda: None)
        frappe.get_attr = original_get_attr
        app._patch_hooks_resolution()

        with pytest.raises(AttributeError, match="non-installed app"):
            frappe.get_attr("saas_platform.billing.handler.bill")

    def test_get_attr_allows_frappe_and_allowed_apps(self):
        """get_attr must work normally for allowed apps."""
        _reset_isolation_guard()
        app = MicroserviceApp(
            "test-service",
            load_framework_hooks=['frappe', 'erpnext']
        )

        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app._patch_app_resolution()

        sentinel = object()
        original_get_attr = MagicMock(return_value=sentinel)
        frappe.get_attr = original_get_attr
        app._patch_hooks_resolution()

        result = frappe.get_attr("frappe.utils.now")
        assert result is sentinel
