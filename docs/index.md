# Frappe Microservice Framework

A Python library for building **secure, multi-tenant** Frappe microservices with minimal boilerplate and a clear separation of concerns.

## Core Principles

- **Secure by Default**: All endpoints require authentication unless explicitly public.
- **Multi-Tenant Isolation**: Data is automatically segregated by tenant, preventing data leaks.
- **Gradual Decomposition**: Designed for the incremental migration of ERPNext modules into microservices.
- **Bounded Context**: Each microservice manages its own database schema and logic, promoting true service independence.
- **Frappe-Compatible**: Leverages Frappe's ORM and conventions while enabling modern development patterns.

## Features at a Glance

*   🔒 **Secure by Default**: All endpoints require authentication via Central Site.
*   🔌 **Independent Database**: Each microservice has its own database (bounded context principle).
*   👤 **User Context Injection**: Authenticated user is automatically injected into handlers.
*   🚀 **Zero Boilerplate**: Create CRUD APIs with a single line of code.
*   🏢 **Multi-Tenant Ready**: Built-in support for tenant isolation.
*   🛡️ **Tenant-Aware Database**: Automatic `tenant_id` filtering prevents cross-tenant data access.
*   🪝 **Document Hooks**: Frappe-style lifecycle hooks without modifying Frappe core.
*   📦 **Frappe-Native**: Seamlessly works with Frappe DocTypes and APIs.
*   📊 **Observability**: Built-in OpenTelemetry tracing and configurable logging.
*   🏗️ **Professional CI/CD**: Automated testing, reporting (Allure), and branch-based Docker tagging.

## Navigation

- [Getting Started](getting-started.md)
- [Architecture & Isolation](architecture.md)
- [Core Features](features.md)
- [Observability](observability.md)
- [CI/CD & Release Strategy](ci-cd.md)
- [Developer Guide](development.md)
