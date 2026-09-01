# Security

## Policy modes

Several protections ship in **warn-only** mode: they log what *would* be blocked
but block nothing. This lets you deploy the upgrade, read the logs, fix what
surfaces, and then enforce — instead of discovering breakage in production.

They flip to `enforce` by default in 2.0. **Set them explicitly in production
rather than relying on either default.**

| Env var | Kwarg | Values | Current default | 2.0 default | Purpose |
|---|---|---|---|---|---|
| `PERMISSION_MODE` | `permission_mode` | `off\|warn\|enforce` | `warn` | `enforce` | Role-permission checks on `tenant_db` reads and `set_value` |
| `CSRF_MODE` | `csrf_mode` | `off\|warn\|enforce` | `warn` | `enforce` | CSRF signal required for cookie-authenticated mutations |
| `TENANT_STRICT` | `tenant_strict` | `off\|warn\|enforce` | `warn` | `enforce` | Fail closed when a table lacks a `tenant_id` column |
| `MASK_PERMISSION_ERRORS` | `mask_permission_errors` | `1/true/…` | off | on | Mask cross-tenant probes as `404` (anti-enumeration) |

An unrecognised value logs a warning and falls back to the default rather than
failing to boot.

`TENANT_STRICT` has no effect while `MULTI_TENANT=0` — see [Tenancy](tenancy.md).

### Finding what enforcement would break

In `warn` mode every would-be denial is logged with a `[would-deny]` prefix:

```
[would-deny] permission read on Sales Invoice for user alice@example.com (mode=warn)
```

Grep for `[would-deny]` in a representative window of traffic. An empty result
means `enforce` is safe to turn on.

## Authentication

Three mechanisms, all validated against the Central Site.

### OAuth2 Bearer tokens

For API clients and service-to-service calls:

```bash
curl -X POST http://central-site:8000/oauth/token \
  -d grant_type=client_credentials \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_SECRET

curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  http://localhost:8000/api/resource/sales-order
```

### Session cookies (SID)

For browser clients using a normal Frappe login:

```javascript
await fetch("http://central-site:8000/api/method/login", {
  method: "POST",
  credentials: "include",
  body: JSON.stringify({ usr: "user@example.com", pwd: "password" }),
});

// the sid cookie is sent automatically
await fetch("http://localhost:8000/api/resource/sales-order", {
  credentials: "include",
});
```

### Internal service token

For trusted service-to-service traffic that should not carry a user session, set
`INTERNAL_SERVICE_TOKEN` and send it as `X-Internal-Token`. A valid token is
granted the identity in `INTERNAL_SERVICE_USER` (default `Administrator`).

!!! warning "Prefer a scoped service user"
    `INTERNAL_SERVICE_USER` is **not** validated against the database at
    authentication time — the connection may not be up yet — so a nonexistent
    user fails later, at the first permission check. Use a dedicated service
    user with only the roles it needs rather than `Administrator`, which is
    platform-level and cannot own tenant data.

### Which methods are CSRF-exempt

`CSRF_MODE` only affects the **`sid` cookie** path, because browsers attach
cookies automatically. These are immune and never checked:

- `Authorization: Bearer`
- `X-Internal-Token`
- the `X-Frappe-SID` header
- `GET`, `HEAD`, `OPTIONS`

The accepted signal is an `X-Requested-With: XMLHttpRequest` header. A full
Frappe CSRF-token round-trip is a planned follow-up.

## SQL safety

### Prefer the TenantAwareDB helpers

```python
# Good — tenant filtering and permission checks are applied for you
orders = app.tenant_db.get_all("Sales Order", filters={"status": "Draft"})

# Bad — raw SQL bypasses both; you own the tenant filter
orders = frappe.db.sql("SELECT * FROM `tabSales Order` WHERE status='Draft'")
```

Raw SQL bypasses role-permission checks by design. Under
`PERMISSION_MODE=enforce` each such call is logged so audits can find them.

### Always parameterise

When raw SQL is unavoidable, pass parameters — never interpolate:

```python
# Good
tenant_id = app.tenant_db.get_tenant_id()
orders = app.tenant_db.sql(
    "SELECT * FROM `tabSales Order` WHERE tenant_id = %s AND status = %s",
    (tenant_id, "Draft"),
    as_dict=True,
)

# NEVER — f-strings and % formatting are injection vectors
orders = app.tenant_db.sql(
    f"SELECT * FROM `tabSales Order` WHERE tenant_id = '{tenant_id}'"
)
```

This applies to the tenant id as much as to user input.

## CORS

`ALLOW_CORS` is written into `site_config.json` as `allow_cors`. Left unset it
defaults to `*` **and logs a warning** — acceptable for local development, not
for production. Set an explicit origin:

```bash
ALLOW_CORS=https://app.example.com
```

## Error responses

Framework errors use one JSON shape:

```json
{
  "status": "error",
  "message": "Human readable message",
  "type": "ExceptionClass",
  "code": 403
}
```

Frappe exceptions map to HTTP status codes: `PermissionError` → 403,
`DoesNotExistError` → 404, `ValidationError` → 400, authentication failures → 401.

Exception strings and tracebacks appear in `500` bodies **only** when `DEBUG=1`
(or Flask debug is on). Otherwise `error` and `details` are `null`, so internal
detail is not leaked to callers.

## Secret scanning

`gitleaks` runs as a pre-commit hook alongside `semgrep` for static analysis. See
[Development](development.md) for the hook setup.
