# Developer Guide

## Local Development Environment

The project is fully pre-configured for VS Code Dev Containers.

### Features
- **Frappe & ERPNext v15** pre-installed.
- **Python 3.11** environment.
- **Allure CLI** for local report generation.
- **`act`** for local pipeline testing.

### Usage
1. Open the project in VS Code.
2. Click **"Reopen in Container"** when prompted.
3. The environment will automatically set up and install dependencies in editable mode.

## Running Tests

Tests are organized into TDD (Pytest) and BDD (Behave).

### TDD Unit Tests
```bash
PYTHONPATH=. pytest --alluredir=allure-results tests/
```

### BDD Behavioral Tests
```bash
PYTHONPATH=. behave -f allure_behave.formatter:AllureFormatter -o allure-results features/
```

### Viewing Reports
```bash
# Serves the visual report at http://localhost:PORT
allure serve allure-results
```

## Where to Make Changes

The `frappe_microservice` package is modular.
Use this guide to find the right file for your change:

| If you are changing... | Edit this file |
|---|---|
| Document lifecycle hooks (`DocumentHooks`) | `frappe_microservice/hooks.py` |
| Tenant resolution or `TenantAwareDB` | `frappe_microservice/tenant.py` |
| App/module isolation (patching `get_installed_apps`, module maps, hook filtering) | `frappe_microservice/isolation.py` |
| OAuth2 or SID session validation | `frappe_microservice/auth.py` |
| `register_resource()` auto-CRUD | `frappe_microservice/resources.py` |
| `MicroserviceApp.__init__`, middleware, logging, OTEL, `secure_route`, `run()` | `frappe_microservice/app.py` |
| DocType controller system | `frappe_microservice/controller.py` |
| CLI / site_config / container entrypoint (main, SERVICE_PATH, SERVICE_APP) | `frappe_microservice/entrypoint.py` |
| Public API re-exports | `frappe_microservice/__init__.py` (and `core.py` for compat) |

> **Important for test authors**: `unittest.mock.patch()` targets must point to the
> module where the symbol is *used at runtime*, not where it is re-exported.
> For example, patch `frappe_microservice.app.get_user_tenant_id` (where
> `MicroserviceApp` imports it), not `frappe_microservice.core.get_user_tenant_id`.

## Manual Verification with Podman Compose

You can verify the dev environment orchestration outside of VS Code:

```bash
# Start the environment
podman compose -f .devcontainer/docker-compose.yml up -d

# Check framework versions
podman exec devcontainer-devcontainer-1 pip list | grep -E "frappe|erpnext"

# Stop the environment
podman compose -f .devcontainer/docker-compose.yml down
```
