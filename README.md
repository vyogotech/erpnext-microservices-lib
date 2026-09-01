# Frappe Microservice Framework

Build secure, independently deployable microservices on top of Frappe and ERPNext —
without changing how you write Frappe code.

Each service owns its own database (bounded context) while a **Central Site** stays
the single source of truth for authentication. Your DocTypes, controllers and hooks
work as they always have; the framework handles isolation, auth, tenancy and
container startup.

The goal is gradual decomposition: peel Sales, HR or Inventory out of an ERPNext
monolith one module at a time, rather than attempting a big-bang rewrite.

📖 **[Full documentation](https://vyogotech.github.io/erpnext-microservices-lib/)**

---

## Why

- **Secure by default** — every endpoint authenticates against the Central Site;
  the authenticated user is injected into your handler.
- **Zero-boilerplate CRUD** — one line exposes a DocType as a documented REST resource.
- **Bounded context** — a service owns its schema and database.
- **Optional multi-tenancy** — automatic `tenant_id` scoping on every read and write,
  or turn it off entirely and run against a stock site.
- **Frappe-native** — DocTypes, document lifecycle hooks and controllers, with no
  modifications to Frappe core.
- **Container-first** — the base image ships the entrypoint; a service needs only a
  `server.py`.

## Installation

```bash
pip install frappe-microservice
```

## Quick start

A secure endpoint:

```python
from frappe_microservice import create_microservice

app = create_microservice("my-service")

@app.secure_route("/hello", methods=["GET"])
def hello(user):
    # `user` is injected once the session is validated against the Central Site
    return {"message": f"Hello {user}!"}

app.run()
```

Automatic CRUD for a DocType — no handler code at all:

```python
from frappe_microservice import create_microservice

app = create_microservice("orders-service")
app.register_resource("Sales Order")
app.run()
```

That registers, fully documented in Swagger at `/apidocs`:

```
GET    /api/resource/sales-order         list
POST   /api/resource/sales-order         create
GET    /api/resource/sales-order/{name}  read
PUT    /api/resource/sales-order/{name}  update
DELETE /api/resource/sales-order/{name}  delete
```

→ [Getting started](https://vyogotech.github.io/erpnext-microservices-lib/getting-started/)
for controllers, document hooks, background jobs and the Central Site client.

## Tenancy

The framework runs in one of two modes, selected by `MULTI_TENANT`:

| | `MULTI_TENANT=1` (default) | `MULTI_TENANT=0` |
|---|---|---|
| Site | Needs a Central Site tenancy provider | Stock Frappe/ERPNext |
| Reads | Scoped by `tenant_id` | Unscoped |
| Writes | Stamped with `tenant_id` | Not stamped |
| Role permissions | Enforced | Enforced |

Multi-tenant mode expects a Central Site app that adds a `tenant_id` column to the
tables and returns `tenant_id` in the session-validation response. That is the whole
contract — any app meeting it works.

Single-tenant mode drops the requirement and runs against a stock site. Note that an
*empty* `MULTI_TENANT` value is not "off": it falls back to the default, so a missing
ConfigMap key cannot silently drop tenant isolation. Set it to `0` explicitly.

→ [Architecture & isolation](https://vyogotech.github.io/erpnext-microservices-lib/architecture/)

## Configuration

Configure by environment variable or keyword argument to `create_microservice()`.
See [`.env.example`](.env.example) for the full annotated set, and the
[configuration reference](https://vyogotech.github.io/erpnext-microservices-lib/getting-started/)
for details.

The security policy flags (`PERMISSION_MODE`, `CSRF_MODE`, `TENANT_STRICT`) ship as
`warn` — log-only, no behaviour change — and become `enforce` by default in 2.0.
Turn them on early to see what would break.

## Container deployment

The base image runs the library entrypoint, so a service needs no `entrypoint.py`:

```dockerfile
FROM ghcr.io/vyogotech/erpnext-microservices-lib:latest
WORKDIR /app
COPY . /app/service/
RUN touch /app/service/__init__.py
# expose as a Frappe app so the framework can load it ("my-service" -> my_service)
RUN ln -sf /app/service /app/sites/apps/my_service
ENV SERVICE_PATH=/app/service
ENV SERVICE_APP=server:app
EXPOSE 8000
```

## Development

```bash
pip install -e ".[dev]"

pytest -m "not integration" tests/   # unit tests
behave features/                     # BDD suite
./scripts/run_integration_tests.sh   # integration (starts a container)
```

Integration tests run against the public `docker.io/vyogo/erpnext` SNE image in
single-tenant mode, so they need no registry credentials. Override the image with
`CENTRAL_SITE_IMAGE`.

→ [Developer guide](https://vyogotech.github.io/erpnext-microservices-lib/development/)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and our [Code of Conduct](CODE_OF_CONDUCT.md).
Install the pre-commit hooks before your first commit:

```bash
pip install pre-commit && pre-commit install
```

## License

[MIT](LICENSE)
