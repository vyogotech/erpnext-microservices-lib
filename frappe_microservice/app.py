"""
MicroserviceApp -- the main application class for Frappe microservices.

This module defines the primary entry point for building microservices:
- MicroserviceApp: Flask-based app that composes IsolationMixin, AuthMixin, and ResourceMixin.
  It handles Frappe init per request, logging, OpenTelemetry, middleware (before/after request),
  secure_route (auth + tenant resolution + error handling), and run().
- create_microservice(): Factory that returns a configured MicroserviceApp instance.

Developers typically use: app = create_microservice("my-service") then @app.secure_route(...).
"""

from flask import Flask, request, jsonify, g, has_app_context
from functools import wraps
import frappe
import os
import logging
import traceback
import uuid
from frappe_microservice.entrypoint import create_site_config
from frappe_microservice.tenant import TenantAwareDB, get_user_tenant_id
from frappe_microservice.isolation import IsolationMixin
from frappe_microservice.auth import AuthMixin
from frappe_microservice.resources import ResourceMixin

try:
    from flasgger import Swagger
except ImportError:
    Swagger = None

from werkzeug.exceptions import HTTPException

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class MicroserviceApp(IsolationMixin, AuthMixin, ResourceMixin):
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
                 otel_exporter_url=None):
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

        if Swagger:
            self.swagger = Swagger(self.flask_app, template={
                "swagger": "2.0",
                "info": {
                    "title": f"{self.name} API",
                    "description": f"Interactive API documentation for {self.name}",
                    "version": "1.0.0"
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

    def _setup_middleware(self):
        """
        Register Flask before_request, after_request, and errorhandler.
        - before_request: If Frappe not yet initialized for this thread, patch app
          resolution, call frappe.init(), filter module maps, optionally override db_host,
          frappe.connect(), set session to Guest, patch hooks resolution; then set
          form_dict, request_ip, g._frappe_rolled_back, g.request_id.
        - after_request: Clear form_dict, commit DB (unless rolled back), add X-Request-ID to response.
        - errorhandler: Catch unhandled exceptions, log traceback, return JSON 500.
        """

        @self.flask_app.before_request
        def setup_frappe_context():
            """
            Initialize Frappe for each request/thread. Only runs full init when
            frappe.local.site is not set (first request or new thread). Order matters:
            patch app resolution -> init -> filter module maps -> connect -> patch hooks.
            """
            needs_init = False
            try:
                needs_init = not hasattr(frappe, 'local') or not hasattr(
                    frappe.local, 'site') or not frappe.local.site
            except (AttributeError, RuntimeError):
                needs_init = True

            if needs_init:
                self._patch_app_resolution()
                frappe.init(site=self.frappe_site, sites_path=self.sites_path)
                self._filter_module_maps()

                if self.db_host:
                    frappe.local.conf.db_host = self.db_host
                    self.logger.info(f"Overriding DB host to: {self.db_host}")

                frappe.connect(set_admin_as_user=False)

                if hasattr(frappe, 'session'):
                    frappe.session.user = 'Guest'
                    frappe.session.sid = None
                    self.logger.debug(
                        "Initialized session as Guest (will be set after validation)")

                self._patch_hooks_resolution()

            frappe.local.form_dict = frappe._dict()
            frappe.local.request_ip = request.remote_addr

            g._frappe_rolled_back = False

            g.request_id = request.headers.get('X-Request-ID', str(uuid.uuid4()))
            self.logger.info(f"Request started: {request.method} {request.path} [request_id={g.request_id}]")

        @self.flask_app.after_request
        def cleanup_frappe_context(response):
            """
            Clean up after each request: clear Frappe form_dict, commit the DB
            transaction (unless the request set g._frappe_rolled_back), and add
            X-Request-ID to response headers for tracing.
            """
            try:
                if hasattr(frappe, 'local') and hasattr(frappe.local, 'form_dict'):
                    frappe.local.form_dict.clear()

                if hasattr(frappe, 'db') and frappe.db:
                    if not getattr(g, '_frappe_rolled_back', False):
                        frappe.db.commit()
                        self.logger.debug("Transaction committed successfully")
                    else:
                        self.logger.debug("Skipped commit (transaction was rolled back)")

                if hasattr(frappe, 'session'):
                    self.logger.debug(
                        f"Request completed: user={frappe.session.user}, sid={frappe.session.sid} [request_id={getattr(g, 'request_id', 'unknown')}]")

                if hasattr(g, 'request_id'):
                    response.headers['X-Request-ID'] = g.request_id
            except Exception as e:
                self.logger.warning(f"Cleanup warning: {e}", exc_info=True)

            return response

        @self.flask_app.errorhandler(Exception)
        def handle_exception(e):
            """
            Global error handler: pass through HTTPException, otherwise log full
            traceback and return JSON 500 with optional details in debug mode.
            """
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
            return jsonify({
                "status": "healthy",
                "service": self.name,
                "site": self.frappe_site
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

        try:
            create_site_config()
            self.logger.info("Checked/Created site_config.json automatically.")
        except Exception as e:
            self.logger.error(f"Failed to automate site_config.json: {e}")

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
