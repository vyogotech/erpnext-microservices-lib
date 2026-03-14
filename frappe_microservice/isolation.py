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
- _patch_hooks_resolution(): Replace get_doc_hooks and get_attr so hooks from
  non-allowed apps are filtered out. Must run AFTER frappe.connect().

Public standalone functions:
- presync_service_doctypes(): One-shot DB sync called from entrypoint before
  Gunicorn starts. Safe to call from multiple pods (idempotent).
- Also syncs fixture data (e.g. SMS Settings, Email Template) from FIXTURES_PATH.
"""

import json
import logging
import os
import threading
from pathlib import Path

import frappe

# Re-entrancy guard: when we're inside microservice_get_attr, nested get_attr
# (e.g. from hook code or query builder) must use the original to avoid recursion.
_get_attr_depth = threading.local()


def _get_depth():
    return getattr(_get_attr_depth, "value", 0)


def _set_depth(value):
    _get_attr_depth.value = value


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
            _logger.debug(f"🔍 [Isolation] original_import_controller failed for {doctype}. registry_id={reg_id}, keys={keys}")

            # Universal fallback to ControllerRegistry
            controller_class = registry.get_controller(doctype)
            if controller_class:
                _logger.debug(f"✅ [Isolation] Resolved {doctype} -> {controller_class.__name__} (id={reg_id})")
                return controller_class
            
            # If not in registry and it should have been (known service doctype), log a warning
            if doctype in _SERVICE_DOCTYPES:
                _logger.warning(
                    f"⚠️ [Isolation] Doctype {doctype} MISSING from registry! "
                    f"registry_id={reg_id}, keys={keys}. "
                    "Falling back to Document."
                )
                return frappe.model.document.Document
            
            _logger.debug(f"❌ [Isolation] {doctype} not in registry or service_doctypes. Re-raising.")
            raise

    frappe.model.base_document.import_controller = microservice_import_controller


def register_module_for_service(module_str, service_app):
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


def register_service_doctypes(doctypes_path, service_name, logger=None):
    """
    Scan doctypes_path for DocType JSONs and register doctype names and
    module mappings in memory only (no DB).
    """
    if not doctypes_path:
        return set()

    if not os.path.isdir(doctypes_path):
        if logger:
            logger.warning(f"DocTypes directory not found: {doctypes_path}")
        return set()

    service_app = service_name.replace("-", "_")
    doctypes_dir = Path(doctypes_path)
    found_names = set()

    for json_path in doctypes_dir.glob("*/*.json"):
        try:
            with open(json_path, 'r') as f:
                doc = json.load(f)
            doc_name = doc.get("name")
            if doc_name:
                found_names.add(doc_name)
            module = doc.get("module")
            register_module_for_service(module, service_app)
        except Exception as e:
            if logger:
                logger.error(f"Error reading DocType JSON at {json_path}: {e}")

    _SERVICE_DOCTYPES.update(found_names)
    apply_controller_patch()

    return found_names


class IsolationMixin:
    """
    Mixin for MicroserviceApp that provides app/module isolation.

    Expects the host class to have:
        self.name: str
        self.load_framework_hooks: list
        self.logger: logging.Logger
    """

    def _get_allowed_apps(self):
        """
        Return the set of app names this microservice may load. Always includes
        'frappe' and the service app (self.name with '-' replaced by '_').
        Plus any apps in load_framework_hooks (e.g. ['erpnext']).
        """
        allowed = set(self.load_framework_hooks or [])
        allowed.add('frappe')
        allowed.add(self.name.replace('-', '_'))
        return allowed

    def _patch_app_resolution(self):
        """
        Override frappe.get_installed_apps() and frappe.get_all_apps() to read
        from the filesystem (apps.txt) filtered by load_framework_hooks.

        MUST be called BEFORE frappe.init() so that setup_module_map() uses
        the patched versions instead of reading from the shared DB.

        The patched get_installed_apps never touches the shared database --
        it reads from apps.txt (baked into the container image at build time)
        and intersects with load_framework_hooks (service-level config).
        """
        if getattr(frappe, "_microservice_isolation_applied", False):
            self.logger.debug(
                "Microservice app isolation already applied, skipping")
            return

        frappe._microservice_isolation_applied = True

        allowed_apps = self._get_allowed_apps()
        service_app_name = self.name.replace('-', '_')

        original_get_all_apps = frappe.get_all_apps

        self.logger.info(
            f"Patching app resolution: allowed apps = {sorted(allowed_apps)}")

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
                if a in allowed_apps and a not in seen:
                    seen.add(a)
                    filtered.append(a)

            if service_app_name not in seen and _service_app_is_importable(service_app_name):
                filtered.append(service_app_name)
            if 'frappe' not in seen:
                filtered.append('frappe')

            if 'frappe' in filtered:
                filtered.remove('frappe')
                filtered.insert(0, 'frappe')

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
                    with_internal_apps=with_internal_apps,
                    sites_path=sites_path
                )
            except Exception:
                apps = []
            return _deduplicated_filter(apps)

        frappe.get_installed_apps = microservice_get_installed_apps
        frappe.get_all_apps = microservice_get_all_apps

        self.logger.info(
            f"Microservice app resolution patched (filesystem-based)")

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

            app_modules = getattr(frappe.local, 'app_modules', None)
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
                        f"(removed {original_count - filtered_count} central-site apps)")

            frappe.local.module_app = {}
            for app, modules in (getattr(frappe.local, 'app_modules', None) or {}).items():
                if not isinstance(modules, (list, tuple)):
                    continue
                for module in modules:
                    frappe.local.module_app[module] = app

            # Ensure service local modules mapped in _sync_service_doctypes are populated
            # for THIS thread.
            if hasattr(self, '_service_modules'):
                service_app = self.name.replace("-", "_")
                for module in self._service_modules:
                    frappe.local.module_app[module] = service_app
                    if service_app not in getattr(frappe.local, 'app_modules', {}):
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
        Patch frappe.get_doc_hooks(), frappe.get_attr(), and frappe._load_app_hooks
        so that:
        - Hooks from non-allowed apps are filtered out.
        - Apps that are in the installed list but have no Python hooks module
          (e.g. non-Frappe microservices like signup-service) are skipped instead
          of raising ModuleNotFoundError.
        MUST be called AFTER frappe.connect() because hook resolution may
        need a database connection.
        """
        allowed_apps = self._get_allowed_apps()

        # Patch _load_app_hooks so apps without a loadable hooks module (e.g. signup_service)
        # are skipped instead of raising. Required when the "service app" is in
        # get_installed_apps() but is not a Frappe app and has no app.hooks module.
        if not getattr(frappe, "_microservice_load_app_hooks_patched", False):
            import inspect
            import types

            original_load_app_hooks = frappe._load_app_hooks

            def microservice_load_app_hooks(app_name=None):
                hooks = {}
                try:
                    apps = [app_name] if app_name else frappe.get_installed_apps(_ensure_on_bench=True)
                except Exception as e:
                    self.logger.warning(
                        "Failed to get installed apps for hook loading: %s. Returning empty hooks.",
                        e,
                        exc_info=True,
                    )
                    return hooks
                if not isinstance(apps, (list, tuple)):
                    self.logger.warning(
                        "get_installed_apps returned non-sequence %s. Returning empty hooks.",
                        type(apps).__name__,
                    )
                    return hooks
                for app in apps:
                    if not app or not isinstance(app, str):
                        continue
                    try:
                        app_hooks = frappe.get_module(f"{app}.hooks")
                    except (ImportError, ModuleNotFoundError) as e:
                        if not getattr(frappe.local, "flags", None) or not getattr(
                            frappe.local.flags, "in_install_app", False
                        ):
                            self.logger.debug(
                                "Skipping hooks for app %r (no hooks module): %s",
                                app,
                                e,
                            )
                        continue
                    except Exception as e:
                        self.logger.warning(
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
                                self.logger.debug(
                                    "Skipping hook %r from app %r: %s",
                                    key,
                                    app,
                                    e,
                                )
                    except Exception as e:
                        self.logger.warning(
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
            self.logger.info(
                "Patched _load_app_hooks to skip apps without a hooks module"
            )

        if getattr(frappe, "_microservice_hooks_resolution_patched", False):
            self.logger.debug("Hooks resolution already patched, skipping")
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
                    type(e).__name__, e,
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
                        if '.' in handler:
                            app_name = handler.split('.')[0]
                            if app_name in allowed_apps:
                                filtered_handlers.append(handler)
                            else:
                                self.logger.debug(
                                    f"Filtering out hook '{handler}' - "
                                    f"app '{app_name}' not in allowed apps")
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
            if '.' in method_string:
                app_name = method_string.split('.')[0]
                if app_name not in allowed_apps:
                    self.logger.debug(
                        f"Skipping hook '{method_string}' - "
                        f"app '{app_name}' not in allowed apps")
                    raise AttributeError(
                        f"Hook from non-installed app '{app_name}' skipped")
            try:
                _set_depth(_get_depth() + 1)
                return original_get_attr(method_string)
            except Exception as e:
                # Catch AppNotInstalledError if frappe has it, plus ImportError/ModuleNotFoundError
                allowed_exceptions = (ImportError, ModuleNotFoundError)
                if hasattr(frappe, 'AppNotInstalledError'):
                    allowed_exceptions += (frappe.AppNotInstalledError,)
                
                if not isinstance(e, allowed_exceptions):
                    raise
                    
                app_name = method_string.split('.')[0] if '.' in method_string else 'unknown'
                self.logger.debug(
                    f"Skipping hook '{method_string}' - "
                    f"app '{app_name}' not installed: {e}")
                raise AttributeError(
                    f"Hook from non-installed app '{app_name}' skipped") from e
            finally:
                _set_depth(_get_depth() - 1)

        frappe.get_attr = microservice_get_attr
        self.logger.info(
            f"Hooks resolution patched: allowed apps = {sorted(allowed_apps)}")

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
        import os
        from pathlib import Path
        import json

        if not os.path.isdir(self.doctypes_path):
            self.logger.warning(f"DocTypes directory not found: {self.doctypes_path}")
            return

        service_app = self.name.replace("-", "_")
        doctypes_dir = Path(self.doctypes_path)
        for json_path in doctypes_dir.glob("*/*.json"):
            try:
                with open(json_path, 'r') as f:
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
        import os
        from pathlib import Path
        import json

        if not os.path.isdir(self.doctypes_path):
            return

        service_app = self.name.replace("-", "_")
        doctypes_dir = Path(self.doctypes_path)
        imported_any = False

        for json_path in doctypes_dir.glob("*/*.json"):
            try:
                with open(json_path, 'r') as f:
                    doc = json.load(f)
                doc_name = doc.get("name")
                module = doc.get("module")

                if doc_name and frappe.db.exists("DocType", doc_name):
                    try:
                        existing_module = frappe.db.get_value("DocType", doc_name, "module")
                        self._register_module_for_service(existing_module, service_app)
                    except Exception:
                        pass

                if module:
                    self._ensure_module_def(module, service_app)

                if doc_name and not frappe.db.exists("DocType", doc_name):
                    self.logger.info(f"Creating DocType {doc_name} in DB...")
                    self._import_doc_without_cache_flush(doc)
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
        import frappe.cache_manager as _cm

        original = _cm.reset_metadata_version
        _cm.reset_metadata_version = lambda: None
        try:
            frappe.modules.import_file.import_doc(
                doc,
                ignore_version=True,
                reset_permissions=False,
            )
        finally:
            _cm.reset_metadata_version = original

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
            m = frappe.get_doc({
                "doctype": "Module Def",
                "module_name": module_name,
                "app_name": app_name,
            })
            m.flags.ignore_permissions = True
            m.flags.ignore_mandatory = True
            m.flags.ignore_links = True
            m.insert()
            frappe.db.commit()
            self.logger.info(
                f"Created Module Def '{module_name}' → app '{app_name}'"
            )
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


# ---------------------------------------------------------------------------
# Standalone helpers for one-shot DB presync (called from entrypoint, not from
# workers). These mirror the IsolationMixin methods but don't need `self`.
# ---------------------------------------------------------------------------

def _ensure_module_def_record(module_name: str, app_name: str, logger=None):
    """Insert a Module Def record if it doesn't already exist. Idempotent."""
    try:
        if not frappe.db.table_exists("Module Def"):
            return
        if frappe.db.exists("Module Def", module_name):
            return
        m = frappe.get_doc({
            "doctype": "Module Def",
            "module_name": module_name,
            "app_name": app_name,
        })
        m.flags.ignore_permissions = True
        m.flags.ignore_mandatory = True
        m.flags.ignore_links = True
        m.insert()
        frappe.db.commit()
        if logger:
            logger.info("Created Module Def '%s' -> app '%s'", module_name, app_name)
    except Exception as e:
        if logger:
            logger.debug("Module Def '%s' skipped: %s", module_name, e)


def _import_doc_suppressing_cache_reset(doc: dict):
    """Import a DocType document without triggering reset_metadata_version."""
    import frappe.cache_manager as _cm

    original = _cm.reset_metadata_version
    _cm.reset_metadata_version = lambda: None
    try:
        frappe.modules.import_file.import_doc(
            doc,
            ignore_version=True,
            reset_permissions=False,
        )
    finally:
        _cm.reset_metadata_version = original


def _get_import_file_by_path():
    """Lazy import of frappe.modules.import_file.import_file_by_path."""
    from frappe.modules.import_file import import_file_by_path
    return import_file_by_path


def _sync_fixtures_from_path(fixtures_path: str | None, logger: logging.Logger):
    """
    Import all .json fixture files from fixtures_path into the DB.

    Each file is imported via Frappe's import_file_by_path with force=True
    (upsert semantics). Errors in one file do not block others.
    """
    if not fixtures_path or not os.path.isdir(fixtures_path):
        if fixtures_path:
            logger.warning("Fixtures directory not found: %s", fixtures_path)
        return

    _import = _get_import_file_by_path()

    fix_dir = Path(fixtures_path)
    for json_path in sorted(fix_dir.glob("*.json")):
        try:
            logger.info("presync: importing fixture %s", json_path.name)
            frappe.flags.mute_emails = True
            _import(
                str(json_path),
                data_import=True,
                force=True,
                reset_permissions=True,
            )
            frappe.flags.mute_emails = False
            frappe.db.commit()
        except Exception as e:
            frappe.flags.mute_emails = False
            logger.warning("presync: error importing fixture %s: %s", json_path.name, e)
            try:
                frappe.db.rollback()
            except Exception:
                pass


def presync_service_doctypes(
    doctypes_path: str | None = None,
    service_name: str | None = None,
    site: str | None = None,
    sites_path: str | None = None,
    fixtures_path: str | None = None,
):
    """
    One-shot DB sync of service doctypes and fixtures. Designed to be called
    once from the container entrypoint before Gunicorn starts.

    Safe across multiple pods: all operations are idempotent (check-then-create
    with caught duplicates). Two pods racing will both succeed without conflict.

    Falls back to env vars: SERVICE_PATH, SERVICE_NAME, DOCTYPES_PATH, FRAPPE_SITE,
    FRAPPE_SITES_PATH. If DOCTYPES_PATH is not set, it defaults to
    {SERVICE_PATH}/{service_app}/{service_app}/doctype (e.g. .../signup_service/signup_service/doctype).
    Fixtures auto-discovered at <SERVICE_PATH>/fixtures/.
    """
    service_path = os.getenv("SERVICE_PATH", "/app/service")
    service_name = service_name or os.getenv("SERVICE_NAME", "")
    service_app = service_name.replace("-", "_") if service_name else ""

    # Default DOCTYPES_PATH to conventional location: {SERVICE_PATH}/{service_app}/{service_app}/doctype
    if not doctypes_path and service_app:
        doctypes_path = os.path.join(service_path, service_app, service_app, "doctype")
    doctypes_path = doctypes_path or os.getenv("DOCTYPES_PATH")

    if not fixtures_path:
        fixtures_path = os.path.join(service_path, "fixtures")
    has_doctypes = doctypes_path and os.path.isdir(doctypes_path)
    has_fixtures = fixtures_path and os.path.isdir(fixtures_path)
    if not has_doctypes and not has_fixtures:
        return
    site = site or os.getenv("FRAPPE_SITE", "site1.local")
    sites_path = sites_path or os.getenv("FRAPPE_SITES_PATH", "/app/sites")

    logger = logging.getLogger("presync")

    try:
        frappe.init(site=site, sites_path=sites_path)
        frappe.connect()
    except Exception as e:
        logger.warning("presync: cannot connect to DB (%s), skipping", e)
        try:
            frappe.destroy()
        except Exception:
            pass
        return

    try:
        if has_doctypes:
            doctypes_dir = Path(doctypes_path)
            imported_any = False
            for json_path in doctypes_dir.glob("*/*.json"):
                try:
                    with open(json_path, "r") as f:
                        doc = json.load(f)

                    module = doc.get("module")
                    if module:
                        _ensure_module_def_record(module, service_app, logger)

                    doc_name = doc.get("name")
                    if doc_name and not frappe.db.exists("DocType", doc_name):
                        logger.info("presync: creating DocType %s", doc_name)
                        _import_doc_suppressing_cache_reset(doc)
                        imported_any = True
                except Exception as e:
                    logger.warning("presync: error syncing %s: %s", json_path, e)
                    try:
                        frappe.db.rollback()
                    except Exception:
                        pass

            if imported_any:
                frappe.db.commit()
            logger.info("presync: service doctype sync complete")

        if has_fixtures:
            _sync_fixtures_from_path(fixtures_path, logger)
            logger.info("presync: fixture sync complete")
    except Exception as e:
        logger.warning("presync: unexpected error: %s", e)
    finally:
        try:
            frappe.destroy()
        except Exception:
            pass
