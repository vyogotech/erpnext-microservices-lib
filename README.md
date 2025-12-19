# Frappe Microservice Framework

A Python framework for building secure, isolated Frappe microservices with proper bounded context and multi-tenant support.

## Features

- 🔒 **Secure by Default**: All endpoints require authentication via Central Site
- 🔌 **Independent Database**: Each microservice has its own database (bounded context principle)
- 👤 **User Context Injection**: Authenticated user is automatically injected into handlers
- 🚀 **Zero Boilerplate**: Create CRUD APIs with a single line of code
- 🏢 **Multi-Tenant Ready**: Built-in support for tenant isolation
- 🛡️ **Tenant-Aware Database**: Automatic tenant_id filtering prevents cross-tenant data access
- 🪝 **Document Hooks**: Frappe-style lifecycle hooks without modifying Frappe core
- 📦 **Frappe-Native**: Seamlessly works with Frappe DocTypes and APIs

## Installation

```bash
pip install frappe-microservice
```

Or install from source:

```bash
cd frappe-microservice-lib
pip install -e .
```

## Quick Start

### Basic Microservice

```python
from frappe_microservice import create_microservice

# Initialize microservice
app = create_microservice("my-service")

# Create a secure endpoint (authentication required)
@app.secure_route('/hello', methods=['GET'])
def hello(user):
    """User is automatically injected after authentication"""
    return {"message": f"Hello {user}!"}

# Start the service
app.run()
```

### Automatic CRUD with Resource API

```python
from frappe_microservice import create_microservice

app = create_microservice("orders-service")

# Register a DocType for automatic CRUD (zero code!)
app.register_resource("Sales Order")

# This automatically creates:
# GET    /api/resource/sales-order        - List orders
# POST   /api/resource/sales-order        - Create order
# GET    /api/resource/sales-order/{name} - Get order
# PUT    /api/resource/sales-order/{name} - Update order
# DELETE /api/resource/sales-order/{name} - Delete order

app.run()
```

### Custom Business Logic

```python
from frappe_microservice import create_microservice
import frappe

app = create_microservice("signup-service")

@app.secure_route('/signup', methods=['POST'])
def signup_company(user):
    """Create a new company with multi-tenant isolation"""
    from flask import request
    data = request.json

    # Create company with tenant_id for isolation
    company = frappe.get_doc({
        "doctype": "Company",
        "company_name": data['company_name'],
        "tenant_id": generate_tenant_id(),
        "admin_user": user
    })
    company.insert()

    return {
        "success": True,
        "company_id": company.name,
        "tenant_id": company.tenant_id
    }

app.run()
```

### Tenant-Aware Database (Automatic Tenant Isolation)

The **TenantAwareDB** wrapper automatically adds `tenant_id` to all queries, preventing accidental cross-tenant data access:

```python
from frappe_microservice import create_microservice

app = create_microservice("my-service")

@app.secure_route('/users', methods=['GET'])
def list_users(user):
    # Get tenant for authenticated user
    tenant_id = get_user_tenant_id(user)
    app.set_tenant_id(tenant_id)

    # ✅ Automatically adds tenant_id filter!
    users = app.tenant_db.get_all(
        'User',
        fields=['name', 'email', 'role']
    )

    return {"data": users}

@app.secure_route('/users/<user_id>', methods=['GET'])
def get_user(user, user_id):
    tenant_id = get_user_tenant_id(user)
    app.set_tenant_id(tenant_id)

    try:
        # ✅ Automatically verifies tenant ownership!
        user_doc = app.tenant_db.get_doc('User', user_id)
        return user_doc.as_dict()
    except frappe.PermissionError:
        return {"error": "Access denied"}, 403
```

**Why TenantAwareDB?**

- **Secure by Default**: Impossible to forget tenant_id filter
- **Automatic Filtering**: All get_all/get_doc calls are tenant-scoped
- **Prevents Leaks**: Raises PermissionError if accessing other tenant's data
- **Zero Boilerplate**: No manual tenant_id filtering needed

See [TENANT_AWARE_DB_EXAMPLE.py](TENANT_AWARE_DB_EXAMPLE.py) for complete examples.

### Document Lifecycle Hooks (No Frappe Modifications!)

Register hooks for document lifecycle events **without modifying Frappe core**. Perfect for microservices!

```python
from frappe_microservice import create_microservice

app = create_microservice("orders-service")

# Global hook - runs for ALL doctypes
@app.tenant_db.before_insert('*')
def ensure_tenant_id(doc):
    """Ensure tenant_id is set on all documents"""
    from flask import g
    if not doc.tenant_id:
        doc.tenant_id = g.tenant_id

# DocType-specific hooks
@app.tenant_db.before_insert('Sales Order')
def set_order_defaults(doc):
    """Set defaults for new orders"""
    if not doc.status:
        doc.status = 'Draft'
    if not doc.order_date:
        doc.order_date = frappe.utils.today()

@app.tenant_db.after_insert('Sales Order')
def send_order_notification(doc):
    """Send notification after order creation"""
    print(f"📧 Order {doc.name} created for {doc.customer}")

@app.tenant_db.before_validate('Sales Order')
def validate_order_amount(doc):
    """Custom validation"""
    if doc.grand_total and doc.grand_total < 0:
        frappe.throw("Order total cannot be negative")

# Use in endpoints - hooks run automatically!
@app.secure_route('/orders', methods=['POST'])
def create_order(user):
    from flask import request
    
    tenant_id = get_user_tenant_id(user)
    app.set_tenant_id(tenant_id)
    
    # All hooks run automatically during insert!
    doc = app.tenant_db.insert_doc('Sales Order', request.json)
    
    return {"success": True, "name": doc.name}
```

