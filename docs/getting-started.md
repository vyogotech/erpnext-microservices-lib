# Getting Started

## Prerequisites

- **A Central Site** — a running Frappe site acting as the authentication
  provider. Services validate every request against it.
- **A tenancy provider app** — only for multi-tenant deployments. Single-tenant
  mode runs against a stock site; see [Tenancy](tenancy.md).

The library requires **Frappe v16**. It imports `frappe.utils.local`, which does
not exist in v15.

## Installation

```bash
pip install frappe-microservice
```

From source:

```bash
git clone https://github.com/vyogotech/erpnext-microservices-lib.git
cd erpnext-microservices-lib
pip install -e .
```

## Your first service

A `server.py` with one authenticated endpoint:

```python
from frappe_microservice import create_microservice

app = create_microservice("my-service")

@app.secure_route("/hello", methods=["GET"])
def hello(user):
    # `user` is injected once the session is validated against the Central Site
    return {"message": f"Hello {user}!"}

if __name__ == "__main__":
    app.run()
```

Point it at a Central Site and a database, then run it:

```bash
export CENTRAL_SITE_URL=http://central-site:8000
export FRAPPE_SITE=dev.localhost
export DB_HOST=localhost
export MULTI_TENANT=0        # stock site, no tenancy provider

python server.py
```

Interactive API docs are at `http://localhost:8000/apidocs`.

## Exposing a DocType

The shortest path from a DocType to a REST API:

```python
app = create_microservice("orders-service")
app.register_resource("Sales Order")
app.run()
```

That registers list, create, read, update and delete routes under
`/api/resource/sales-order`, all authenticated, tenant-scoped and documented in
Swagger. See [Core Features](features.md).

## Choosing a tenancy mode

| | `MULTI_TENANT=1` (default) | `MULTI_TENANT=0` |
|---|---|---|
| Site | Needs a Central Site tenancy provider | Stock Frappe/ERPNext |
| Reads | Scoped by `tenant_id` | Unscoped |
| Writes | Stamped with `tenant_id` | Not stamped |
| Role permissions | Enforced | Enforced |

Start with `MULTI_TENANT=0` if you are evaluating the library or serving a single
organisation — it needs nothing beyond a stock site. [Tenancy](tenancy.md)
covers the Central Site contract multi-tenant mode expects.

!!! warning "An empty value is not off"
    `MULTI_TENANT=""` resolves to the **default (on)**, so a missing ConfigMap
    key cannot silently drop tenant isolation. Disabling requires `MULTI_TENANT=0`.

## Running in a container

The base image ships the entrypoint, so a service needs no `entrypoint.py` — only
a `server.py` that creates the app:

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

| Variable | Purpose | Default |
|---|---|---|
| `SERVICE_PATH` | Directory holding your service code | `/app/service` |
| `SERVICE_APP` | WSGI target as `module:attr` | `server:app` |

The entrypoint (`python -m frappe_microservice.entrypoint`) writes
`site_config.json` from the environment, runs the DocType sync once, and then
execs Gunicorn. Use `SERVICE_APP=main:app` for a different module or attribute.

Use immutable image tags in production — every build publishes `sha-<full>`.
`latest` only ever tracks `main`. See [CI/CD](ci-cd.md#image-tags).

## Next steps

- [Configuration](configuration.md) — every environment variable and kwarg
- [Core Features](features.md) — controllers, hooks, background jobs, Swagger
- [Tenancy](tenancy.md) — modes and the Central Site contract
- [Security](security.md) — policy modes, authentication, SQL safety
- [Architecture](architecture.md) — bounded context and isolation
