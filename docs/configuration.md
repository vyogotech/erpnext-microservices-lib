# Configuration

Every option is an environment variable; many also have a
`create_microservice(...)` keyword argument. **The keyword argument wins over the
environment variable, which wins over the default.**

## How values resolve

Two helpers in `frappe_microservice/policy.py` parse configuration, and their
empty-value handling is deliberate:

**Boolean flags** (`resolve_bool`) accept `1`, `true`, `yes`, `on`
(case-insensitive). An **unset or empty** variable falls back to the default —
so a Kubernetes `env:` entry with no value, or a missing ConfigMap key, cannot
silently flip a security flag that defaults on. Any *other* non-empty string is
read as false: `MULTI_TENANT=disabled` means off, not the default.

**Tri-state modes** (`resolve_mode`) accept `off`, `warn`, `enforce`. Unset falls
back to the default silently; an unknown *or empty* value falls back with a
logged warning rather than failing to boot.

## Tenancy

| Variable | Kwarg | Values | Default | Purpose |
|---|---|---|---|---|
| `MULTI_TENANT` | `multi_tenant` | `1/true/yes/on` | **on** | Master switch for tenant isolation. See [Tenancy](tenancy.md) |

## Security policy modes

See [Security](security.md) for what each mode does and how to roll them out.

| Variable | Kwarg | Values | Default | 2.0 default |
|---|---|---|---|---|
| `PERMISSION_MODE` | `permission_mode` | `off\|warn\|enforce` | `warn` | `enforce` |
| `CSRF_MODE` | `csrf_mode` | `off\|warn\|enforce` | `warn` | `enforce` |
| `TENANT_STRICT` | `tenant_strict` | `off\|warn\|enforce` | `warn` | `enforce` |
| `MASK_PERMISSION_ERRORS` | `mask_permission_errors` | `1/true/yes/on` | off | on |
| `INTERNAL_SERVICE_TOKEN` | — | secret | unset (path disabled) | — |
| `INTERNAL_SERVICE_USER` | — | username | `Administrator` | — |
| `ALLOW_CORS` | — | origin string | `*` **and a warning** | explicit |

`ALLOW_CORS` is written into `site_config.json`. An explicitly empty value is
honoured as-is (effectively disabling CORS) rather than falling back to `*`.

## Site & database

| Variable | Kwarg | Default | Purpose |
|---|---|---|---|
| `FRAPPE_SITE` | `frappe_site` | `site1.local` | Frappe site name |
| `FRAPPE_SITES_PATH` | `sites_path` | `/app/sites` | Sites directory |
| `DB_HOST` | `db_host` | unset | Service database host |
| `DB_PORT` | — | `3306` | Port; ignored if `DB_HOST_PORT` is set |
| `DB_HOST_PORT` | — | unset | Kubernetes-style port; takes precedence |
| `DB_NAME` | — | empty | Database name |
| `DB_USER` | — | falls back to `DB_NAME`, then `frappe` | Database user |
| `DB_PASSWORD` | — | `changeme` | Database password |
| `ENCRYPTION_KEY` | — | unset | Frappe `encryption_key`; overridden by `/secrets/encryption_key.txt` when present |
| `REDIS_HOST` | — | `localhost` | Base Redis host |
| `REDIS_PORT` | — | `6379` | Redis port |
| `REDIS_CACHE_HOST` | — | `REDIS_HOST` | Overrides the cache host only |
| `REDIS_QUEUE_HOST` | — | `REDIS_HOST` | Overrides the queue host only |
| `REDIS_NAMESPACE` | — | unset (key omitted) | Frappe `redis_namespace` |

!!! warning "Set `FRAPPE_SITE` explicitly"
    Left unset, the application initialises site `site1.local` while
    `site_config.json` is written under `dev.localhost`. The two only agree when
    you set `FRAPPE_SITE`. Always set it in a container.

## Central Site

| Variable | Default | Purpose |
|---|---|---|
| `CENTRAL_SITE_URL` | `http://central-site:8000` | Base URL of the Central Site |
| `CENTRAL_SITE_API_KEY` | unset | API key (token auth) |
| `CENTRAL_SITE_API_SECRET` | unset | API secret (token auth) |
| `CENTRAL_SITE_USER` | unset | Username (password auth) |
| `CENTRAL_SITE_PASSWORD` | unset | Password (password auth) |
| `CENTRAL_SITE_TIMEOUT` | `10` | **Currently read but not applied** to the client |

Use either the key/secret pair or the user/password pair.

## Service discovery & container

| Variable | Default | Purpose |
|---|---|---|
| `SERVICE_PATH` | `/app/service` | Service source root |
| `SERVICE_APP` | `server:app` | WSGI target as `module:attr` |
| `SERVICE_NAME` | empty | Name used for the doctypes path, advisory lock and Redis namespace |
| `PORT` | `8000` | Gunicorn bind port (read by the entrypoint, not by `MicroserviceApp`) |
| `GUNICORN_WORKERS` | `4` | Worker processes |
| `GUNICORN_TIMEOUT` | `120` | Worker timeout, seconds |

## Schema sync

At startup the entrypoint runs the DocType sync once, before Gunicorn forks
workers; in dev (`app.run()`) it syncs lazily on the first request.

