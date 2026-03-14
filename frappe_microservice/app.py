"""
MicroserviceApp -- the main application class for Frappe microservices.

This module defines the primary entry point for building microservices:
- MicroserviceApp: Flask-based app that composes IsolationMixin, AuthMixin, and ResourceMixin.
  It initializes Frappe once at server startup, reuses the DB connection across requests,
  and provides secure_route (auth + tenant resolution + error handling) and run().
- create_microservice(): Factory that returns a configured MicroserviceApp instance.

Developers typically use: app = create_microservice("my-service") then @app.secure_route(...).
"""

from flask import Flask, request, jsonify, g, has_app_context
from functools import wraps
import copy
import os
import logging
import traceback
import uuid
import threading
import copy

import frappe
from frappe.utils.local import _contextvar

from frappe_microservice.site_config import create_site_config
from frappe_microservice.tenant import TenantAwareDB, get_user_tenant_id
from frappe_microservice.isolation import IsolationMixin
from frappe_microservice.auth import AuthMixin
from frappe_microservice.resources import ResourceMixin
from frappe_microservice.central import CentralSiteClient
from frappe_microservice.background import BackgroundTaskMixin

try:
    from flasgger import Swagger
except ImportError:
    Swagger = None

from werkzeug.exceptions import HTTPException

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class MicroserviceApp(IsolationMixin, AuthMixin, ResourceMixin, BackgroundTaskMixin):
    """
    Base class for Frappe microservices

    Features:
    - Automatic Frappe initialization
    - Session-based authentication via Central Site
    - Secure-by-default endpoints
    - Automatic database connection management
    - Built-in error handling
    - Tenant-aware database queries

    Usage:
        app = MicroserviceApp(
            name="orders-service",
            central_site_url="http://central-site:8000",
            frappe_site="dev.localhost",
            tenant_field='tenant_id'
        )

        @app.secure_route('/orders', methods=['GET'])
        def list_orders(user):
            return {"data": app.tenant_db.get_all('Sales Order')}
    """

    def __init__(self,
                 name,
                 central_site_url=None,
                 frappe_site=None,
                 sites_path=None,
                 db_host=None,
                 port=8000,
                 tenant_field='tenant_id',
                 get_tenant_id_func=None,
                 load_framework_hooks=None,
                 log_level=None,
                 otel_exporter_url=None,
                 doctypes_path=None,
                 fixtures_path=None):
        """
        Initialize the microservice: create Flask app, set up logging and optional
        OpenTelemetry, resolve load_framework_hooks (frappe + service app + config),
        set site paths and ensure log dirs exist, create TenantAwareDB with
        _get_current_tenant_id, register middleware and /health, then Swagger if available.

        Args:
            load_framework_hooks: Control which framework hooks to load. Can be:
                - List of app names: ['frappe', 'erpnext'] - Load specific apps
                - 'full': Load frappe + erpnext hooks (backward compatibility)
                - 'frappe-only': Load only frappe hooks (backward compatibility)
                - 'none': Load only microservice hooks (backward compatibility)
                - None: Default to ['frappe', 'erpnext'] for backward compatibility
            log_level: logging level (e.g. logging.INFO, "DEBUG"). Defaults to LOG_LEVEL env var.
            otel_exporter_url: OTLP exporter URL. Defaults to OTEL_EXPORTER_OTLP_ENDPOINT env var.
        """
        self.name = name
        self.port = port
        self.tenant_field = tenant_field

        self.flask_app = Flask(name)

        self.log_level = log_level or os.getenv('LOG_LEVEL', 'INFO')
        self._setup_logging()

        self.otel_exporter_url = otel_exporter_url or os.getenv(
            'OTEL_EXPORTER_OTLP_ENDPOINT')
        self._setup_otel()

        if load_framework_hooks is None or load_framework_hooks == 'full':
            self.load_framework_hooks = ['frappe', 'erpnext']
        elif load_framework_hooks == 'frappe-only':
            self.load_framework_hooks = ['frappe']
        elif load_framework_hooks == 'none':
            self.load_framework_hooks = []
        elif isinstance(load_framework_hooks, list):
            self.load_framework_hooks = load_framework_hooks
        else:
            raise ValueError(
                f"load_framework_hooks must be a list of app names or one of ['full', 'frappe-only', 'none'], "
                f"got '{load_framework_hooks}'")

        self.frappe_site = frappe_site or os.getenv(
            'FRAPPE_SITE', 'site1.local')
        self.sites_path = sites_path or os.getenv(
            'FRAPPE_SITES_PATH', '/app/sites')

        os.environ['SITES_PATH'] = self.sites_path

        for _base in ['/app', '/app/sites', self.sites_path]:
            try:
                os.makedirs(os.path.join(_base, self.frappe_site, 'logs'), exist_ok=True)
            except Exception:
                pass

        self.db_host = db_host or os.getenv('DB_HOST')

        self.central_site_url = central_site_url or os.getenv(
            'CENTRAL_SITE_URL', 'http://central-site:8000')

        self._custom_get_tenant_id = get_tenant_id_func

        self.tenant_db = TenantAwareDB(
            self._get_current_tenant_id, logger=self.logger)

        self._setup_middleware()
        self._register_built_in_routes()
        self._central_client = None

        if Swagger:
            self.swagger = Swagger(self.flask_app, template={
                "swagger": "2.0",
                "info": {
                    "title": f"{self.name} API",
                    "description": f"Interactive API documentation for {self.name}",
                    "version": "1.1.0"
                },
                "basePath": "/",
                "schemes": ["http", "https"],
                "securityDefinitions": {
                    "sid": {
                        "type": "apiKey",
                        "name": "sid",
                        "in": "cookie",
                        "description": "Frappe Session ID (sid) cookie"
                    }
                }
            })
            self.logger.info("Swagger documentation enabled at /apidocs")
        else:
            self.swagger = None
            self.logger.warning("Flasgger not found. Swagger documentation disabled.")

        self.doctypes_path = doctypes_path
        self.fixtures_path = fixtures_path or self._resolve_fixtures_path()
        self._service_doctype_names = set()
        self._db_connected = False
        self._frappe_local_base = None

        self._initialize_frappe()
        self._maybe_start_rq_worker()

    @property
    def central(self):
        """
        Returns a Frappe-like API client for the Central Site.
        Lazy-initialized using config from env or self.central_site_url.
        """
        if not self._central_client:
            self._central_client = CentralSiteClient(url=self.central_site_url)
        return self._central_client

    @staticmethod
    def _resolve_fixtures_path():
        """Derive fixtures_path from SERVICE_PATH convention: <SERVICE_PATH>/fixtures."""
        service_path = os.getenv("SERVICE_PATH")
        if not service_path:
            return None
        candidate = os.path.join(service_path, "fixtures")
        return candidate if os.path.isdir(candidate) else None

    def _setup_logging(self):
        """
        Configure logging level and format for the microservice.
        Converts string log level (e.g. "DEBUG") to logging constant and sets it
        on both the root logger and the Flask app logger. self.logger is then
        the Flask app logger for use elsewhere.
        """
        if isinstance(self.log_level, str):
            self.log_level = getattr(logging, self.log_level.upper(), logging.INFO)

        logging.getLogger().setLevel(self.log_level)

        self.flask_app.logger.setLevel(self.log_level)
        self.logger = self.flask_app.logger

        self.logger.info(f"Logging initialized at level: {logging.getLevelName(self.log_level)}")

    def _setup_otel(self):
        """
        Configure OpenTelemetry tracing if OTEL_EXPORTER_OTLP_ENDPOINT is set.
        Sets up TracerProvider, BatchSpanProcessor, OTLP exporter, and instruments
        the Flask app so HTTP requests are traced. No-op if URL is missing or
        opentelemetry packages are not installed.
        """
        if not self.otel_exporter_url:
            self.logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set, tracing disabled.")
            return

        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.instrumentation.flask import FlaskInstrumentor
            from opentelemetry.sdk.resources import SERVICE_NAME, Resource

            resource = Resource(attributes={
                SERVICE_NAME: self.name
            })

            provider = TracerProvider(resource=resource)
            processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=self.otel_exporter_url))
            provider.add_span_processor(processor)
            trace.set_tracer_provider(provider)

            FlaskInstrumentor().instrument_app(self.flask_app)

            self.logger.info(f"OpenTelemetry tracing enabled. Sending to: {self.otel_exporter_url}")

        except ImportError:
            self.logger.warning("OpenTelemetry libraries not found. Run 'pip install \"frappe-microservice[otel]\"' to enable tracing.")
        except Exception as e:
            self.logger.error(f"Failed to initialize OpenTelemetry: {e}")

    def _get_current_tenant_id(self):
        """
        Return the current request's tenant_id. Used by TenantAwareDB for all DB operations.
        Priority: (1) custom get_tenant_id_func if provided at init, (2) g.tenant_id set
        by secure_route after validating the session. Returns None if not set.
        """
        if self._custom_get_tenant_id:
            return self._custom_get_tenant_id()

        if hasattr(g, 'tenant_id'):
            return g.tenant_id

        return None

    def set_tenant_id(self, tenant_id):
        """
        Set tenant_id in Flask g for the current request. All subsequent tenant_db
        operations in this request will be scoped to this tenant. Typically called
        after resolving tenant from the authenticated user (e.g. via get_user_tenant_id).
        """
        g.tenant_id = tenant_id

    def _initialize_frappe(self):
        """
        Phase 1: One-time Frappe initialization at server startup.

        Runs frappe.init(), applies all isolation patches, and captures the
        resulting frappe.local dict for reuse across requests. Does NOT open
        a DB connection -- that is deferred to the first real request so
        gunicorn workers can fork safely.
        """
        try:
            create_site_config()
        except Exception as e:
            self.logger.error(f"Failed to create site_config.json: {e}")

        self._patch_app_resolution()
        frappe.init(site=self.frappe_site, sites_path=self.sites_path)
        self._filter_module_maps()
        self._patch_controller_resolution()
        self._patch_hooks_resolution()

        if self.db_host:
            frappe.local.conf.db_host = self.db_host

        frappe.local.session = frappe._dict(user='Guest', sid=None, data=frappe._dict())

        self._patch_version_session_data()

        self._frappe_local_base = _contextvar.get()
        self._main_pid = os.getpid()
        self.logger.info("Frappe initialized at startup (DB deferred to first request)")

    def _patch_version_session_data(self):
        """Patch Frappe Version doctype so session.data.get() never crashes when data is None."""
        try:
            from frappe.core.doctype.version import version as version_module
            _set_impersonator = version_module.Version.set_impersonator

            @staticmethod
            def _safe_set_impersonator(data):
                if not frappe.session:
                    return
                session_data = getattr(frappe.session, "data", None)
                if session_data is None:
                    frappe.session.data = frappe._dict()
                    session_data = frappe.session.data
                if impersonator := session_data.get("impersonated_by"):
                    data["impersonated_by"] = impersonator
                if audit_user := session_data.get("audit_user"):
                    data["audit_user"] = audit_user

            version_module.Version.set_impersonator = _safe_set_impersonator
        except Exception as e:
            self.logger.warning("Could not patch Version.set_impersonator: %s", e)

    def _restore_frappe_local(self):
        """
        Restore frappe.local from the startup-captured dict and reset
        per-request fields. On the first call per worker process, opens the
        DB connection and registers service doctypes in memory.

        DB sync of doctypes is handled by the entrypoint (presync) before
        Gunicorn starts. In dev mode (no entrypoint), falls back to
        _sync_service_doctypes_to_db() on first request.

        The DB object is stored on `self` rather than relying on the
        ContextVar dict mutation, because Werkzeug may run each request in
        a copied context that doesn't see in-place dict changes from prior
        requests.

        When Gunicorn runs with --preload, the app is loaded once in the
        master; workers inherit the same _frappe_local_base. Each worker
        must use its own copy so DB and request state are not shared.
        """
        if os.getpid() != self._main_pid:
            if not getattr(self, "_worker_local_copied", False):
                self._frappe_local_base = copy.deepcopy(self._frappe_local_base)
                self._worker_local_copied = True
        _contextvar.set(self._frappe_local_base)

        # Frappe's Version doctype expects frappe.session.data to be a dict (e.g. .get("impersonated_by"))
        if getattr(frappe.local, "session", None) is not None:
            if not getattr(frappe.local.session, "data", None):
                frappe.local.session.data = frappe._dict()

        if not self._db_connected:
            frappe.connect(set_admin_as_user=False)
            self._db_obj = frappe.local.db
            self._register_service_doctypes_from_json()
            if not os.getenv("_DOCTYPES_PRESYNCED"):
                self._sync_service_doctypes_to_db()
                self._sync_fixtures_to_db()
            self._db_connected = True
            self.logger.info("DB connection established on first request")
        else:
            frappe.local.db = self._db_obj
            try:
                self._db_obj._conn.ping()
            except Exception as e:
                self.logger.warning("DB connection lost (%s: %s), reconnecting",
                                    type(e).__name__, e)
                frappe.connect(set_admin_as_user=False)
                self._db_obj = frappe.local.db

        frappe.local.form_dict = frappe._dict()
        frappe.local.request_ip = request.remote_addr
        frappe.local.flags = frappe._dict(currently_saving=[])
        frappe.local.session = frappe._dict(user='Guest', sid=None, data=frappe._dict())
        frappe.local.error_log = []
        frappe.local.message_log = []

    _SKIP_PATHS = frozenset(('/health', '/socket.io/', '/socket.io'))

    def _setup_middleware(self):
        """
        Register Flask before_request, after_request, and errorhandler.
        - before_request: Restore frappe.local from startup state, reset
          per-request fields. Skips /health and /socket.io entirely.
        - after_request: Commit DB (unless rolled back). Never closes
          the connection -- it is reused across requests.
        - errorhandler: Catch unhandled exceptions, return JSON 500.
        """

        @self.flask_app.before_request
        def frappe_before_request():
            if request.path in self._SKIP_PATHS:
                return None

            try:
                self._restore_frappe_local()
            except Exception as e:
                self.logger.error(
                    "Frappe context restore failed: %s", e, exc_info=True)
                return jsonify({
                    "status": "error",
                    "message": "Service temporarily unavailable.",
                    "type": type(e).__name__,
                    "code": 503,
                }), 503

            g._frappe_rolled_back = False
            g.request_id = request.headers.get('X-Request-ID', str(uuid.uuid4()))
            self.logger.info(
                f"Request started: {request.method} {request.path} "
                f"[request_id={g.request_id}]")

        @self.flask_app.after_request
        def cleanup_frappe_context(response):
            if request.path in self._SKIP_PATHS:
                return response

            try:
                if hasattr(frappe, 'db') and frappe.db:
                    if not getattr(g, '_frappe_rolled_back', False):
                        try:
                            frappe.db.commit()
                        except Exception:
                            frappe.db.rollback()

                if hasattr(g, 'request_id'):
                    response.headers['X-Request-ID'] = g.request_id
            except Exception as e:
                self.logger.warning(f"Cleanup warning: {e}", exc_info=True)

            return response

        @self.flask_app.errorhandler(Exception)
        def handle_exception(e):
            if isinstance(e, HTTPException):
                return e

            self.logger.error(f"UNHANDLED EXCEPTION: {str(e)}\n{traceback.format_exc()}")

            return jsonify({
                "status": "error",
                "message": "An internal server error occurred.",
                "type": type(e).__name__,
                "code": 500,
                "error": str(e),
                "details": traceback.format_exc() if self.flask_app.debug or os.getenv('DEBUG') == '1' else None
            }), 500


    def _register_built_in_routes(self):
        """
        Register built-in routes. Currently only GET /health returning JSON with
        status, service name, and frappe_site. Used by orchestrators and load balancers.
        """

        @self.flask_app.route('/health', methods=['GET'])
        def health():
            import datetime
            return jsonify({
                "status": "healthy",
                "service": self.name,
                "site": self.frappe_site,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "database": "connected"
            })

    def secure_route(self, rule, **options):
        """
        Decorator that registers a route and wraps the view with: (1) session
        validation (OAuth2 Bearer or SID cookie via Central Site), (2) sync of
        frappe.session.user with validated user, (3) resolution of tenant_id via
        get_user_tenant_id and storage in g.tenant_id, (4) injection of username
        as first argument to the view, (5) conversion of dict return to jsonify,
        (6) catch of Frappe/ValueError and return of appropriate JSON error (403/404/400/500)
        with optional rollback. Use for any endpoint that requires an authenticated user.
        """
        def decorator(f):
            @wraps(f)
            def wrapper(*args, **kwargs):
                self.logger.info(
                    f"SECURE_ROUTE DEBUG: Method {f.__name__} called")

                username, error_response = self._validate_session()

                self.logger.info(
                    f"SECURE_ROUTE DEBUG: _validate_session returned: {username}, {error_response is not None}")

                if error_response:
                    return error_response

                if hasattr(frappe, 'session') and frappe.session.user != username:
                    self.logger.warning(
                        f"Session mismatch detected: frappe.session.user={frappe.session.user}, validated={username}. Fixing...")
                    frappe.set_user(username)

                g.current_user = username
                self.logger.debug(
                    f"Set g.current_user = {username}, frappe.session.user = {frappe.session.user}")

                if not hasattr(g, 'tenant_id') or not g.tenant_id:
                    tenant_id = get_user_tenant_id(username)
                    if tenant_id:
                        g.tenant_id = tenant_id
                        self.logger.debug(
                            f"Set g.tenant_id = {tenant_id} for user {username}")
                    else:
                        self.logger.warning(
                            f"No tenant_id found for user {username}")

                try:
                    result = f(username, *args, **kwargs)

                    if isinstance(result, dict):
                        return jsonify(result)

                    return result

                except (frappe.PermissionError, frappe.AuthenticationError) as e:
                    if hasattr(frappe, 'db') and frappe.db:
                        frappe.db.rollback()
                    g._frappe_rolled_back = True
                    self.logger.warning(
                        f"Access denied in {f.__name__}: {str(e)}\n{traceback.format_exc()}")
                    return jsonify({
                        "status": "error",
                        "message": str(e) or "You do not have permission to access this resource.",
                        "type": "PermissionError",
                        "code": 403,
                        "request_id": getattr(g, 'request_id', None),
                        "details": traceback.format_exc() if self.flask_app.debug else None
                    }), 403

                except (frappe.DoesNotExistError, frappe.LinkValidationError) as e:
                    if hasattr(frappe, 'db') and frappe.db:
                        frappe.db.rollback()
                    g._frappe_rolled_back = True
                    self.logger.warning(
                        f"Resource not found in {f.__name__}: {str(e)}")
                    return jsonify({
                        "status": "error",
                        "message": str(e) or "The requested resource was not found.",
                        "type": "DoesNotExistError",
                        "code": 404,
                        "request_id": getattr(g, 'request_id', None)
                    }), 404

                except (frappe.ValidationError, ValueError, TypeError, KeyError) as e:
                    if hasattr(frappe, 'db') and frappe.db:
                        frappe.db.rollback()
                    g._frappe_rolled_back = True
                    self.logger.warning(
                        f"Invalid request in {f.__name__}: {str(e)}")
                    return jsonify({
                        "status": "error",
                        "message": f"Invalid input data: {str(e)}",
                        "type": type(e).__name__,
                        "code": 400,
                        "request_id": getattr(g, 'request_id', None)
                    }), 400

                except Exception as e:
                    try:
                        frappe.db.rollback()
                        g._frappe_rolled_back = True
                    except Exception as rb_e:
                        self.logger.error(
                            f"Rollback failed: {rb_e}", exc_info=True)

                    self.logger.error(
                        f"Endpoint error in {f.__name__}: {e}\n{traceback.format_exc()}")

                    return jsonify({
                        "status": "error",
                        "message": "An internal server error occurred.",
                        "type": type(e).__name__,
                        "code": 500,
                        "request_id": getattr(g, 'request_id', None),
                        "details": traceback.format_exc() if self.flask_app.debug else None
                    }), 500

            self.flask_app.route(rule, **options)(wrapper)
            return wrapper

        return decorator

    def route(self, rule, **options):
        """
        Register a plain Flask route with no authentication. Use for health checks,
        webhooks, or public endpoints. For user-scoped endpoints use secure_route.
        """
        return self.flask_app.route(rule, **options)

    def _json_error_response(self, payload: dict, status_code: int):
        """
        Return (response, status_code). Uses jsonify when Flask app context exists
        (so auth mixin can return proper Response); otherwise returns raw (payload, status_code)
        for edge cases where there is no app context.
        """
        if has_app_context():
            return jsonify(payload), status_code
        return payload, status_code

    @property
    def db(self):
        """
        Direct access to Frappe's database object (frappe.db). Prefer app.tenant_db
        for tenant-scoped queries; use this only when you need raw DB access and
        will apply tenant filtering yourself.
        """
        return frappe.db

    def run_background_task(self, func, *args, **kwargs):
        """
        Executes a function in a background thread with the Frappe context restored.
        This ensures that thread-local proxies like frappe.db and frappe.qb are 
        properly bound.
        """
        base_ctx = self._frappe_local_base
        
        def _task_wrapper():
            with self.flask_app.app_context():
                try:
                    # Restore Frappe context
                    if base_ctx:
                        _contextvar.set(copy.copy(base_ctx))
                    
                    # Establish database connection for this thread
                    frappe.connect()
                    
                    # Execute the task
                    func(*args, **kwargs)
                    
                    # Commit any changes
                    frappe.db.commit()
                except Exception as e:
                    self.logger.error(f"Background task failed: {e}", exc_info=True)
                    try:
                        frappe.db.rollback()
                    except Exception:
                        pass
                finally:
                    try:
                        frappe.destroy()
                    except Exception:
                        pass

        thread = threading.Thread(target=_task_wrapper, daemon=True)
        thread.start()
        return thread

    def __call__(self, environ, start_response):
        """WSGI entry point so Gunicorn can use SERVICE_APP (e.g. server:app) directly."""
        return self.flask_app(environ, start_response)

    def run(self, **kwargs):
        """
        Start the Flask development server. Ensures site_config.json exists via
        create_site_config(), then runs flask_app.run() with host/port/debug from
        kwargs or defaults (0.0.0.0, self.port, False). For production use a WSGI
        server (gunicorn, uwsgi) and run the flask_app object instead.
        """
        self.logger.info("=" * 60)
        self.logger.info(f"Starting {self.name}")
        self.logger.info("=" * 60)
        self.logger.info(f"Site: {self.frappe_site}")
        self.logger.info(f"Central Site: {self.central_site_url}")
        self.logger.info(f"Port: {self.port}")
        self.logger.info("=" * 60)

        run_args = {
            'host': kwargs.pop('host', '0.0.0.0'),
            'port': kwargs.pop('port', self.port),
            'debug': kwargs.pop('debug', False)
        }

        self.flask_app.run(
            **run_args,
            **kwargs
        )


def create_microservice(name, **config):
    """
    Factory that creates and returns a MicroserviceApp instance. Pass any
    MicroserviceApp.__init__ keyword (central_site_url, frappe_site, port,
    load_framework_hooks, etc.) as config. This is the recommended entry point
    for new microservices.
    """
    return MicroserviceApp(name, **config)
