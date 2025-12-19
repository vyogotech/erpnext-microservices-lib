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

__version__ = "1.0.0"
__author__ = "Frappe MS Team"

from .core import MicroserviceApp, create_microservice, TenantAwareDB, get_user_tenant_id
from .controller import (
    DocumentController,
    ControllerRegistry,
    register_controller,
    get_controller_registry,
    setup_controllers
)

__all__ = [
    'MicroserviceApp',
    'create_microservice',
    'TenantAwareDB',
    'DocumentController',
    'ControllerRegistry',
    'register_controller',
    'get_controller_registry',
    'setup_controllers',
    'get_user_tenant_id',
    '__version__'
]