| Variable | Values | Default | Purpose |
|---|---|---|---|
| `DOCTYPE_SYNC_MODE` | `migrate\|create` | `migrate` | Schema sync behaviour |
| `FIXTURE_SYNC_MODE` | `strict\|soft` | `strict` | Whether a failed fixture aborts startup |
| `DOCTYPES_PATH` | path | derived from `SERVICE_PATH` / `SERVICE_NAME` | DocType JSON directory |
| `SERVICE_POST_PRESYNC` | `module.attr` | unset | Callable run after sync, inside the advisory lock |

- **`migrate`** (default) — create-if-missing *and* update-if-changed. Editing a
  DocType JSON and restarting updates both `tabDocType` metadata and the SQL
  table. It is **additive only**: columns are never dropped.
- **`create`** — create-only, for operators who must not run DDL at startup.

`FIXTURE_SYNC_MODE=strict` is the default on purpose: a fixture is service-owned
schema, so a failed import means the service is running against a schema it does
not have. `soft` downgrades that to an error log and is intended for recovery.

Concurrent replicas are safe — the sync takes a database advisory lock, and a
replica that cannot acquire it within 120s skips rather than crashing. For an
explicit migration step, `frappe-ms migrate` runs the same sync and suits a
Kubernetes Job.

## Background jobs

| Variable | Values | Default | Purpose |
|---|---|---|---|
| `ENABLE_RQ` | `1/true/yes/on` | off | Enable RQ and start the embedded worker |
| `REDIS_URL` | URL | `redis://localhost:6379` | Redis for the queue — **separate from `REDIS_HOST`** |
| `WORKER_MODE` | exactly `1` | unset | Set up the queue but do not start the thread; run `frappe-ms worker` instead |
| `RQ_DEFAULT_JOB_TIMEOUT` | seconds | `-1` (none) | Default job timeout |

## Logging & observability

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Library log level; an unrecognised name falls back to `INFO` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset (tracing off) | OTLP collector endpoint |
| `FRAPPE_STREAM_LOGGING` | `1` | Stream Frappe logs to stderr instead of files |
| `DEBUG` | unset | Include exception detail in `500` bodies. Must be exactly `1` |

The library forces `FRAPPE_STREAM_LOGGING=1` at import. Frappe's file handlers
resolve paths relative to the process working directory — a bench-era assumption
that crashes with `FileNotFoundError: .../logs/database.log` under a container
`WORKDIR` or a read-only root filesystem. To opt back into file logs, set
`FRAPPE_STREAM_LOGGING=""` *before* importing the library.

Under `readOnlyRootFilesystem: true`, the **sites directory must stay writable** —
`site_config.json` is written there. Mount an `emptyDir` for it.

## Gotchas

A few options do not follow the general rules. These are accurate as of this
version:

- **`WORKER_MODE` and `DEBUG` require exactly the string `1`.** Unlike the
  boolean flags, `true` / `yes` / `on` silently do nothing.
- **`RQ_DEFAULT_JOB_TIMEOUT=""` and `CENTRAL_SITE_TIMEOUT=""` raise `ValueError`.**
  The database and Redis port helpers guard against empty values; these two do
  not. Leave them unset rather than empty.
- **`CENTRAL_SITE_TIMEOUT` has no effect** — it is read but never applied to the
  client.
- **`LOAD_FRAMEWORK_HOOKS` only affects the presync path**, not
  `MicroserviceApp`, and the two default differently (`[]` versus
  `["frappe", "erpnext"]`). Prefer the `load_framework_hooks` kwarg, which
  accepts a list of app names or one of `"full"`, `"frappe-only"`, `"none"`.

## Constructor-only options

These have no environment variable:

| Kwarg | Default | Purpose |
|---|---|---|
| `name` (positional) | required | Flask app name, RQ queue name, Redis namespace, OTel service name |
| `port` | `8000` | Dev-server port for `run()` |
| `tenant_field` | `tenant_id` | Tenant column name |
| `get_tenant_id_func` | `None` | Custom tenant resolver |
| `doctypes_path` | `None` | DocType JSON directory |
| `fixtures_path` | `$SERVICE_PATH/fixtures` if present | Fixture directory |
| `controllers_path` | `$SERVICE_PATH/controllers` if present | Controller discovery root |
| `load_framework_hooks` | `["frappe", "erpnext"]` | Framework apps whose hooks may load |

## Example

A starting point for a container deployment — see
[`.env.example`](https://github.com/vyogotech/erpnext-microservices-lib/blob/main/.env.example)
for the annotated version:

```bash
FRAPPE_SITE=dev.localhost
FRAPPE_SITES_PATH=/app/sites
DB_HOST=orders-db
DB_NAME=orders_db
REDIS_HOST=redis

CENTRAL_SITE_URL=http://central-site:8000
CENTRAL_SITE_API_KEY=...
CENTRAL_SITE_API_SECRET=...

MULTI_TENANT=1
TENANT_STRICT=warn
PERMISSION_MODE=warn
CSRF_MODE=warn
ALLOW_CORS=https://app.example.com

SERVICE_PATH=/app/service
SERVICE_APP=server:app
DOCTYPE_SYNC_MODE=migrate
```
