# Tenancy

The framework runs in one of two modes, selected by `MULTI_TENANT`. Both are
first-class; neither is a degraded version of the other.

| | `MULTI_TENANT=1` (default) | `MULTI_TENANT=0` |
|---|---|---|
| Site | Needs a Central Site tenancy provider | Stock Frappe/ERPNext |
| Reads | Scoped by `tenant_id` | Unscoped |
| Writes | Stamped with `tenant_id` | Not stamped |
| `get_doc` | Verifies tenant ownership | No tenant check |
| Master docnames | Tenant-scoped (`<tenant8>-<slug>`) | Frappe autonaming |
| Startup DDL | Adds a `tenant_id` column per DocType | None |
| Role permissions | Enforced | Enforced — unchanged |

!!! warning "An empty value is not off"
    `MULTI_TENANT=""` resolves to the **default (on)**, not to off. A declared
    but valueless environment variable — a Kubernetes `env:` entry with no
    value, or a missing ConfigMap key — must not silently drop tenant
    isolation. Disabling requires an explicit `MULTI_TENANT=0`.

## Multi-tenant mode

A single site holds every tenant's rows, separated by a `tenant_id` column.
`app.tenant_db` applies that separation automatically, so handler code does not
carry tenant logic:

```python
# scoped to the caller's tenant — no filter written by hand
orders = app.tenant_db.get_all("Sales Order", filters={"status": "Draft"})

# stamped with the caller's tenant
app.tenant_db.insert_doc("Sales Order", {"customer": "CUST-001"})

# raises TenantAccessError if the document belongs to another tenant
order = app.tenant_db.get_doc("Sales Order", "SO-0001")
```

### The Central Site contract

Multi-tenant mode expects the Central Site to run an app providing tenancy. The
framework depends on no particular implementation — only on this contract:

1. **Tenant identity** — a `tenant_id` field on the `User` DocType.
2. **Column presence** — a `tenant_id` column on the tables services read and
   write. Frappe keeps `tenant_id` outside DocType metadata, so a provider
   normally adds it with an `ALTER TABLE` patch across every non-single DocType.
3. **Session response** — session validation returns the user's `tenant_id`, so a
   service can resolve the tenant from a session alone.

Any app meeting that contract works. Without one, multi-tenant mode cannot
function: the service cannot resolve a tenant, and tenant-scoped writes fail
because the column is absent.

### Why writes need the column on every table

The stamping patch assumes every non-single table carries `tenant_id`. Measured
on an ERPNext site with a provider's patch applied, 753 of 756 tables do. The
three that do not — `tabSeries`, `tabSessions`, `tabSingles` — are framework
stores written by raw SQL or `update_single`, never through a DocType insert.

That is why a site *without* the patch fails loudly rather than degrading: the
first child-row insert raises `OperationalError 1054` instead of silently
writing rows under a default tenant that no tenant can read back.

### Tenant-scoped docnames

Field-autonamed master doctypes (Item, Customer, Supplier, …) would collide when
two tenants create "Acme Corp". The framework prefixes their docnames with the
first 8 characters of the tenant id, matching what a Central Site tenancy hook
produces so both converge on the same primary key.

Two tenant ids sharing their first 8 hex characters collapse to the same prefix.
That is cosmetic, not an isolation hole — collisions get a numeric suffix and
lookups stay tenant-filtered.

## Single-tenant mode

Set `MULTI_TENANT=0` and the framework resolves, filters on, stamps and verifies
no `tenant_id` at all. It runs against a stock Frappe/ERPNext site with no
tenancy provider and no `tenant_id` column anywhere.

Use it when:

- one deployment serves one organisation;
- you want to adopt the framework before committing to a tenancy provider;
- you are running the integration suite (which is exactly what CI does).

What changes internally, beyond the query paths:

- `secure_route` skips the per-request tenant lookup. On a stock site that lookup
  would query a `tenant_id` column on `tabUser` that does not exist — two failing
  queries and three error log lines on **every** authenticated request.
- `presync` does not add `tenant_id` columns. This one matters: re-creating the
  column would quietly reintroduce the dependency this mode exists to remove.
- Master docnames fall back to Frappe autonaming.
- Link presentation is inert, because nothing was renamed to present.

### What does *not* change

**Role permissions still apply.** `PERMISSION_MODE` is orthogonal to tenancy — a
single-tenant site still has Roles, DocPerms, and users who should not read
Purchase Invoices. Single-tenant mode is not "no checks".

`TENANT_STRICT` is deliberately ignored while the mode is off. Its purpose is to
flag tables *missing* a `tenant_id` column, which on a stock site is every table:
the default `warn` would log on every read, and `enforce` would make the mode
impossible.

## Choosing at runtime

Both forms are equivalent; the environment variable is the usual choice for
containers:

```python
app = create_microservice("my-service", multi_tenant=False)
```

```bash
MULTI_TENANT=0
```

The keyword argument wins over the environment variable, which wins over the
default. See [Configuration](configuration.md) for the resolution rules.
