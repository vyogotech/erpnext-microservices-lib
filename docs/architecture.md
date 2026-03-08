# Architecture & Isolation

## Package Structure

The `frappe_microservice` package is split into focused modules.
`core.py` is kept as a thin re-export layer so existing imports remain valid.

```
frappe_microservice/
├── __init__.py         # Public API surface
├── core.py             # Re-export shim (backward compat)
├── app.py              # MicroserviceApp class + create_microservice()
├── hooks.py            # DocumentHooks lifecycle system
├── tenant.py           # NullContext, get_user_tenant_id, TenantAwareDB
├── isolation.py        # IsolationMixin – app/module/hooks filtering
├── auth.py             # AuthMixin – OAuth2 & SID session validation
├── resources.py        # ResourceMixin – auto CRUD endpoint generation
├── controller.py       # DocumentController + registry
└── entrypoint.py       # create_site_config, run_app, main() (container entrypoint)
```

### Module Dependency Diagram

```mermaid
graph TD
    app[app.py<br/>MicroserviceApp] --> isolation[isolation.py<br/>IsolationMixin]
    app --> auth[auth.py<br/>AuthMixin]
    app --> resources[resources.py<br/>ResourceMixin]
    app --> tenant[tenant.py<br/>TenantAwareDB]
    tenant --> hooks[hooks.py<br/>DocumentHooks]
    core[core.py<br/>re-export shim] -.-> app
    core -.-> tenant
    core -.-> hooks
    init[__init__.py] -.-> core
```

### Module Responsibilities

| Module | Owns |
|---|---|
| `hooks.py` | `DocumentHooks` – register/run lifecycle hooks per DocType |
| `tenant.py` | `TenantAwareDB`, `get_user_tenant_id`, `NullContext` |
| `isolation.py` | `IsolationMixin` – patches `frappe.get_installed_apps`, module maps, hooks resolution |
| `auth.py` | `AuthMixin` – `_validate_oauth_token`, `_validate_session` |
| `resources.py` | `ResourceMixin` – `register_resource()` auto-CRUD |
| `app.py` | `MicroserviceApp` (composed from the three mixins) and `create_microservice()` |
| `core.py` | Re-exports everything for backward compatibility |
| `entrypoint.py` | `create_site_config`, `run_app`, `main()` (container entrypoint: SERVICE_PATH, SERVICE_APP) |

## Bounded Context

In a true microservices architecture, each service must own its data. The Frappe Microservice Framework promotes this by encouraging an **Independent Database** for each service.

- **Central Site**: Acts as the "Source of Truth" for authentication and global user state.
- **Service Database**: Contains only the DocTypes relevant to the service's bounded context.

## External Dependencies

### Central Site & `saas_platform` App

The framework relies on a **Central Site** running a specific Frappe app called [saas_platform](file:///Users/varkrish/personal/saas_platform).

- **Tenant Identification**: The `saas_platform` app is responsible for adding the `tenant_id` field to the `User` DocType on the Central Site.
- **Session Validation**: When a microservice validates a session, it expects the Central Site to return a user profile that includes this `tenant_id`.
- **Data Isolation**: Without the `saas_platform` app on the Central Site, the microservice will not be able to resolve the user's tenant, effectively disabling multi-tenant data isolation.

## Strategy: Granular Decomposition of ERPNext

One of the primary goals of this framework is to enable the **gradual decomposition of monolithic ERPNext** into smaller, manageable microservices.

Instead of a high-risk "big bang" migration, teams can:
1.  **Identify a Module**: Choose a specific module (e.g., "Orders", "Signups", "Inventory").
2.  **Define a Bounded Context**: Extract relevant DocTypes to a new service-specific database.
3.  **Implement via Framework**: Use the Frappe Microservice Framework to expose APIs and business logic.
4.  **Secure Integration**: Leverage the Central Site for unified authentication across all microservices.

This approach allows for independent scaling, faster deployment cycles, and technology diversification while maintaining the power of Frappe's DocType system.

## Multi-Tenancy

The framework is built for SaaS applications. It assumes a "Single Site, Multi-Tenant" model where data is isolated using a `tenant_id` field.

### TenantAwareDB

The `app.tenant_db` object is a wrapper around Frappe's database methods. It automatically ensures that:
1.  **Filtering**: All `get_all` calls include a `tenant_id` filter in the `WHERE` clause.
2.  **Validation**: All `get_doc` calls verify that the requested document belongs to the current user's tenant.
3.  **Inheritance**: All `insert_doc` calls automatically set the `tenant_id` from the current request context.

### Authentication & Identification

1.  A user logs in to the **Central Site**.
2.  The microservice receives the request with the `sid` cookie.
3.  `MicroserviceApp` validates the session against the Central Site.
4.  The user's `tenant_id` is resolved and stored in the request context (`flask.g`).
5.  All database operations are now scoped to that `tenant_id`.

## Database Diagram (Example)

```mermaid
graph LR
    User[End User] --> MS[Microservice]
    MS --> Auth[Central Site Auth]
    MS --> DB[(Service DB)]
    DB -- "tenant_id filtering" --> Data[Tenant Data]
```
