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

Exception strings and tracebacks are only included in `500` bodies when
`DEBUG=1` (or Flask debug is on); otherwise `error`/`details` are `null`.

## Automatic tenant resolution

`secure_route` authenticates the caller and resolves their tenant via
`get_user_tenant_id(user)` into `g.tenant_id`. Every `app.tenant_db` query is
then automatically scoped to that tenant — you rarely need
`app.set_tenant_id(...)` (only for custom resolution).

## Health & readiness

- `GET /health` — liveness; reports the real DB connection state
  (`connected`/`pending`) without querying.
- `GET /health/ready` — readiness; runs `SELECT 1` and returns `503` if the DB
  is unreachable.

## Security modes

Read/write authorization (`PERMISSION_MODE`), CSRF (`CSRF_MODE`), tenant
strictness (`TENANT_STRICT`), and cross-tenant masking
(`MASK_PERMISSION_ERRORS`) are configurable; see the README "Security
configuration" table for defaults and the 2.0 roadmap.
