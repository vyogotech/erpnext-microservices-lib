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
"""

import frappe


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

        def _deduplicated_filter(apps):
            """
            Keep only allowed apps, deduplicate, and ensure 'frappe' is first
            in the list (Frappe expects this for module resolution).
            """
            seen = set()
            filtered = []
            for a in apps:
                if a in allowed_apps and a not in seen:
                    seen.add(a)
                    filtered.append(a)

            if service_app_name not in seen:
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
            """
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
                return original_get_attr(method_string)
            except (
                getattr(frappe, 'AppNotInstalledError', type(None)),
                ImportError,
                ModuleNotFoundError,
            ) as e:
                app_name = method_string.split('.')[0] if '.' in method_string else 'unknown'
                self.logger.debug(
                    f"Skipping hook '{method_string}' - "
                    f"app '{app_name}' not installed: {e}")
                raise AttributeError(
                    f"Hook from non-installed app '{app_name}' skipped") from e

        frappe.get_attr = microservice_get_attr
        self.logger.info(
            f"Hooks resolution patched: allowed apps = {sorted(allowed_apps)}")

    def _sync_service_doctypes(self):
        """
        Scan doctypes_path for DocType JSONs.  For each JSON:
        - Register the doctype name in self._service_doctype_names.
        - If the DocType does NOT exist in the DB, create it via import_doc.
        - If it already exists, skip DB changes (never delete/overwrite).
        - Always register the module mapping in frappe.local so
          get_module_app() resolves correctly for this service.
        """
        if not getattr(self, "doctypes_path", None):
            return

        import os
        from pathlib import Path
        import json

        if not os.path.isdir(self.doctypes_path):
            self.logger.warning(f"DocTypes directory not found: {self.doctypes_path}")
            return

        imported_any = False
        doctypes_dir = Path(self.doctypes_path)
        for json_path in doctypes_dir.glob("*/*.json"):
            try:
                with open(json_path, 'r') as f:
                    doc = json.load(f)

                doc_name = doc.get("name")
                if doc_name:
                    self._service_doctype_names.add(doc_name)
                    self.logger.debug(f"Found service DocType: {doc_name}")

                    if not frappe.db.exists("DocType", doc_name):
                        self.logger.info(f"Creating DocType {doc_name} in DB...")
                        frappe.modules.import_file.import_doc(
                            doc,
                            ignore_version=True,
                            reset_permissions=False,
                        )
                        imported_any = True
                    else:
                        self.logger.debug(
                            f"DocType {doc_name} already exists in DB, skipping import"
                        )

                service_app = self.name.replace("-", "_")
                if not hasattr(frappe.local, "module_app"):
                    frappe.local.module_app = {}
                if not hasattr(frappe.local, "app_modules"):
                    frappe.local.app_modules = {}

                # Register module from JSON (our canonical module name)
                module = doc.get("module")
                if module:
                    scrubbed_module = module.lower().replace(" ", "_")
                    frappe.local.module_app[scrubbed_module] = service_app
                    if service_app not in frappe.local.app_modules:
                        frappe.local.app_modules[service_app] = []
                    if scrubbed_module not in frappe.local.app_modules[service_app]:
                        frappe.local.app_modules[service_app].append(scrubbed_module)

                # When doctype already exists in DB, it may have a different module (e.g. Saas Platform).
                # Register that module -> service_app so Frappe resolves it without "Module X not found".
                if frappe.db.exists("DocType", doc_name):
                    try:
                        existing_module = frappe.db.get_value("DocType", doc_name, "module")
                        if existing_module:
                            existing_scrubbed = existing_module.lower().replace(" ", "_")
                            frappe.local.module_app[existing_scrubbed] = service_app
                            if existing_scrubbed not in (
                                frappe.local.app_modules.get(service_app) or []
                            ):
                                if service_app not in frappe.local.app_modules:
                                    frappe.local.app_modules[service_app] = []
                                frappe.local.app_modules[service_app].append(existing_scrubbed)
                    except Exception:
                        pass

            except Exception as e:
                self.logger.error(
                    f"Error reading/syncing DocType JSON at {json_path}: {e}",
                    exc_info=True,
                )

        if imported_any:
            frappe.db.commit()

    def _patch_controller_resolution(self):
        """
        Patch frappe.model.base_document.import_controller so that service
        doctypes whose Python module cannot be found fall back to
        ControllerRegistry (if registered) or base Document, instead of
        raising ImportError.
        """
        if getattr(frappe, "_microservice_controller_patched", False):
            return

        original_import_controller = frappe.model.base_document.import_controller
        service_doctype_names = self._service_doctype_names

        def microservice_import_controller(doctype):
            try:
                return original_import_controller(doctype)
            except (ImportError, ModuleNotFoundError):
                if doctype in service_doctype_names:
                    from frappe_microservice.controller import get_controller_registry
                    registry = get_controller_registry()
                    controller_class = registry.get_controller(doctype)
                    if controller_class:
                        return controller_class

                    return frappe.model.document.Document
                raise

        frappe.model.base_document.import_controller = microservice_import_controller
        frappe._microservice_controller_patched = True
