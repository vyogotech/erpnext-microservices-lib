"""
App isolation mixin -- prevents microservices from loading central-site apps.

When multiple microservices share a DB/Redis with the central site, Frappe can
see all installed apps and cached module/hook data from the central site. This
mixin ensures each microservice only loads apps from its own bounded context:
- _get_allowed_apps(): Set of app names (frappe + service app + load_framework_hooks).
- _patch_app_resolution(): Replace get_installed_apps/get_all_apps so they return
  only allowed apps (from filesystem apps.txt, not shared DB). Must run BEFORE frappe.init().
- _filter_module_maps(): After init, strip frappe.local.app_modules/module_app to
  allowed apps only (cleans any Redis contamination). Must run AFTER frappe.init().
- _patch_hooks_resolution(): Replace get_doc_hooks, get_attr, get_hooks (jinja),
  and get_jinja_hooks so hooks from non-allowed apps are filtered out. Must run
  AFTER frappe.connect().

Public standalone functions:
- presync_service_doctypes(): One-shot DB sync called from entrypoint before
  Gunicorn starts. Returns True on success so the entrypoint can set
  ``_DOCTYPES_PRESYNCED`` only when sync actually completed. Safe to call
  from multiple pods (idempotent).
- Also syncs fixture data (e.g. SMS Settings, Email Template) from FIXTURES_PATH.
"""

import json
import logging
import os
import threading

import frappe

from frappe_microservice.presync import (
    _sync_fixtures_from_path,
    resolve_doctype_sync_mode,
    suppress_metadata_version_reset as _suppress_metadata_version_reset,
    _sync_doctype_json_file,
)

# Back-compat re-exports: these moved to presync.py when this module was split
# up. Importers (including the test suite) still reference the old location, so
# keep the names resolvable here rather than breaking every call site.
from frappe_microservice.presync import (  # noqa: F401
    _get_import_file_by_path,
    _resolve_post_presync_callable,
    patch_logger_fallback,
    presync_service_doctypes,
    register_module_for_service,
    register_service_doctypes,
)

# Re-entrancy guard: when we're inside microservice_get_attr, nested get_attr
# (e.g. from hook code or query builder) must use the original to avoid recursion.
_get_attr_depth = threading.local()


def _get_depth():
    return getattr(_get_attr_depth, "value", 0)


def _set_depth(value):
    _get_attr_depth.value = value


def _hook_path_app_name(method_string):
    """Return the Frappe app prefix from a dotted hook path, or empty string."""
    if not isinstance(method_string, str) or "." not in method_string:
        return ""
    return method_string.split(".", 1)[0]


def _filter_jinja_hook_paths(object_paths, allowed_apps):
    """Keep only Jinja hook paths owned by apps this microservice may load."""
    filtered = []
    for obj_path in object_paths or []:
        if not isinstance(obj_path, str):
            continue
        app_name = _hook_path_app_name(obj_path)
        if app_name and app_name not in allowed_apps:
            continue
        filtered.append(obj_path)
    return filtered


def _filter_jinja_hooks_config(jinja_hooks, allowed_apps):
    """Filter ``jinja`` hook dict (methods/filters) to allowed app prefixes."""
    if not isinstance(jinja_hooks, dict):
        return jinja_hooks
    return {
        **jinja_hooks,
        "methods": _filter_jinja_hook_paths(jinja_hooks.get("methods"), allowed_apps),
        "filters": _filter_jinja_hook_paths(jinja_hooks.get("filters"), allowed_apps),
    }


_module_logger = logging.getLogger(__name__)

_SERVICE_DOCTYPES = set()


