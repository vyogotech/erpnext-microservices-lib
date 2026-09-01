"""
Frappe Microservices Framework

A Python library for building secure, isolated Frappe microservices with
proper bounded context and multi-tenant support.

Key Features:
- Session-based authentication via Central Site
- Automatic Frappe initialization and context management
- Secure-by-default endpoints with user injection
- Built-in CRUD resource APIs (Frappe-style /api/resource pattern)
- Independent database per service (bounded context principle)
- Multi-tenant data isolation
- Document lifecycle hooks
- Traditional DocType controllers

Quick Start:
    from frappe_microservice import create_microservice

    app = create_microservice("my-service")

    @app.secure_route('/hello', methods=['GET'])
    def hello(user):
        return {"message": f"Hello {user}!"}

    app.run()

For detailed documentation, visit: https://github.com/your-repo/frappe-microservice
"""

# ---------------------------------------------------------------------------
# Container-safe logging.
# Frappe's file log handlers resolve paths relative to the process CWD (a
# bench-era assumption) and crash with FileNotFoundError under a container
# WORKDIR or a read-only root filesystem. FRAPPE_STREAM_LOGGING=1 makes every
# frappe.logger() use a StreamHandler (stderr) instead of a RotatingFileHandler.
# This value is read at import time of frappe.utils.logger as a default param,
# so it MUST be set before any frappe import in this package. `setdefault`
# preserves an operator override (set FRAPPE_STREAM_LOGGING="" to opt out).
# ---------------------------------------------------------------------------
import os

os.environ.setdefault("FRAPPE_STREAM_LOGGING", "1")

__version__ = "1.6.0"
__author__ = "Frappe MS Team"

# E402 throughout: these imports must follow the os.environ.setdefault above,
# which has to run before anything pulls in frappe. Moving them to the top of
# the file would silently disable stream logging.
from .core import (  # noqa: E402
    MicroserviceApp,
    create_microservice,
    TenantAwareDB,
    get_user_tenant_id,
)
from .app import get_app  # noqa: E402
from .naming import resolve_or_create, tenant_scoped_docname  # noqa: E402
from .controller import (  # noqa: E402
    DocumentController,
    ControllerRegistry,
    register_controller,
    get_controller_registry,
    setup_controllers,
)

__all__ = [
    "MicroserviceApp",
    "create_microservice",
    "TenantAwareDB",
    "DocumentController",
    "ControllerRegistry",
    "register_controller",
    "get_controller_registry",
    "setup_controllers",
    "get_user_tenant_id",
    "get_app",
    "resolve_or_create",
    "tenant_scoped_docname",
    "__version__",
]
