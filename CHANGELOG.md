# Changelog

## [1.1.0] - 2026-03-09

### Added
- **Central Site client**: `CentralSiteClient` in `frappe_microservice.central` for calling the central site API (FrappeClient-style). Configured via `CENTRAL_SITE_URL`, `CENTRAL_SITE_API_KEY`, `CENTRAL_SITE_API_SECRET`, or `CENTRAL_SITE_USER`/`CENTRAL_SITE_PASSWORD`. Access via `app.central` on `MicroserviceApp`.
- **Service DocTypes sync**: Support for syncing DocType definitions into the microservice DB (see `tenant.py` and `isolation.py`). Tests in `test_service_doctypes.py`.
- Tests for central client (`test_central.py`).

### Changed
- `app.py`: Refinements to app setup and exposure of `app.central` when central client is configured.
- `isolation.py`: Extended for service DocType handling and tenant isolation behavior.
- `tenant.py`: Updates for tenant-aware DB and service DocType flows.
- `conftest.py`: Mock setup adjustments for new modules.
- Swagger real-integration test now skips when `flasgger` is not installed (`pytest.importorskip`), so the test suite passes in environments without optional deps.

### Fixed
- Test suite: unit tests (excluding `integration` marker) pass consistently; integration tests remain opt-in when services are running.

---

## [0.2.0] - 2026-03-08

### Added
- New modular components extracted from core.py:
  - app.py: MicroserviceApp class
  - auth.py: authentication and JWT handling
  - hooks.py: Frappe hook integration
  - isolation.py: tenant isolation logic
  - resources.py: REST resource handlers
  - tenant.py: tenant-aware database operations
- End-to-end simulation tests (test_e2e_simulation.py)
- Patch target guard tests (test_patch_target_guard.py)
- Expanded documentation (architecture.md, development.md, getting-started.md)

### Changed
- core.py: refactored from monolith to thin re-export module
- entrypoint.py: updated imports for new modular structure
- Containerfile: streamlined build steps
- Containerfile.postgres: updated base image and build steps
- README.md: expanded with architecture overview and module descriptions
- All test files updated to work with new module structure

### Fixed
- **Module loading / RecursionError**: Guard monkey-patching of `frappe.get_installed_apps` and `frappe.get_all_apps` with an idempotency flag (`_microservice_isolation_applied`) so that repeated calls to `_patch_app_resolution()` do not double-wrap and cause RecursionError. Ensures each microservice only loads apps from its bounded context (apps.txt + load_framework_hooks) and does not see central-site-only apps from the shared DB.

### Improved
- Test coverage increased to meet 80% CI threshold
