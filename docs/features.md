# Core Features

## Auto-CRUD resources

`register_resource()` exposes a DocType as a REST resource — authenticated,
tenant-scoped and documented in Swagger, with no handler code:

```python
app = create_microservice("orders-service")
app.register_resource("Sales Order")
```

```
GET    /api/resource/sales-order         list
POST   /api/resource/sales-order         create
GET    /api/resource/sales-order/{name}  read
PUT    /api/resource/sales-order/{name}  update
DELETE /api/resource/sales-order/{name}  delete
```

List routes accept the usual Frappe query parameters (`filters`, `fields`,
`limit`, `order_by`).

## Secure endpoints

`secure_route` authenticates the caller against the Central Site and injects the
resolved user:

```python
@app.secure_route("/orders/summary", methods=["GET"])
def order_summary(user):
    orders = app.tenant_db.get_all("Sales Order", filters={"status": "Draft"})
    return {"user": user, "count": len(orders)}
```

Use `app.route(...)` for a deliberately public endpoint — it takes no `user`.

## Document controllers

Organise business logic in Frappe-style classes:

```python
# controllers/sales_order.py
from frappe_microservice.controller import DocumentController

class SalesOrder(DocumentController):
    def validate(self):
        if not self.customer:
            self.throw("Customer is required")

    def before_insert(self):
        self.status = self.status or "Draft"
```

Register them once:

```python
from frappe_microservice import setup_controllers
setup_controllers(app, controllers_directory="./controllers")
```

Controllers are auto-discovered from `$SERVICE_PATH/controllers/` when that
directory exists, and run during insert and update — including from background
jobs.

## Document hooks

For logic that does not warrant a class, or that spans DocTypes:

```python
@app.tenant_db.on("Sales Order", "before_insert")
def set_defaults(doc):
    doc.status = doc.status or "Draft"

@app.tenant_db.on("*", "after_insert")     # every DocType
def audit(doc):
    app.logger.info("created %s %s", doc.doctype, doc.name)
```

Events: `before_validate`, `validate`, `before_insert`, `after_insert`,
`before_update`, `after_update`, `before_delete`, `after_delete`.

## Background tasks

An embedded RQ worker runs as a daemon thread inside the service container — no
separate worker deployment. It is **off by default**; enable with `ENABLE_RQ=1`.

```python
@app.secure_route("/reports", methods=["POST"])
def trigger_report(user):
    app.enqueue_task(build_report, user, period="monthly")
    return {"status": "enqueued"}

def build_report(user, period=None):
    import frappe
    # full Frappe API available — context is restored for you
    frappe.get_doc({...}).insert()
    frappe.db.commit()
```

The worker restores `frappe.local` and the database connection, so jobs behave
like request handlers. Controller discovery works there too.

Calling `enqueue_task()` with `ENABLE_RQ` unset raises `RuntimeError` rather than
silently dropping the job.

!!! note "Job timeouts need a forking worker"
    `RQ_DEFAULT_JOB_TIMEOUT` sets a default, but the **embedded** worker is a
    `SimpleWorker` and cannot enforce it. For hard timeouts, set `WORKER_MODE=1`
    (which starts the queue without the thread) and run `frappe-ms worker` as a
    separate forking process.

RQ connects via `REDIS_URL`, which is **separate** from the `REDIS_HOST` /
`REDIS_PORT` pair that feeds `site_config.json`. Setting one does not configure
the other.

Background jobs run outside a request, so there is no session and no tenant
context. In multi-tenant mode, pass the tenant explicitly to anything a job writes.

## Central Site client

`app.central` calls back to the Central Site with a Frappe-like API. It is
**lazily initialised** — no connection is attempted until first use.

```python
@app.secure_route("/sync", methods=["POST"])
def sync(user):
    tenant = app.central.get_doc("Tenant", "tenant-123")
    app.central.update("Tenant", "tenant-123", {"status": "Active"})
    result = app.central.call("some_whitelisted_method", {"param": "value"})
    return {"status": "synced"}
```

Authenticate with either an API key/secret pair or a username/password — see
[Configuration](configuration.md#central-site).

## Swagger / OpenAPI

Every service exposes interactive docs at `/apidocs`, and the raw spec at
`/apispec_1.json`. Registered resources are documented automatically.

Document custom endpoints with YAML in the docstring:

```python
@app.secure_route("/orders/summary", methods=["GET"])
def order_summary(user):
    """
    Get an order summary for the current tenant
    ---
    tags: [Orders]
    parameters:
      - name: days
        in: query
        type: integer
        default: 7
    responses:
      200:
        description: Summary retrieved
    """
    return {"total_orders": 100}
```

## Health & readiness probes

Two endpoints, because liveness and readiness answer different questions:

- **`GET /health`** — liveness. Never queries the database; reports the
  connection state as `connected` or `pending`. Restarting a container because
  the database is briefly unreachable makes an outage worse, so this stays cheap.
- **`GET /health/ready`** — readiness. Runs `SELECT 1`; returns `200`, or `503`
  with `{"status": "not_ready"}` when the database is unreachable. Point your
  orchestrator's readiness probe here so traffic stops without a restart loop.

## Error handling

All framework errors share one JSON shape, and Frappe exceptions map to sensible
HTTP status codes. Tracebacks appear only under `DEBUG=1` — see
[Security](security.md#error-responses).

## Tenant-aware database

`app.tenant_db` wraps the Frappe ORM with tenant scoping, permission checks and
lifecycle hooks. Its behaviour depends on the tenancy mode — see
[Tenancy](tenancy.md).
