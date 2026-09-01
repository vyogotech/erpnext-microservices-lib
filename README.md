# Frappe Microservice Framework

Build secure, independently deployable microservices on top of Frappe and
ERPNext — without changing how you write Frappe code.

Each service owns its own database (bounded context) while a **Central Site**
stays the single source of truth for authentication. Your DocTypes, controllers
and hooks work as they always have; the framework handles isolation, auth,
tenancy and container startup.

The goal is gradual decomposition: peel Sales, HR or Inventory out of an ERPNext
monolith one module at a time, rather than attempting a big-bang rewrite.

📖 **[Documentation](https://vyogotech.github.io/erpnext-microservices-lib/)**

---

## Why

- **Auto-CRUD** — one line exposes a DocType as a documented REST resource.
- **Secure by default** — every endpoint authenticates against the Central Site
  and receives the resolved user.
- **Bounded context** — a service owns its schema and database.
- **Optional multi-tenancy** — automatic `tenant_id` scoping, or turn it off and
  run against a stock site.
- **Frappe-native** — controllers and lifecycle hooks, no changes to Frappe core.
- **Container-first** — the base image ships the entrypoint; a service needs only
  a `server.py`.

Requires **Frappe v16** and **Python 3.10+**.

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
app = create_microservice("orders-service")
app.register_resource("Sales Order")
app.run()
```

That registers, documented in Swagger at `/apidocs`:

```
GET    /api/resource/sales-order         list
POST   /api/resource/sales-order         create
GET    /api/resource/sales-order/{name}  read
PUT    /api/resource/sales-order/{name}  update
DELETE /api/resource/sales-order/{name}  delete
```

→ [Getting started](https://vyogotech.github.io/erpnext-microservices-lib/getting-started/)
· [Core features](https://vyogotech.github.io/erpnext-microservices-lib/features/)
for controllers, document hooks, background jobs and the Central Site client.

## Tenancy

Two modes, selected by `MULTI_TENANT`:

| | `MULTI_TENANT=1` (default) | `MULTI_TENANT=0` |
|---|---|---|
| Site | Needs a Central Site tenancy provider | Stock Frappe/ERPNext |
| Reads | Scoped by `tenant_id` | Unscoped |
| Writes | Stamped with `tenant_id` | Not stamped |
| Role permissions | Enforced | Enforced |

Multi-tenant mode expects a Central Site app that adds a `tenant_id` column to
the tables and returns `tenant_id` in the session-validation response. That is
the whole contract — any app meeting it works.

Single-tenant mode drops the requirement and runs against a stock site. Note an
*empty* `MULTI_TENANT` is **not** "off": it falls back to the default, so a
missing ConfigMap key cannot silently drop tenant isolation. Set it to `0`
explicitly.

→ [Tenancy](https://vyogotech.github.io/erpnext-microservices-lib/tenancy/)

## Security posture

Enforcement features ship in **warn-only** mode — they log what *would* be
blocked but block nothing — and flip to `enforce` in 2.0. Deploy, grep the logs
for `[would-deny]`, fix what surfaces, then enforce.

| Variable | Values | Default | 2.0 |
|---|---|---|---|
| `PERMISSION_MODE` | `off\|warn\|enforce` | `warn` | `enforce` |
| `CSRF_MODE` | `off\|warn\|enforce` | `warn` | `enforce` |
| `TENANT_STRICT` | `off\|warn\|enforce` | `warn` | `enforce` |
| `MASK_PERMISSION_ERRORS` | `1/true/yes/on` | off | on |

**Set these explicitly in production** rather than relying on either default.

→ [Security](https://vyogotech.github.io/erpnext-microservices-lib/security/)

## Configuration

Every option is an environment variable; most also have a
`create_microservice(...)` keyword argument, which takes precedence. See
[`.env.example`](.env.example) for an annotated starting point and the
[configuration reference](https://vyogotech.github.io/erpnext-microservices-lib/configuration/)
for the complete set.

## Container deployment

The base image runs the library entrypoint, so a service needs no
`entrypoint.py`:

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

The entrypoint writes `site_config.json` from the environment, runs the DocType
schema sync once before Gunicorn forks, and execs Gunicorn. Liveness (`/health`)
and readiness (`/health/ready`) probes are built in.

Every build publishes immutable `sha-<full>` tags — prefer those in production.
`latest` only ever tracks `main`.

## Development

```bash
pip install -e ".[dev]"

pytest -m "not integration" tests/   # unit tests (80% coverage gate)
behave features/                     # BDD suite
flake8 frappe_microservice/          # what CI lints with
./scripts/run_integration_tests.sh   # integration (starts a container)
```

Integration tests run against the public `docker.io/vyogo/erpnext:sne-version-16`
image in single-tenant mode, so they need no registry credentials. Override with
`CENTRAL_SITE_IMAGE`.

→ [Developer guide](https://vyogotech.github.io/erpnext-microservices-lib/development/)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md). Install the hooks before your first
commit:

```bash
make setup
```

## License

[MIT](LICENSE)