def apply_controller_patch():
    """
    Standalone function to patch frappe.model.base_document.import_controller.
    Allows microservice doctypes with custom controllers to fall back to the
    ControllerRegistry or base Document if their Python module is missing.
    """
    if getattr(frappe, "_microservice_controller_patched", False):
        return

    frappe._microservice_controller_patched = True

    original_import_controller = frappe.model.base_document.import_controller

    import logging

    _logger = logging.getLogger(__name__)

    def microservice_import_controller(doctype):
        _logger.debug(f"🔍 [Isolation] Resolution request for: {doctype}")
        try:
            return original_import_controller(doctype)
        except (ImportError, ModuleNotFoundError):
            from frappe_microservice.controller import get_controller_registry

            registry = get_controller_registry()

            reg_id = id(registry)
            keys = list(registry.list_controllers().keys())
            _logger.debug(
                "🔍 [Isolation] original_import_controller failed for %s. "
                "registry_id=%s, keys=%s",
                doctype,
                reg_id,
                keys,
            )

            # Universal fallback to ControllerRegistry
            controller_class = registry.get_controller(doctype)
            if controller_class:
                _logger.debug(
                    f"✅ [Isolation] Resolved {doctype} -> {controller_class.__name__} (id={reg_id})"
                )
                return controller_class

            # If not in registry and it should have been (known service doctype), log a warning
            if doctype in _SERVICE_DOCTYPES:
                _logger.warning(
                    f"⚠️ [Isolation] Doctype {doctype} MISSING from registry! "
                    f"registry_id={reg_id}, keys={keys}. "
                    "Falling back to Document."
                )
                return frappe.model.document.Document

            _logger.debug(
                f"❌ [Isolation] {doctype} not in registry or service_doctypes. Re-raising."
            )
            raise

    frappe.model.base_document.import_controller = microservice_import_controller


def get_allowed_apps(service_name, load_framework_hooks=None):
    """App names this microservice may load: frappe + its own app + configured hooks."""
    allowed = set(load_framework_hooks or [])
    allowed.add("frappe")
    allowed.add(service_name.replace("-", "_"))
    return allowed


def patch_app_resolution(service_name, load_framework_hooks=None, logger=None):
    """Override frappe.get_installed_apps/get_all_apps to read from apps.txt.

    MUST be called BEFORE frappe.init() so setup_module_map() uses the patched
    versions instead of reading from the shared DB. Reads apps.txt (baked into
    the image) intersected with load_framework_hooks, so it never touches the
    shared database.

    Module-level rather than only a mixin method because ``presync`` needs it
    too: presync runs in the entrypoint, before any MicroserviceApp exists, and
    called frappe.init() without this patch. frappe.connect() then walked every
    app on the site, hit one the container cannot import (tenancy_app), and
    raised — so presync logged "cannot connect to DB" and skipped, which is why
    service fixtures were never applied. See IsolationMixin._patch_app_resolution.
    """
    logger = logger or _module_logger

    if getattr(frappe, "_microservice_isolation_applied", False):
        logger.debug("Microservice app isolation already applied, skipping")
        return

    frappe._microservice_isolation_applied = True

    from frappe_microservice.cache import patch_redis_make_key

    patch_redis_make_key(service_name, logger)

    allowed_apps = get_allowed_apps(service_name, load_framework_hooks)
    service_app_name = service_name.replace("-", "_")

    original_get_all_apps = frappe.get_all_apps

    logger.info(f"Patching app resolution: allowed apps = {sorted(allowed_apps)}")

    def _service_app_is_importable(app_name):
        """Return True only if the service app exists as an importable Python module."""
        import importlib.util

        return importlib.util.find_spec(app_name) is not None

    def _deduplicated_filter(apps):
        """
        Keep only allowed apps, deduplicate, and ensure 'frappe' is first
        in the list (Frappe expects this for module resolution).
        Only adds service_app_name if it actually exists as a Python module
        — services like file-metadata-service are pure Flask apps with no
        Frappe module, so blindly appending would cause ModuleNotFoundError.
        """
        seen = set()
        filtered = []
        for a in apps:
            if a in allowed_apps and a not in seen and _service_app_is_importable(a):
                seen.add(a)
                filtered.append(a)

        if service_app_name not in seen and _service_app_is_importable(
            service_app_name
        ):
            filtered.append(service_app_name)
        if "frappe" not in seen:
            filtered.append("frappe")

        if "frappe" in filtered:
            filtered.remove("frappe")
            filtered.insert(0, "frappe")

        return filtered

    def microservice_get_installed_apps(*, _ensure_on_bench=False):
        """
        Return installed apps as seen by this microservice: call original
        get_all_apps (which reads apps.txt), then filter and deduplicate.
        """
        try:
            apps = original_get_all_apps(with_internal_apps=False)
        except Exception:
            apps = []
        return _deduplicated_filter(apps)

    def microservice_get_all_apps(with_internal_apps=True, sites_path=None):
        """Same as get_installed_apps but honours with_internal_apps/sites_path."""
        try:
            apps = original_get_all_apps(
                with_internal_apps=with_internal_apps, sites_path=sites_path
            )
        except Exception:
            apps = []
        return _deduplicated_filter(apps)

    frappe.get_installed_apps = microservice_get_installed_apps
    frappe.get_all_apps = microservice_get_all_apps

    logger.info("Microservice app resolution patched (filesystem-based)")


