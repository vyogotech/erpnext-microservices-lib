"""
Re-export layer for backward compatibility.

After modularization, the main implementation lives in app.py, hooks.py, tenant.py,
isolation.py, auth.py, and resources.py. This module re-exports the public API so
that existing imports continue to work without change:

    from frappe_microservice.core import MicroserviceApp, create_microservice
    from frappe_microservice.core import TenantAwareDB, get_user_tenant_id
    from frappe_microservice.core import DocumentHooks, NullContext

New code can import from the concrete modules (e.g. from frappe_microservice.app
import MicroserviceApp) or keep using this module. __all__ defines the public surface.
"""

from frappe_microservice.hooks import DocumentHooks                          # noqa: F401
from frappe_microservice.tenant import NullContext, get_user_tenant_id, TenantAwareDB  # noqa: F401
from frappe_microservice.app import MicroserviceApp, create_microservice     # noqa: F401

__all__ = [
    'NullContext',
    'DocumentHooks',
    'get_user_tenant_id',
    'TenantAwareDB',
    'MicroserviceApp',
    'create_microservice',
]
