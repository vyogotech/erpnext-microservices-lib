# signup-service (example)

A tiny microservice built with `frappe-microservice` showing the three common
building blocks:

- `@app.secure_route(...)` — an authenticated endpoint with the user injected.
- automatic tenant resolution (`app.tenant_db.get_tenant_id()` — no manual
  `set_tenant_id` needed).
- `app.register_resource("Signup Request")` — zero-code tenant-scoped CRUD.

## Run locally

```bash
pip install frappe-microservice
python server.py   # Flask dev server on 0.0.0.0:8000
```

Authenticated endpoints validate the caller against the Central Site
(`CENTRAL_SITE_URL`). See the top-level README "Security configuration" for the
relevant environment variables.

## Container

Build on the base image; the library entrypoint discovers this app via
`SERVICE_PATH` / `SERVICE_APP` (see `Containerfile`).
