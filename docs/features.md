# Core Features

## Document Controllers

Document Controllers allow you to organize your business logic into Frappe-style classes.

```python
# controllers/sales_order.py
from frappe_microservice.controller import DocumentController

class SalesOrder(DocumentController):
    def validate(self):
        if not self.customer:
            self.throw("Customer is required")
```

Register them in your `server.py`:
```python
from frappe_microservice import setup_controllers
setup_controllers(app, controllers_directory="./controllers")
```

## Document Hooks

Add custom logic to lifecycle events without modifying core frameworks.

```python
@app.tenant_db.on('Sales Order', 'before_insert')
def set_order_defaults(doc):
    if not doc.status:
        doc.status = 'Draft'
```

Supported events include `before_validate`, `validate`, `before_insert`, `after_insert`, `before_update`, `after_update`, `before_delete`, and `after_delete`.

## Standardized Error Handling

All framework responses follow a consistent JSON format:

```json
{
  "status": "error",
  "message": "Human readable message",
  "type": "ExceptionClass",
  "code": 4xx
}
```

The framework automatically maps Frappe exceptions to appropriate HTTP status codes:
- `PermissionError` -> 403
- `DoesNotExistError` -> 404
- `ValidationError` -> 400
- Authentication failures -> 401

## Tenant Safety on Writes

`TenantAwareDB` enforces tenant isolation during write operations:

- Parent and child rows are synchronized to the active `tenant_id` before `insert_doc` and `update_doc` persistence.
- Link fields are validated before save/insert. Cross-tenant references are rejected unless the linked document belongs to the same tenant or `SYSTEM`.

This protects against accidental tenant drift introduced by appended child rows or manually supplied Link values.
