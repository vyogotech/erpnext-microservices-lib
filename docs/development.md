# Developer Guide

## Setup

```bash
git clone https://github.com/vyogotech/erpnext-microservices-lib.git
cd erpnext-microservices-lib
pip install -e ".[dev]"
make setup          # installs and registers the pre-commit hooks
```

`make setup` and `make lint` are the only Make targets.

### Python versions

There is no single version — each context pins its own, which matters when
reproducing a failure:

| Context | Python |
|---|---|
| CI `test` job | 3.10 |
| `Containerfile`, `Containerfile.dev` | 3.14 |
| `Containerfile.postgres` | 3.11 |
| `.devcontainer` | 3.11 |

The package requires **3.10+** (`setup.py`), because it uses PEP 604 unions
without `from __future__ import annotations`.

### Dev container

`.devcontainer/` is configured for VS Code — open the project and choose
"Reopen in Container".

!!! warning "The dev container is currently stale"
    `.devcontainer/Dockerfile` installs Frappe and ERPNext from **`version-15`**,
    but the library imports `frappe.utils.local`, which only exists in v16. Code
    touching `MicroserviceApp` will fail to import there. Its compose file also
    defines a single service — no MariaDB, no Redis — so nothing needing a live
    site runs in it.

    Use the integration scripts below for anything requiring a real site.

## Tests

Three suites, run differently.

### Unit tests

```bash
pytest -m "not integration" tests/
```

The `-m "not integration"` filter matters — without it pytest collects the
integration suite, which needs a live Frappe site. `pytest.ini` applies branch
coverage with an **80% `--cov-fail-under` gate** and writes HTML to `htmlcov/`.

`./run_tests.sh [unit|integration|all]` wraps the same commands.

### BDD tests

```bash
behave features/
```

### Integration tests

These run *inside* a container with a real Frappe site, MariaDB and Redis. Two
harnesses, answering two different questions:

```bash
# Does the library work against a real site?
./scripts/run_integration_tests.sh

# Does the SHIPPED SERVICE IMAGE work? (pruned dependencies and all)
SERVICE_IMAGE=localhost/frappe-microservice-lib:latest \
  ./scripts/run_service_image_integration.sh
```

Both start the public `docker.io/vyogo/erpnext:sne-version-16` image with
`MULTI_TENANT=0`, bind-mount the repo at `/mnt/lib`, and tear down on exit.
`KEEP_RUNNING=1` leaves the containers up for debugging; override the image with
`CENTRAL_SITE_IMAGE`.

!!! danger "One suite per stack"
    Do not run both scripts against the same live containers. Both mutate the
    shared site database and each breaks the other — verified in both directions.
    `KEEP_RUNNING=1` defeats the clean-start guarantee, so start fresh when
    switching.

The integration suite has **no coverage gate** — `tests/integration/pytest.ini`
sets only the marker and a 120s timeout.

## Linting

**Local and CI lint with different tools.** CI runs `flake8 frappe_microservice/`
against `setup.cfg` (max line length 120, with cosmetic codes ignored so the
baseline still catches real pyflakes errors). Pre-commit runs a different stack:

```bash
make lint          # pre-commit run --all-files
```

| Hook | What it does |
|---|---|
| `ruff` + `ruff-format` | Lint and format |
| `lizard` | Complexity ≤ 15 per function, ≤ 450 lines per file |
| `semgrep` | Static security analysis (`--config=auto`) |
| `gitleaks` | Secret scanning |
| whitespace hooks | Trailing whitespace, EOF newline, large files |

Two things to know before trusting a green or red run:

- **`ruff` runs without `--fix`, deliberately.** Autofix once removed a live
  import and produced a `NameError`; the hook reports rather than rewrites.
- **Some hooks fail on pre-existing code.** Establish the baseline on an
  untouched checkout before assuming a failure is yours.

Because CI enforces only flake8, run that directly before pushing:

```bash
flake8 frappe_microservice/
```

## Where to make changes

| If you are changing… | Edit |
|---|---|
| `MicroserviceApp`, middleware, `secure_route`, logging, OTEL | `app.py` |
| OAuth2 / SID / internal-token validation | `auth.py` |
| Embedded RQ worker, `enqueue_task` | `background.py` |
| Redis key namespacing for a shared cache | `cache.py` |
| `CentralSiteClient` (`app.central`) | `central.py` |
| The `frappe-ms` CLI | `cli.py` |
| DocType controller system | `controller.py` |
| Container startup order, Gunicorn exec | `entrypoint.py` |
| `DocumentHooks` lifecycle system | `hooks.py` |
| App/module isolation, hook filtering | `isolation.py` |
| Tenant-scoped docnames, `resolve_or_create` | `naming.py` |
| `off`/`warn`/`enforce` and boolean flag parsing | `policy.py` |
| Link-value label presentation | `presentation.py` |
| Startup DocType schema and fixture sync | `presync.py` |
| `register_resource()` auto-CRUD | `resources.py` |
| `site_config.json` generation | `site_config.py` |
| `TenantAwareDB`, tenant resolution and stamping | `tenant.py` |
| Public API re-exports | `__init__.py` (and `core.py` for compatibility) |

!!! note "Patch targets in tests"
    `unittest.mock.patch()` must target the module where a symbol is *used at
    runtime*, not where it is re-exported. Patch
    `frappe_microservice.app.get_user_tenant_id` — where `MicroserviceApp`
    imports it — not `frappe_microservice.core.get_user_tenant_id`.
    `tests/test_patch_target_guard.py` enforces this.

## Conventions

- **Do not break public API signatures.** Add keyword arguments with defaults;
  never reorder or remove parameters.
- **New enforcement ships as `warn` first.** Add the flag, default it to
  log-only, document the 2.0 default, and give operators a release to discover
  what would break. See [Security](security.md).
- **Keep the coverage gate.** 80% with branch coverage; do not lower it to make a
  change fit.
- **Update the CHANGELOG** under `[Unreleased]`, and the docs alongside the code.

## Reports

Allure is wired into both suites:

```bash
pytest -m "not integration" --alluredir=allure-results tests/
behave -f allure_behave.formatter:AllureFormatter -o allure-results features/
allure serve allure-results
```