**Supported Hook Events:**
- `before_validate` - Before validation starts
- `validate` - During validation
- `before_insert` - Before inserting into database
- `after_insert` - After inserting into database
- `before_update` - Before updating document
- `after_update` - After updating document
- `before_delete` - Before deleting document
- `after_delete` - After deleting document

**Why Document Hooks?**
- 🚫 **No Frappe Core Changes**: Works entirely in microservice layer
- 🎯 **Microservice-Specific**: Each service has its own hooks
- 🔧 **Full Control**: Easy to test and debug
- 📝 **Clean Code**: Separate business logic from endpoints
- 🔄 **Reusable**: Write once, applies to all operations

See [DOCUMENT_HOOKS_EXAMPLES.py](DOCUMENT_HOOKS_EXAMPLES.py) for comprehensive examples.

### Traditional DocType Controllers

Use familiar Frappe-style controller classes for your DocTypes! No Frappe core modifications needed.

```python
# controllers/sales_order.py
from frappe_microservice.controller import DocumentController

class SalesOrder(DocumentController):
    def validate(self):
        """Validate order data"""
        if not self.customer:
            self.throw("Customer is required")
        self.calculate_total()
    
    def before_insert(self):
        """Set defaults"""
        if not self.status:
            self.status = 'Draft'
        if not self.order_date:
            self.order_date = frappe.utils.today()
    
    def after_insert(self):
        """Post-creation tasks"""
        self.send_notification()
        self.update_customer_stats()
    
    def calculate_total(self):
        """Reusable method"""
        self.grand_total = sum(item.amount for item in self.items)
    
    def send_notification(self):
        print(f"Order {self.name} created")

# server.py
from frappe_microservice import create_microservice, setup_controllers

app = create_microservice("orders-service")

# Auto-discover and register controllers
setup_controllers(app, controllers_directory="./controllers")

# Controllers run automatically during insert/update!
@app.secure_route('/orders', methods=['POST'])
def create_order(user):
    doc = app.tenant_db.insert_doc('Sales Order', request.json)
    return {"success": True, "name": doc.name}
```

**Features:**
- 🎯 **Traditional Pattern**: Familiar Frappe controller style
- 📁 **One File Per DocType**: Clean code organization
- 🔄 **Auto-Discovery**: Automatically loads from directory
- 🎭 **Lifecycle Methods**: validate(), before_insert(), after_insert(), etc.
- 🛠️ **Custom Methods**: Define reusable business logic
- 🧪 **Easy Testing**: Test controllers independently

**File Naming Convention:**
- `sales_order.py` → `Sales Order` DocType → `SalesOrder` class
- `signup_user.py` → `Signup User` DocType → `SignupUser` class

See [signup-service/](../signup-service/) for a complete example with controllers.

## Configuration

### Environment Variables

```bash
# Frappe configuration
export FRAPPE_SITE="dev.localhost"
export FRAPPE_SITES_PATH="/home/frappe/frappe-bench/sites"

# Database (for independent microservice DB)
export DB_HOST="signup-db"

# Central Site for authentication
export CENTRAL_SITE_URL="http://central-site:8000"
```

### Programmatic Configuration

```python
app = MicroserviceApp(
    name="my-service",
    central_site_url="http://central-site:8000",
    frappe_site="dev.localhost",
    sites_path="/home/frappe/frappe-bench/sites",
    db_host="my-service-db",  # Independent database
    port=8000
)
```

## Architecture: Bounded Context

Each microservice should have its own database for true isolation:

```yaml
services:
  # Signup Microservice
  signup-service:
    image: frappe-ms
    environment:
      - DB_HOST=signup-db
    depends_on:
      - signup-db

  # Independent Database
  signup-db:
    image: mariadb:10.6
    environment:
      MYSQL_DATABASE: signup_db
      MYSQL_USER: signup_user
      MYSQL_PASSWORD: signup_pass

## ERPNext Decomposition Strategy

The Frappe Microservice Framework is designed to facilitate the **gradual decomposition of the ERPNext monolith**. Instead of a high-risk full migration, you can incrementally extract specific domains (e.g., Sales, HR, Inventory) into dedicated microservices:

1.  **Define Bounded Contexts**: Create independent databases for specific ERPNext modules.
2.  **Modular Migration**: Use the framework to build services that handle specific DocTypes.
3.  **Unified Authentication**: Keep the Central Site as the single point of entry for user sessions.
4.  **Independent Scaling**: Scale critical services (like Order processing) independently from the rest of the ERP.

This strategy reduces technical debt and enables a more agile, resilient architecture.
```

## Multi-Tenant Isolation

All data should include a `tenant_id` for isolation:

```python
# Create with tenant isolation
company = frappe.get_doc({
    "doctype": "Company",
    "tenant_id": tenant_id,
    "company_name": "Acme Corp"
})

# Query with tenant filter
companies = frappe.get_all(
    "Company",
    filters={"tenant_id": tenant_id}
)
```

## API Reference

### MicroserviceApp

The main class for creating microservices.

#### Methods

- `secure_route(rule, **options)` - Register an authenticated endpoint
- `route(rule, **options)` - Register a public endpoint
- `register_resource(doctype, **options)` - Auto-create CRUD endpoints
- `run(**kwargs)` - Start the microservice

### create_microservice(name, \*\*config)

Quick setup function for creating a microservice.

## Examples

See the `examples/` directory for complete examples:

- `examples/signup-service/` - Multi-tenant user signup
- `examples/orders-service/` - Order management with CRUD
- `examples/notifications-service/` - Event-driven notifications

## Development

```bash
# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black frappe_microservice/

# Lint
flake8 frappe_microservice/
```

## License

MIT License

## Contributing

Contributions are welcome! Please see CONTRIBUTING.md for details.