def patch_load_app_hooks(allowed_apps, logger=None):
    """Replace frappe._load_app_hooks so apps with no hooks module are skipped.

    Needed wherever patch_app_resolution() has put a hooks-less service app on
    the installed-apps list. MUST be called AFTER frappe.connect().
    """
    logger = logger or _module_logger

    if not getattr(frappe, "_microservice_load_app_hooks_patched", False):
        import inspect
        import types

        def microservice_load_app_hooks(app_name=None):
            hooks = {}
            try:
                apps = (
                    [app_name]
                    if app_name
                    else frappe.get_installed_apps(_ensure_on_bench=True)
                )
            except Exception as e:
                logger.warning(
                    "Failed to get installed apps for hook loading: %s. Returning empty hooks.",
                    e,
                    exc_info=True,
                )
                return hooks
            if not isinstance(apps, (list, tuple)):
                logger.warning(
                    "get_installed_apps returned non-sequence %s. Returning empty hooks.",
                    type(apps).__name__,
                )
                return hooks
            for app in apps:
                if not app or not isinstance(app, str):
                    continue
                if app not in allowed_apps:
                    logger.debug(
                        "Skipping hooks for app %r (not in allowed apps)",
                        app,
                    )
                    continue
                try:
                    app_hooks = frappe.get_module(f"{app}.hooks")
                except (ImportError, ModuleNotFoundError) as e:
                    if not getattr(frappe.local, "flags", None) or not getattr(
                        frappe.local.flags, "in_install_app", False
                    ):
                        logger.debug(
                            "Skipping hooks for app %r (no hooks module): %s",
                            app,
                            e,
                        )
                    continue
                except Exception as e:
                    logger.warning(
                        "Unexpected error loading hooks module for app %r: %s. Skipping.",
                        app,
                        e,
                        exc_info=True,
                    )
                    continue
                if app_hooks is None:
                    continue
                try:

                    def _is_valid_hook(obj):
                        return not isinstance(
                            obj, (types.ModuleType, types.FunctionType, type)
                        )

                    for key, value in inspect.getmembers(
                        app_hooks, predicate=_is_valid_hook
                    ):
                        if key.startswith("_"):
                            continue
                        try:
                            frappe.append_hook(hooks, key, value)
                        except Exception as e:
                            logger.debug(
                                "Skipping hook %r from app %r: %s",
                                key,
                                app,
                                e,
                            )
                except Exception as e:
                    logger.warning(
                        "Error reading hooks from app %r: %s. Skipping app.",
                        app,
                        e,
                        exc_info=True,
                    )
            return hooks

        frappe._load_app_hooks = microservice_load_app_hooks
        # Cached wrappers close over the original _load_app_hooks; replace them
        # so get_hooks() uses our implementation in all code paths.
        frappe._request_cached_load_app_hooks = frappe.request_cache(
            microservice_load_app_hooks
        )
        frappe._site_cached_load_app_hooks = frappe.site_cache(
            microservice_load_app_hooks
        )
        frappe._microservice_load_app_hooks_patched = True
        if hasattr(frappe, "client_cache") and frappe.client_cache:
            frappe.client_cache.delete_value("app_hooks")
        logger.info(
            "Patched _load_app_hooks to skip apps without a hooks module"
        )


