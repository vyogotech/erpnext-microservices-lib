# Frappe Microservice Framework

Build secure, independently deployable microservices on top of Frappe and
ERPNext — without changing how you write Frappe code.

Each service owns its own database while a **Central Site** stays the single
source of truth for authentication. Your DocTypes, controllers and hooks work as
they always have; the framework handles isolation, auth, tenancy and container
startup.

The goal is gradual decomposition: peel Sales, HR or Inventory out of an ERPNext
monolith one module at a time, rather than attempting a big-bang rewrite.

## What it gives you

- **Auto-CRUD** — `app.register_resource("Sales Order")` exposes a DocType as a
  documented REST resource with no handler code.
- **Secure by default** — every `secure_route` authenticates against the Central
  Site and injects the resolved user. OAuth2 Bearer tokens, `sid` cookies and an
  internal service token are all supported.
- **Bounded context** — a service owns its schema and database, so it can be
  deployed and scaled on its own.
- **Optional multi-tenancy** — automatic `tenant_id` scoping on every read and
  write, or turn it off entirely and run against a stock site.
- **Frappe-native** — DocType controllers and lifecycle hooks, with no changes to
  Frappe core.
- **Container-first** — the base image ships the entrypoint, syncs DocType schema
  at startup, and logs to stderr. Liveness and readiness probes are built in.
- **Background jobs** — an embedded RQ worker, with Frappe context restored.
- **Observability** — OpenTelemetry tracing and container-safe logging.

## Where to start

| | |
|---|---|
| [Getting Started](getting-started.md) | Install, first service, container deployment |
| [Configuration](configuration.md) | Every environment variable and kwarg |
| [Tenancy](tenancy.md) | Single- vs multi-tenant, and the Central Site contract |
| [Core Features](features.md) | CRUD, controllers, hooks, jobs, Swagger, probes |
| [Security](security.md) | Policy modes, authentication, SQL safety |
| [Architecture](architecture.md) | Bounded context, isolation, decomposition |
| [Observability](observability.md) | Tracing and logging |
| [CI/CD](ci-cd.md) | Pipeline, image tags, framework versions |
| [Developer Guide](development.md) | Tests, linting, where to make changes |
| [Dependency Slimming](dependency-slimming.md) | How the image stays small |

## Requirements

Frappe **v16** and Python **3.10+**.
