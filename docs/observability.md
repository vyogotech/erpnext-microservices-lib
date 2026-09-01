# Observability

## Tracing (OpenTelemetry)

Tracing is **off until you point it at a collector**:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://jaeger:4317"
```

With it unset the framework logs `tracing disabled` and adds no overhead. If the
OpenTelemetry packages are not installed, tracing is a no-op rather than an
error.

The OTel `service.name` comes from the app's name — `create_microservice("orders-service")`
reports as `orders-service`. There is no environment variable for it.

### What is instrumented

- **HTTP requests** — every incoming request, via Flask instrumentation.
- **Database operations** — `TenantAwareDB.get_all`, `get_doc` and `insert_doc`
  emit manual spans carrying `db.system`, `db.operation`, `db.target` (the
  DocType) and, where known, the document name.

Spans are exported through a `BatchSpanProcessor`, so a slow or unreachable
collector does not block request handling. It will, however, log export failures —
if you see repeated `Failed to export traces` warnings, the endpoint is wrong or
the collector is down.

The OpenTelemetry packages ship in the base install for 1.5.0 and are also
available as the `[otel]` extra. They move to extra-only in 2.0.

## Logging

`LOG_LEVEL` sets the level (default `INFO`); an unrecognised name silently falls
back to `INFO`.

```bash
export LOG_LEVEL=DEBUG
```

### Container-safe logging

Frappe's default handlers write to files whose paths resolve relative to the
process working directory — a bench-era assumption. Under a container `WORKDIR`,
or a read-only root filesystem, that crashes with
`FileNotFoundError: .../logs/database.log`.

The library therefore sets `FRAPPE_STREAM_LOGGING=1` at import: logs go to
stderr, which is what Kubernetes and Docker collect, and no `*.log` file is ever
opened. This is the 12-factor behaviour and the reason services boot cleanly on a
read-only filesystem.

To opt back into bench-style file logs, set `FRAPPE_STREAM_LOGGING=""` **before**
importing the library.

!!! warning "Read-only root filesystems"
    Even with stream logging, the **sites directory must stay writable** —
    `site_config.json` is written there at startup. Mount an `emptyDir` over it
    when running with `readOnlyRootFilesystem: true`.

## Health probes

Point your orchestrator at the right endpoint for the right question — `/health`
for liveness, `/health/ready` for readiness. Using the readiness endpoint as a
liveness probe turns a brief database outage into a restart loop. See
[Core Features](features.md#health-readiness-probes).