class IsolationMixin:
    """Mixin for MicroserviceApp providing app/module isolation.

    Expects the host class to have: self.name, self.load_framework_hooks,
    self.logger. The app-resolution logic itself lives in the module-level
    functions above so presync (which runs before any app object exists) can
    reuse it rather than reimplementing it.
    """

    def _get_allowed_apps(self):
        return get_allowed_apps(self.name, self.load_framework_hooks)

    def _patch_app_resolution(self):
        patch_app_resolution(self.name, self.load_framework_hooks, self.logger)

    def _filter_module_maps(self):
        """
        Filter frappe.local.app_modules and rebuild frappe.local.module_app
        to remove any central-site contamination from shared Redis cache.

        MUST be called AFTER frappe.init() because setup_module_map() inside
        frappe.init() may load stale data from shared Redis.

        Safe because frappe.local is thread-local -- does not write back to
        Redis, does not affect the central site or other services.
        """
        try:
            allowed_apps = self._get_allowed_apps()

            app_modules = getattr(frappe.local, "app_modules", None)
            if app_modules and isinstance(app_modules, dict):
                original_count = len(app_modules)
                frappe.local.app_modules = {
                    app: modules
                    for app, modules in app_modules.items()
                    if app in allowed_apps
                }
                filtered_count = len(frappe.local.app_modules)
                if original_count != filtered_count:
                    self.logger.info(
                        f"Filtered module maps: {original_count} -> {filtered_count} apps "
                        f"(removed {original_count - filtered_count} central-site apps)"
                    )

            frappe.local.module_app = {}
            for app, modules in (
                getattr(frappe.local, "app_modules", None) or {}
            ).items():
                if not isinstance(modules, (list, tuple)):
                    continue
                for module in modules:
                    frappe.local.module_app[module] = app

            # Ensure service local modules mapped in _sync_service_doctypes are populated
            # for THIS thread.
            if hasattr(self, "_service_modules"):
                service_app = self.name.replace("-", "_")
                for module in self._service_modules:
                    frappe.local.module_app[module] = service_app
                    if service_app not in getattr(frappe.local, "app_modules", {}):
                        frappe.local.app_modules[service_app] = []
                    if module not in frappe.local.app_modules[service_app]:
                        frappe.local.app_modules[service_app].append(module)

        except Exception as e:
            self.logger.warning(
                "Error filtering module maps: %s. Resetting to empty.",
                e,
                exc_info=True,
            )
            frappe.local.module_app = {}

    def _patch_hooks_resolution(self):
        """
        Patch frappe.get_doc_hooks(), frappe.get_attr(), frappe._load_app_hooks,
        and frappe.utils.jinja.get_jinja_hooks so that:
        - Hooks from non-allowed apps are filtered out.
        - Apps that are in the installed list but have no Python hooks module
          (e.g. non-Frappe microservices like signup-service) are skipped instead
          of raising ModuleNotFoundError.
        MUST be called AFTER frappe.connect() because hook resolution may
        need a database connection.
        """
        allowed_apps = self._get_allowed_apps()

        patch_load_app_hooks(allowed_apps, self.logger)

        if getattr(frappe, "_microservice_hooks_resolution_patched", False):
            self.logger.debug("Hooks resolution already patched, skipping")
            self._patch_get_hooks_jinja_filter(allowed_apps)
            self._patch_jinja_hooks_resolution(allowed_apps)
            return

        frappe._microservice_hooks_resolution_patched = True

        original_get_doc_hooks = frappe.get_doc_hooks

        def microservice_get_doc_hooks():
            """
            Return doc_events hooks with handlers from non-allowed apps removed.
            Handler strings are "app.module.func"; we keep only those whose app is in allowed_apps.
            """
            try:
                all_hooks = original_get_doc_hooks()
            except Exception as e:
                self.logger.warning(
                    "original_get_doc_hooks() raised %s: %s. Returning empty hooks.",
                    type(e).__name__,
                    e,
                    exc_info=True,
                )
                return {}

            if not isinstance(all_hooks, dict):
                return {}

            filtered_hooks = {}
            for doctype, events in all_hooks.items():
                filtered_hooks[doctype] = {}
                if not isinstance(events, dict):
                    continue
                for event, handlers in events.items():
                    filtered_handlers = []
                    if not isinstance(handlers, (list, tuple)):
                        continue
                    for handler in handlers:
                        if not isinstance(handler, str):
                            continue
                        if "." in handler:
                            app_name = handler.split(".")[0]
                            if app_name in allowed_apps:
                                filtered_handlers.append(handler)
                            else:
                                self.logger.debug(
                                    f"Filtering out hook '{handler}' - "
                                    f"app '{app_name}' not in allowed apps"
                                )
                        else:
                            filtered_handlers.append(handler)
                    if filtered_handlers:
                        filtered_hooks[doctype][event] = filtered_handlers

            return filtered_hooks

        frappe.get_doc_hooks = microservice_get_doc_hooks

        original_get_attr = frappe.get_attr

        def microservice_get_attr(method_string):
            """
            Resolve a method string (e.g. 'erpnext.stock.utils.func'). If the
            app part is not in allowed_apps, raise AttributeError so the hook
            is skipped. Also catch AppNotInstalledError/ImportError and convert
            to AttributeError.
            Re-entrancy: when already inside this wrapper (e.g. hook code ran
            a DB query which called get_additional_filters_from_hooks again),
            use original_get_attr only to avoid RecursionError.
            """
            if _get_depth() > 0:
                return original_get_attr(method_string)
            if not isinstance(method_string, str):
                raise AttributeError(
                    f"method_string must be a string, got {type(method_string).__name__}"
                )
            if "." in method_string:
                app_name = method_string.split(".")[0]
                if app_name not in allowed_apps:
                    self.logger.debug(
                        f"Skipping hook '{method_string}' - "
                        f"app '{app_name}' not in allowed apps"
                    )
                    raise AttributeError(
                        f"Hook from non-installed app '{app_name}' skipped"
                    )
            try:
                _set_depth(_get_depth() + 1)
                return original_get_attr(method_string)
            except Exception as e:
                app_name = (
                    method_string.split(".")[0] if "." in method_string else "unknown"
                )

                # AppNotInstalledError specifically means "app not installed" —
                # always a legitimate skip, even for allowed apps. Guard the
                # isinstance() with a type check so a non-class attribute (e.g.
                # under a mocked frappe in tests) cannot raise TypeError here.
                _app_not_installed = getattr(frappe, "AppNotInstalledError", None)
                if isinstance(_app_not_installed, type) and isinstance(
                    e, _app_not_installed
                ):
                    self.logger.debug(
                        f"Skipping hook '{method_string}' - "
                        f"app '{app_name}' not installed: {e}"
                    )
                    raise AttributeError(
                        f"Hook from non-installed app '{app_name}' skipped"
                    ) from e

                # Only convert import failures to a skip for NON-allowed apps.
                # A broken import inside our own / an allowed app (typo'd hook
                # path, missing dependency) is a real bug — let it propagate
                # instead of being silently swallowed as a skipped hook.
                if isinstance(e, (ImportError, ModuleNotFoundError)):
                    if app_name in allowed_apps:
                        raise
                    self.logger.debug(
                        f"Skipping hook '{method_string}' - "
                        f"app '{app_name}' not installed: {e}"
                    )
                    raise AttributeError(
                        f"Hook from non-installed app '{app_name}' skipped"
                    ) from e

                raise
            finally:
                _set_depth(_get_depth() - 1)

        frappe.get_attr = microservice_get_attr
        self._patch_get_hooks_jinja_filter(allowed_apps)
        self._patch_jinja_hooks_resolution(allowed_apps)
        self.logger.info(
            f"Hooks resolution patched: allowed apps = {sorted(allowed_apps)}"
        )

    def _patch_get_hooks_jinja_filter(self, allowed_apps):
        """Filter cached ``app_hooks`` Jinja entries from central-site apps (shared Redis)."""
        if getattr(frappe, "_microservice_get_hooks_jinja_patched", False):
            self.logger.debug("get_hooks jinja filter already patched, skipping")
            return

        original_get_hooks = frappe.get_hooks

        def microservice_get_hooks(
            hook=None, default="_KEEP_DEFAULT_LIST", app_name=None
        ):
            result = original_get_hooks(hook, default, app_name)
            if hook == "jinja":
                return _filter_jinja_hooks_config(result, allowed_apps)
            if hook is None and isinstance(result, dict) and "jinja" in result:
                filtered = dict(result)
                filtered["jinja"] = _filter_jinja_hooks_config(
                    result.get("jinja"), allowed_apps
                )
                return frappe._dict(filtered)
            return result

        frappe.get_hooks = microservice_get_hooks
        frappe._microservice_get_hooks_jinja_patched = True
        self.logger.info(
            "Patched get_hooks to filter central-site jinja hooks: %s",
            sorted(allowed_apps),
        )

    def _patch_jinja_hooks_resolution(self, allowed_apps):
        """Skip Jinja hook paths from non-allowed apps (e.g. company_tracker on central site)."""
        try:
            import frappe.utils.jinja as jinja_module
        except (ImportError, ModuleNotFoundError) as e:
            self.logger.debug(
                "Skipping get_jinja_hooks patch (frappe.utils.jinja unavailable): %s",
                e,
            )
            return

        if getattr(jinja_module, "_microservice_jinja_hooks_patched", False):
            self.logger.debug("Jinja hooks resolution already patched, skipping")
            return

        def microservice_get_jinja_hooks():
            if not getattr(frappe.local, "site", None):
                return (None, None)

            from inspect import getmembers, isfunction
            from types import FunctionType, ModuleType

            values = _filter_jinja_hooks_config(
                frappe.get_hooks("jinja") or {}, allowed_apps
            )
            if not isinstance(values, dict):
                return ({}, {})

            def get_obj_dict_from_paths(object_paths):
                out = {}
                for obj_path in _filter_jinja_hook_paths(object_paths, allowed_apps):
                    try:
                        try:
                            obj = frappe.get_module(obj_path)
                        except ModuleNotFoundError:
                            obj = frappe.get_attr(obj_path)

                        if isinstance(obj, ModuleType):
                            functions = getmembers(obj, isfunction)
                            for function_name, function in functions:
                                out[function_name] = function
                        elif isinstance(obj, FunctionType):
                            out[obj.__name__] = obj
                    except AttributeError as e:
                        if "skipped" in str(e).lower():
                            self.logger.debug(
                                "Skipping unresolved jinja hook %r: %s",
                                obj_path,
                                e,
                            )
                            continue
                        raise
                    except (ImportError, ModuleNotFoundError) as e:
                        self.logger.debug(
                            "Skipping unresolved jinja hook %r: %s",
                            obj_path,
                            e,
                        )
                        continue
                return out

            methods = values.get("methods", [])
            filters = values.get("filters", [])
            method_dict = get_obj_dict_from_paths(methods)
            filter_dict = get_obj_dict_from_paths(filters)
            return method_dict, filter_dict

        jinja_module.get_jinja_hooks = microservice_get_jinja_hooks
        jinja_module._microservice_jinja_hooks_patched = True
        self.logger.info(
            "Patched get_jinja_hooks to filter non-allowed apps: %s",
            sorted(allowed_apps),
        )

    def _register_module_for_service(self, module_str, service_app):
        """Register a module -> service_app in frappe.local (in-memory only)."""
        if not module_str:
            return
        if not hasattr(frappe.local, "module_app"):
            frappe.local.module_app = {}
        if not hasattr(frappe.local, "app_modules"):
            frappe.local.app_modules = {}
        if service_app not in frappe.local.app_modules:
            frappe.local.app_modules[service_app] = []

        scrubbed = module_str.lower().replace(" ", "_")
        for key in (module_str, scrubbed):
            if key not in frappe.local.module_app:
                frappe.local.module_app[key] = service_app
            if key not in frappe.local.app_modules[service_app]:
                frappe.local.app_modules[service_app].append(key)

    def _register_service_doctypes_from_json(self):
        """
        Scan doctypes_path for DocType JSONs and register doctype names and
        module mappings in memory only (no DB). Every worker must run this
        so frappe.local.module_app and _service_doctype_names are populated.
        """
        if not getattr(self, "doctypes_path", None):
            return
        from pathlib import Path

        if not os.path.isdir(self.doctypes_path):
            self.logger.warning(f"DocTypes directory not found: {self.doctypes_path}")
            return

        service_app = self.name.replace("-", "_")
        doctypes_dir = Path(self.doctypes_path)
        for json_path in doctypes_dir.glob("*/*.json"):
            try:
                with open(json_path, "r") as f:
                    doc = json.load(f)
                doc_name = doc.get("name")
                if doc_name:
                    self._service_doctype_names.add(doc_name)
                    _SERVICE_DOCTYPES.add(doc_name)
                    self.logger.debug(f"Found service DocType: {doc_name}")
                module = doc.get("module")
                self._register_module_for_service(module, service_app)
            except Exception as e:
                self.logger.error(
                    f"Error reading DocType JSON at {json_path}: {e}",
                    exc_info=True,
                )

    def _sync_service_doctypes_to_db(self):
        """
        Ensure Module Def and DocType records exist in the DB (create if missing).
        Should run only once per site+service across all workers; caller uses
        a file lock + sentinel. Uses frappe.db so must be called with DB connected.
        """
        if not getattr(self, "doctypes_path", None):
            return
        from pathlib import Path

        if not os.path.isdir(self.doctypes_path):
            return

        service_app = self.name.replace("-", "_")
        doctypes_dir = Path(self.doctypes_path)
        imported_any = False
        sync_mode = resolve_doctype_sync_mode(self.logger)

        def _ensure_module_def(module_name, app_name, _logger):
            self._ensure_module_def(module_name, app_name)

        for json_path in doctypes_dir.glob("*/*.json"):
            try:
                with open(json_path, "r") as f:
                    doc = json.load(f)
                doc_name = doc.get("name")

                # Keep the module mapping for already-present doctypes in sync.
                if doc_name and frappe.db.exists("DocType", doc_name):
                    try:
                        existing_module = frappe.db.get_value(
                            "DocType", doc_name, "module"
                        )
                        self._register_module_for_service(existing_module, service_app)
                    except Exception:
                        pass

                # create-if-missing (create mode) or create+update (migrate mode),
                # ensuring Module Def + tenant_id column. See doctype-startup-sync.md.
                if _sync_doctype_json_file(
                    json_path,
                    service_app,
                    sync_mode,
                    self.logger,
                    ensure_module_def=_ensure_module_def,
                ):
                    imported_any = True
            except Exception as e:
                self.logger.error(
                    f"Error syncing DocType at {json_path}: {e}",
                    exc_info=True,
                )
                try:
                    frappe.db.rollback()
                except Exception:
                    pass

        if imported_any:
            frappe.db.commit()

    def _sync_service_doctypes(self):
        """
        Full sync: register from JSON (in-memory) then sync to DB.
        Call this when you want both; for Gunicorn, use _register_service_doctypes_from_json
        in every worker and _sync_service_doctypes_to_db once under lock.
        """
        self._register_service_doctypes_from_json()
        self._sync_service_doctypes_to_db()

    def _sync_fixtures_to_db(self):
        """
        Import fixture JSONs (e.g. SMS Settings, Email Templates) into the DB.
        Delegates to _sync_fixtures_from_path which handles per-file error isolation.
        """
        logger = getattr(self, "logger", logging.getLogger("presync"))
        _sync_fixtures_from_path(getattr(self, "fixtures_path", None), logger)

    def _import_doc_without_cache_flush(self, doc: dict):
        """
        Run frappe.modules.import_file.import_doc while suppressing the
        reset_metadata_version() call that DocType.after_save triggers.

        Background: when a DocType is saved, frappe.clear_cache(doctype=name)
        calls reset_metadata_version(), which bumps a hash in shared Redis.  The
        central site detects the version bump on its next request and reloads all
        hooks from scratch.  If the central site's Frappe has a hook pointing to a
        function that was removed/renamed (e.g. frappe.integrations.oauth2.
        set_cors_for_privileged_requests), that reload raises an AttributeError.

        We suppress only reset_metadata_version — the DocType-specific cache keys
        are still cleared so the service itself stays consistent.
        """
        with _suppress_metadata_version_reset():
            frappe.modules.import_file.import_doc(
                doc,
                ignore_version=True,
                reset_permissions=False,
            )

    def _ensure_module_def(self, module_name: str, app_name: str):
        """
        Insert a Module Def record for `module_name` owned by `app_name` if one
        doesn't already exist. This tells Frappe (and the shared central site) that
        this module belongs to the service app, not to 'frappe' — preventing the
        central site from trying to load controller classes it doesn't have.

        Uses ignore_permissions + ignore_mandatory so it works without an Admin
        session, and catches all exceptions silently so a failure here never blocks
        the service from starting.
        """
        try:
            if not frappe.db.table_exists("Module Def"):
                return
            if frappe.db.exists("Module Def", module_name):
                return
            m = frappe.get_doc(
                {
                    "doctype": "Module Def",
                    "module_name": module_name,
                    "app_name": app_name,
                }
            )
            m.flags.ignore_permissions = True
            m.flags.ignore_mandatory = True
            m.flags.ignore_links = True
            m.insert()
            frappe.db.commit()
            self.logger.info(f"Created Module Def '{module_name}' → app '{app_name}'")
        except Exception as e:
            self.logger.debug(f"Module Def '{module_name}' skipped: {e}")

    def _patch_controller_resolution(self):
        """
        Patch frappe.model.base_document.import_controller so that service
        doctypes whose Python module cannot be found fall back to
        ControllerRegistry (if registered) or base Document, instead of
        raising ImportError.
        """
        apply_controller_patch()
