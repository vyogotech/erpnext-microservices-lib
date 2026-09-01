# Observability

The framework includes built-in support for distributed tracing and structured logging.

## OpenTelemetry (OTEL)

Instrumentation is provided for Flask and database operations.

### Configuration
Set the following environment variable to point to your OTLP collector:
```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://jaeger-host:4317"
```

### Automatic Spans
- **Web Requests**: All incoming HTTP requests are automatically instrumented.
- **Database Operations**: `TenantAwareDB` methods (`get_all`, `get_doc`, `insert_doc`) create manual spans with metadata (DocType, tenant_id).

## Logging

Logging is pre-configured and respects the `LOG_LEVEL` environment variable.

```bash
export LOG_LEVEL=DEBUG
```

The application logger is configured to output to standard output in a format suitable for container logs.
