"""
End-to-End Simulation Tests for Microservice App Isolation

These tests simulate realistic scenarios where microservices share a database
and Redis cache with a central Frappe site, reproducing the contamination
problems that occur in production K8s deployments and verifying the fix.

Scenarios covered:
- Full initialization lifecycle under contamination
- Multiple sequential HTTP requests with persistent isolation
- Multiple independent microservices sharing DB/Redis
- Redis cache round-trip contamination
- Service restart with stale cache
- Hooks loading under contamination
- Edge cases: empty apps.txt, missing modules, overlapping configs
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from copy import deepcopy

import frappe
from frappe_microservice.core import MicroserviceApp


def _reset_isolation():
    """Reset all isolation state between tests."""
    for flag in (
        "_microservice_isolation_applied",
        "_microservice_load_app_hooks_patched",
        "_microservice_hooks_resolution_patched",
        "_microservice_controller_patched",
    ):
        if hasattr(frappe, flag):
            delattr(frappe, flag)


CENTRAL_SITE_APP_MODULES = {
    'frappe': ['core', 'desk', 'email', 'website', 'printing', 'contacts'],
    'erpnext': ['accounts', 'stock', 'selling', 'buying', 'manufacturing'],
    'hrms': ['hr', 'payroll', 'attendance'],
    'saas_platform': ['saas', 'billing', 'subscriptions', 'tenant_manager'],
    'insights': ['insights_core', 'dashboards'],
}

CENTRAL_SITE_MODULE_APP = {}
for _app, _modules in CENTRAL_SITE_APP_MODULES.items():
    for _mod in _modules:
        CENTRAL_SITE_MODULE_APP[_mod] = _app

CENTRAL_SITE_INSTALLED_APPS = [
    'frappe', 'erpnext', 'hrms', 'saas_platform', 'insights'
]


class TestFullLifecycleSimulation:
    """Simulate complete before_request lifecycle under realistic contamination."""

    def test_signup_service_full_init_with_contaminated_redis(self):
        """
        Simulate: signup-service starts in K8s, shared Redis has central
        site's full module map cached. Verify that after full init lifecycle
        the service only sees frappe + erpnext + signup_service.
        """
        _reset_isolation()

        contaminated_modules = deepcopy(CENTRAL_SITE_APP_MODULES)
        contaminated_module_app = deepcopy(CENTRAL_SITE_MODULE_APP)

        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
                frappe_site="dev.localhost",
            )

        frappe.local.app_modules = deepcopy(contaminated_modules)
        frappe.local.module_app = deepcopy(contaminated_module_app)
        app._filter_module_maps()

        assert set(frappe.local.app_modules.keys()) == {'frappe', 'erpnext'}
        assert 'hrms' not in frappe.local.app_modules
        assert 'saas_platform' not in frappe.local.app_modules
        assert 'insights' not in frappe.local.app_modules

        for mod in ['hr', 'payroll', 'saas', 'billing', 'insights_core']:
            assert mod not in frappe.local.module_app

        for mod in ['core', 'desk', 'accounts', 'stock']:
            assert mod in frappe.local.module_app

        installed = frappe.get_installed_apps()
        assert installed[0] == 'frappe'
        assert 'erpnext' in installed
        assert 'signup_service' in installed
        assert 'hrms' not in installed
        assert 'saas_platform' not in installed

    def test_order_service_full_init_with_different_hooks(self):
        """
        Simulate: order-service with load_framework_hooks=['frappe', 'erpnext']
        but apps.txt only has frappe. Verify erpnext is not force-loaded.
        """
        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp(
                "order-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        installed = frappe.get_installed_apps()
        assert 'frappe' in installed
        assert 'order_service' in installed
        assert 'erpnext' not in installed, \
            "erpnext is in load_framework_hooks but not in apps.txt"

    def test_full_init_sequence_via_before_request(self):
        """
        End-to-end: trigger before_request and verify the entire init
        sequence produces correct isolated state.
        """
        _reset_isolation()
        app = MicroserviceApp(
            "signup-service",
            load_framework_hooks=['frappe', 'erpnext'],
        )

        contaminated = deepcopy(CENTRAL_SITE_APP_MODULES)

        def fake_init(site=None, sites_path=None):
            frappe.local.site = site
            frappe.local.app_modules = deepcopy(contaminated)
            frappe.local.module_app = deepcopy(CENTRAL_SITE_MODULE_APP)
            frappe.local.conf = MagicMock()
            frappe.local.form_dict = frappe._dict()

        def fake_connect(**kwargs):
            pass

        frappe.local.site = None

        with patch("frappe.init", side_effect=fake_init), \
             patch("frappe.connect", side_effect=fake_connect), \
             patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):

            with app.flask_app.test_request_context():
                app.flask_app.preprocess_request()

            assert 'hrms' not in frappe.local.app_modules
            assert 'saas_platform' not in frappe.local.app_modules
            assert 'hr' not in frappe.local.module_app

            installed = frappe.get_installed_apps()
            assert 'hrms' not in installed
            assert 'saas_platform' not in installed


class TestMultiRequestIsolation:
    """Verify isolation persists correctly across multiple sequential requests."""

    def test_isolation_holds_across_three_requests(self):
        """
        Three sequential requests must all see the same isolated app list,
        even as frappe.local state resets between requests.
        """
        _reset_isolation()
        app = MicroserviceApp(
            "signup-service",
            load_framework_hooks=['frappe', 'erpnext'],
        )

        request_results = []

        def fake_init(site=None, sites_path=None):
            frappe.local.site = site
            frappe.local.app_modules = deepcopy(CENTRAL_SITE_APP_MODULES)
            frappe.local.module_app = deepcopy(CENTRAL_SITE_MODULE_APP)
            frappe.local.conf = MagicMock()
            frappe.local.form_dict = frappe._dict()

        frappe.local.site = None

        with patch("frappe.init", side_effect=fake_init), \
             patch("frappe.connect"), \
             patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):

            for i in range(3):
                with app.flask_app.test_request_context():
                    if i > 0:
                        frappe.local.site = None
                    app.flask_app.preprocess_request()

                    result = {
                        'installed': frappe.get_installed_apps(),
                        'app_modules_keys': set(frappe.local.app_modules.keys()),
                        'module_app_keys': set(frappe.local.module_app.keys()),
                    }
                    request_results.append(result)

        for i, r in enumerate(request_results):
            assert 'hrms' not in r['installed'], f"Request {i}: hrms leaked"
            assert 'saas_platform' not in r['installed'], f"Request {i}: saas_platform leaked"
            assert 'hrms' not in r['app_modules_keys'], f"Request {i}: hrms in module map"

        assert request_results[0]['installed'] == request_results[1]['installed']
        assert request_results[1]['installed'] == request_results[2]['installed']

    def test_health_endpoint_works_after_isolation(self):
        """
        /health must return 200 regardless of isolation state.
        """
        _reset_isolation()
        app = MicroserviceApp(
            "signup-service",
            load_framework_hooks=['frappe', 'erpnext'],
        )
        app.flask_app.testing = True
        client = app.flask_app.test_client()

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json
        assert data["service"] == "signup-service"
        assert data["status"] == "healthy"


class TestMultiServiceSimulation:
    """
    Simulate two different microservices (signup + order) that share
    the same DB and Redis, verifying they each get correct isolation.
    """

    def test_two_services_different_framework_hooks(self):
        """
        signup-service: load_framework_hooks=['frappe', 'erpnext']
        order-service: load_framework_hooks=['frappe']

        Both share the same contaminated module map from Redis.
        Each must see only its own allowed set.
        """
        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            signup_app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        signup_installed = frappe.get_installed_apps()
        assert 'frappe' in signup_installed
        assert 'erpnext' in signup_installed
        assert 'signup_service' in signup_installed

        frappe.local.app_modules = deepcopy(CENTRAL_SITE_APP_MODULES)
        signup_app._filter_module_maps()
        signup_module_keys = set(frappe.local.app_modules.keys())
        assert signup_module_keys == {'frappe', 'erpnext'}

        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            order_app = MicroserviceApp(
                "order-service",
                load_framework_hooks=['frappe'],
            )

        order_installed = frappe.get_installed_apps()
        assert 'frappe' in order_installed
        assert 'order_service' in order_installed
        assert 'erpnext' not in order_installed

        frappe.local.app_modules = deepcopy(CENTRAL_SITE_APP_MODULES)
        order_app._filter_module_maps()
        order_module_keys = set(frappe.local.app_modules.keys())
        assert order_module_keys == {'frappe'}


class TestRedisCacheContamination:
    """
    Simulate the specific Redis cache contamination path:
    central site writes app_modules to Redis -> microservice reads it.
    """

    def test_stale_redis_cache_is_cleaned_by_filter(self):
        """
        Scenario: Central site updated Redis with 5 apps' modules.
        Microservice calls frappe.init() -> setup_module_map() reads
        from Redis -> gets all 5 apps. _filter_module_maps() must
        strip the extra 3.
        """
        _reset_isolation()
        app = MicroserviceApp(
            "signup-service",
            load_framework_hooks=['frappe', 'erpnext'],
        )

        frappe.local.app_modules = deepcopy(CENTRAL_SITE_APP_MODULES)
        frappe.local.module_app = deepcopy(CENTRAL_SITE_MODULE_APP)

        original_count = len(frappe.local.app_modules)
        assert original_count == 5

        app._filter_module_maps()

        assert len(frappe.local.app_modules) == 2
        assert set(frappe.local.app_modules.keys()) == {'frappe', 'erpnext'}

        expected_modules = set()
        for mods in CENTRAL_SITE_APP_MODULES.values():
            for m in mods:
                if CENTRAL_SITE_MODULE_APP[m] in ('frappe', 'erpnext'):
                    expected_modules.add(m)

        assert set(frappe.local.module_app.keys()) == expected_modules

    def test_redis_cache_with_new_central_app_added_after_deploy(self):
        """
        Scenario: After microservice deployment, admin installs a new app
        on central site (e.g., 'crm'). Redis cache now includes 'crm'.
        Microservice must still be isolated.
        """
        _reset_isolation()
        app = MicroserviceApp(
            "signup-service",
            load_framework_hooks=['frappe', 'erpnext'],
        )

        extended_modules = deepcopy(CENTRAL_SITE_APP_MODULES)
        extended_modules['crm'] = ['crm_core', 'deals', 'leads']

        frappe.local.app_modules = extended_modules
        frappe.local.module_app = {}
        for a, mods in extended_modules.items():
            for m in mods:
                frappe.local.module_app[m] = a

        app._filter_module_maps()

        assert 'crm' not in frappe.local.app_modules
        assert 'crm_core' not in frappe.local.module_app
        assert 'deals' not in frappe.local.module_app


class TestServiceRestartSimulation:
    """
    Simulate service restart scenarios where the idempotency guard
    needs to be reset and re-applied.
    """

    def test_restart_reapplies_isolation_correctly(self):
        """
        Simulate a process restart: guard is reset, _patch_app_resolution
        is called again, and isolation must work as before.
        """
        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        first_installed = frappe.get_installed_apps()
        assert 'hrms' not in first_installed

        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app2 = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        second_installed = frappe.get_installed_apps()
        assert first_installed == second_installed

    def test_restart_with_changed_apps_txt(self):
        """
        Simulate: Between restarts, apps.txt changes (e.g., erpnext removed
        from container image). The new instance should reflect the change.
        """
        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app1 = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        v1 = frappe.get_installed_apps()
        assert 'erpnext' in v1

        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app2 = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        v2 = frappe.get_installed_apps()
        assert 'erpnext' not in v2, \
            "erpnext removed from apps.txt should not appear"
        assert 'frappe' in v2
        assert 'signup_service' in v2


class TestHooksContamination:
    """
    Full hooks resolution tests simulating realistic doc_events data
    from a contaminated central site.
    """

    def test_doc_events_from_five_apps_filtered_to_two(self):
        """
        Central site has doc_events from 5 apps. Microservice must only
        process hooks from frappe and erpnext.
        """
        _reset_isolation()

        def central_site_doc_hooks():
            return {
                "User": {
                    "after_insert": [
                        "frappe.core.doctype.user.user.on_new_user",
                        "hrms.hr.utils.on_user_create",
                        "saas_platform.tenant_manager.hooks.on_user_create",
                    ],
                    "on_update": [
                        "frappe.core.doctype.user.user.on_update",
                        "erpnext.setup.utils.on_user_update",
                        "insights.insights_core.hooks.on_user_change",
                    ],
                },
                "Sales Order": {
                    "before_submit": [
                        "erpnext.selling.doctype.sales_order.sales_order.validate",
                        "saas_platform.billing.hooks.check_subscription",
                    ],
                },
            }

        frappe.get_doc_hooks = central_site_doc_hooks
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        filtered = frappe.get_doc_hooks()

        user_after_insert = filtered.get("User", {}).get("after_insert", [])
        assert "frappe.core.doctype.user.user.on_new_user" in user_after_insert
        assert "hrms.hr.utils.on_user_create" not in user_after_insert
        assert "saas_platform.tenant_manager.hooks.on_user_create" not in user_after_insert

        user_on_update = filtered.get("User", {}).get("on_update", [])
        assert "frappe.core.doctype.user.user.on_update" in user_on_update
        assert "erpnext.setup.utils.on_user_update" in user_on_update
        assert "insights.insights_core.hooks.on_user_change" not in user_on_update

        so_before_submit = filtered.get("Sales Order", {}).get("before_submit", [])
        assert "erpnext.selling.doctype.sales_order.sales_order.validate" in so_before_submit
        assert "saas_platform.billing.hooks.check_subscription" not in so_before_submit

    def test_get_attr_blocks_import_from_non_allowed_apps(self):
        """
        When Frappe tries to resolve a hook function via get_attr,
        non-allowed apps must raise AttributeError to prevent ImportError.
        """
        _reset_isolation()
        original_attr = MagicMock(return_value=lambda: None)
        frappe.get_attr = original_attr

        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        result = frappe.get_attr("frappe.utils.now")
        assert result is not None

        result = frappe.get_attr("erpnext.accounts.utils.get_balance")
        assert result is not None

        for blocked_path in [
            "hrms.hr.utils.on_user_create",
            "saas_platform.billing.hooks.check_sub",
            "insights.insights_core.hooks.track",
        ]:
            with pytest.raises(AttributeError, match="non-installed app"):
                frappe.get_attr(blocked_path)

    def test_hooks_with_no_dot_in_handler_pass_through(self):
        """Handlers without dots (edge case) should pass through unfiltered."""
        _reset_isolation()

        def doc_hooks_with_plain_handler():
            return {
                "User": {
                    "on_update": [
                        "frappe.core.on_update",
                        "plain_handler_no_dots",
                    ]
                }
            }

        frappe.get_doc_hooks = doc_hooks_with_plain_handler
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe'],
            )

        filtered = frappe.get_doc_hooks()
        handlers = filtered["User"]["on_update"]
        assert "plain_handler_no_dots" in handlers
        assert "frappe.core.on_update" in handlers

    def test_get_attr_reentrancy_depth_guard_avoids_recursion(self):
        """
        When already inside the get_attr wrapper (depth > 0), a nested call
        must delegate to the original get_attr only, avoiding RecursionError
        (e.g. when hook code or query builder triggers get_attr again).
        """
        from frappe_microservice.isolation import _get_depth, _set_depth

        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        try:
            _set_depth(1)
            result = frappe.get_attr("frappe.utils.now")
            assert callable(result)
        finally:
            _set_depth(0)

    @pytest.mark.integration
    def test_get_attr_nested_call_from_filters_hooks_no_recursion(self):
        """
        get_additional_filters_from_hooks() calls get_attr(hook)() for each
        hook. If a hook's callable triggers get_attr again (e.g. DB query
        path), the re-entrancy guard must prevent RecursionError.
        Requires real Frappe installation (not mocked).
        """

        def _hook_that_calls_get_attr_nested():
            frappe.get_attr("frappe.utils.now")
            return {}

        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe'],
            )

        import frappe.utils as utils_mod
        utils_mod._test_hook_nested = _hook_that_calls_get_attr_nested
        try:
            filter_hooks = ["frappe.utils._test_hook_nested"]
            result = frappe._dict()
            for hook in filter_hooks:
                result.update(frappe.get_attr(hook)())
            assert isinstance(result, (dict, type(frappe._dict())))
        finally:
            if hasattr(utils_mod, "_test_hook_nested"):
                del utils_mod._test_hook_nested
        _reset_isolation()


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_apps_txt_is_empty(self):
        """
        If apps.txt is empty (broken build), the service should still
        include frappe and its own app name.
        """
        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=[]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        installed = frappe.get_installed_apps()
        assert 'frappe' in installed
        assert 'signup_service' in installed
        assert installed[0] == 'frappe'

    def test_apps_txt_read_raises_exception(self):
        """
        If reading apps.txt throws an exception, graceful fallback to
        frappe + service app.
        """
        _reset_isolation()
        with patch("frappe.get_all_apps",
                    side_effect=FileNotFoundError("apps.txt not found")):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        installed = frappe.get_installed_apps()
        assert 'frappe' in installed
        assert 'signup_service' in installed

    def test_load_framework_hooks_none_mode(self):
        """
        load_framework_hooks='none' -> only frappe + service app,
        even if apps.txt has many apps.
        """
        _reset_isolation()
        with patch("frappe.get_all_apps",
                    return_value=["frappe", "erpnext", "hrms"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks='none',
            )

        installed = frappe.get_installed_apps()
        assert 'frappe' in installed
        assert 'signup_service' in installed
        assert 'erpnext' not in installed
        assert 'hrms' not in installed

    def test_service_name_with_hyphens_converted_to_underscores(self):
        """Service name signup-service -> signup_service in app list."""
        _reset_isolation()
        with patch("frappe.get_all_apps", return_value=["frappe"]):
            app = MicroserviceApp(
                "my-complex-service-name",
                load_framework_hooks=['frappe'],
            )

        installed = frappe.get_installed_apps()
        assert 'my_complex_service_name' in installed

    def test_duplicate_apps_in_apps_txt(self):
        """
        If apps.txt has duplicates, the result should not have duplicates.
        """
        _reset_isolation()
        with patch("frappe.get_all_apps",
                    return_value=["frappe", "erpnext", "frappe", "erpnext"]):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        installed = frappe.get_installed_apps()
        assert installed.count('frappe') == 1
        assert installed.count('erpnext') == 1

    def test_filter_module_maps_with_service_app_modules(self):
        """
        If the service's own app has modules in the map, they must survive
        filtering even though they may not be in the Redis-cached map.
        """
        _reset_isolation()
        app = MicroserviceApp(
            "signup-service",
            load_framework_hooks=['frappe', 'erpnext'],
        )

        modules_with_service = deepcopy(CENTRAL_SITE_APP_MODULES)
        modules_with_service['signup_service'] = ['signup', 'registration']

        frappe.local.app_modules = modules_with_service
        frappe.local.module_app = {}
        for a, mods in modules_with_service.items():
            for m in mods:
                frappe.local.module_app[m] = a

        app._filter_module_maps()

        assert 'signup_service' in frappe.local.app_modules
        assert 'signup' in frappe.local.module_app
        assert 'registration' in frappe.local.module_app
        assert frappe.local.module_app['signup'] == 'signup_service'

    def test_all_apps_filtered_leaves_at_least_frappe(self):
        """
        Even with aggressive filtering, frappe must always remain.
        """
        _reset_isolation()
        app = MicroserviceApp(
            "test-service",
            load_framework_hooks=[],
        )

        frappe.local.app_modules = deepcopy(CENTRAL_SITE_APP_MODULES)
        frappe.local.module_app = deepcopy(CENTRAL_SITE_MODULE_APP)

        app._filter_module_maps()

        assert 'frappe' in frappe.local.app_modules
        assert len(frappe.local.app_modules) == 1

    def test_get_all_apps_consistency_with_get_installed_apps(self):
        """
        Both get_all_apps() and get_installed_apps() should return
        consistent results after patching.
        """
        _reset_isolation()
        with patch("frappe.get_all_apps",
                    return_value=["frappe", "erpnext", "hrms"]):
            app = MicroserviceApp(
                "test-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

            installed = frappe.get_installed_apps()
            all_apps = frappe.get_all_apps()

        for a in installed:
            assert a in all_apps, \
                f"Installed app '{a}' missing from get_all_apps"


class TestCompleteSignupServiceScenario:
    """
    Full scenario test modeling the exact signup-service production issue:

    1. Central site has frappe, erpnext, hrms, saas_platform installed
    2. Shared DB tabDefaultValue has all 4 apps in installed_apps
    3. Shared Redis has central site's module map cached
    4. signup-service container has only frappe + erpnext in apps.txt
    5. signup-service starts, calls frappe.init() -> reads contaminated data
    6. With the fix: isolation must prevent ModuleNotFoundError
    """

    def test_production_scenario_no_module_not_found_error(self):
        """
        The original bug: setup_module_map loads hrms/saas_platform modules,
        then Frappe tries to import hooks from these apps, causing
        ModuleNotFoundError because they aren't installed in the container.

        The fix must prevent this entire chain.
        """
        _reset_isolation()

        import_calls = []
        original_get_attr = MagicMock(side_effect=lambda m: import_calls.append(m))
        frappe.get_attr = original_get_attr

        def central_doc_hooks():
            return {
                "User": {
                    "after_insert": [
                        "frappe.core.doctype.user.user.on_new_user",
                        "hrms.hr.utils.on_user_create",
                        "saas_platform.tenant_manager.hooks.on_user_create",
                    ],
                },
            }

        frappe.get_doc_hooks = central_doc_hooks
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        frappe.local.app_modules = deepcopy(CENTRAL_SITE_APP_MODULES)
        frappe.local.module_app = deepcopy(CENTRAL_SITE_MODULE_APP)
        app._filter_module_maps()

        hooks = frappe.get_doc_hooks()
        user_hooks = hooks.get("User", {}).get("after_insert", [])

        assert "hrms.hr.utils.on_user_create" not in user_hooks
        assert "saas_platform.tenant_manager.hooks.on_user_create" not in user_hooks
        assert "frappe.core.doctype.user.user.on_new_user" in user_hooks

        for hook in user_hooks:
            frappe.get_attr(hook)

        for called in import_calls:
            app_name = called.split('.')[0]
            assert app_name in ('frappe', 'erpnext', 'signup_service'), \
                f"get_attr called for non-allowed app: {called}"

    def test_production_scenario_with_hooks_and_module_maps_combined(self):
        """
        Combined check: after full init lifecycle, both module maps AND
        hooks are properly isolated.
        """
        _reset_isolation()

        def fake_doc_hooks():
            return {
                "User": {
                    "on_update": [
                        "frappe.core.user.on_update",
                        "hrms.hr.on_user_update",
                    ]
                }
            }

        sentinel = object()
        frappe.get_doc_hooks = fake_doc_hooks
        frappe.get_attr = MagicMock(return_value=sentinel)

        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        frappe.local.app_modules = deepcopy(CENTRAL_SITE_APP_MODULES)
        frappe.local.module_app = deepcopy(CENTRAL_SITE_MODULE_APP)
        app._filter_module_maps()

        assert len(frappe.local.app_modules) == 2

        hooks = frappe.get_doc_hooks()
        handlers = hooks.get("User", {}).get("on_update", [])
        assert len(handlers) == 1
        assert handlers[0] == "frappe.core.user.on_update"

        for handler in handlers:
            result = frappe.get_attr(handler)
            assert result is sentinel

        with pytest.raises(AttributeError):
            frappe.get_attr("hrms.hr.on_user_update")

    def test_production_scenario_repeated_requests(self):
        """
        Simulate the service handling 5 sequential requests, each time
        verifying isolation. This is the "soak test" equivalent.
        """
        _reset_isolation()
        app = MicroserviceApp(
            "signup-service",
            load_framework_hooks=['frappe', 'erpnext'],
        )

        def fake_init(site=None, sites_path=None):
            frappe.local.site = site
            frappe.local.app_modules = deepcopy(CENTRAL_SITE_APP_MODULES)
            frappe.local.module_app = deepcopy(CENTRAL_SITE_MODULE_APP)
            frappe.local.conf = MagicMock()
            frappe.local.form_dict = frappe._dict()

        frappe.local.site = None

        with patch("frappe.init", side_effect=fake_init), \
             patch("frappe.connect"), \
             patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):

            for request_num in range(5):
                with app.flask_app.test_request_context():
                    if request_num > 0:
                        frappe.local.site = None
                    app.flask_app.preprocess_request()

                    installed = frappe.get_installed_apps()
                    assert 'hrms' not in installed, \
                        f"Request {request_num}: hrms leaked"
                    assert 'saas_platform' not in installed, \
                        f"Request {request_num}: saas_platform leaked"
                    assert 'hrms' not in frappe.local.app_modules, \
                        f"Request {request_num}: hrms in module map"
                    assert 'hr' not in frappe.local.module_app, \
                        f"Request {request_num}: hr module leaked"


class TestAppNotInstalledErrorHandling:
    """
    Test that the fix handles the specific AppNotInstalledError that
    Frappe raises when trying to import from non-installed apps.
    """

    def test_get_attr_catches_app_not_installed_error(self):
        """
        When original get_attr raises AppNotInstalledError,
        the wrapper must convert it to AttributeError.
        """
        _reset_isolation()

        frappe.AppNotInstalledError = type(
            'AppNotInstalledError', (Exception,), {})

        def raising_get_attr(method_string):
            raise frappe.AppNotInstalledError(
                f"App not installed: {method_string}")

        frappe.get_attr = raising_get_attr
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        with pytest.raises(AttributeError, match="non-installed app"):
            frappe.get_attr("frappe.utils.now")

    def test_allowed_app_import_error_becomes_attribute_error(self):
        """
        If an allowed app's hook can't be imported, the ImportError is converted
        to AttributeError so Frappe's hook dispatcher can skip it gracefully
        instead of crashing the request.
        """
        _reset_isolation()

        def raising_get_attr(method_string):
            raise ImportError(f"No module named '{method_string}'")

        frappe.get_attr = raising_get_attr
        with patch("frappe.get_all_apps", return_value=["frappe", "erpnext"]):
            app = MicroserviceApp(
                "signup-service",
                load_framework_hooks=['frappe', 'erpnext'],
            )

        with pytest.raises(AttributeError, match="non-installed app"):
            frappe.get_attr("frappe.nonexistent.module.func")
