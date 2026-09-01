# Contributing

Thanks for contributing to `frappe-microservice`!

## Dev setup

```bash
pip install -e ".[dev]"
```

Python 3.10+ is required (the library uses PEP 604 `X | None` syntax).

## Running tests

```bash
# Unit tests (frappe is mocked — no external services needed)
PYTHONPATH=. pytest -m "not integration" tests/

# BDD tests
PYTHONPATH=. behave features/

# Lint (must pass in CI)
flake8 frappe_microservice/

# Integration tests (require Docker/Podman + the ERPNext container)
./scripts/run_integration_tests.sh
```

The coverage gate is **≥80%** (`pytest.ini`). Do not lower it.

## Conventions

- **No public API signature changes.** Constructor kwargs may be *added* with
  defaults; nothing is removed or reordered.
- New security enforcement ships in **warn-only** mode (`off|warn|enforce`,
  default `warn`) and flips to `enforce` in the next major. Use
  `frappe_microservice.policy.resolve_mode` for the tri-state.
- If you patch or reference new Frappe internals, add a guard test in
  `tests/test_patch_target_guard.py`.
- Every new env var / kwarg must be documented in the README
  "Security configuration" table and get a `CHANGELOG.md` entry.

## Pull requests

- Keep changes focused; one workstream per PR where possible.
- Include tests for new behavior and make sure `flake8` + the unit suite pass.
- Update `CHANGELOG.md` (and the "defaults changing in 2.0" list for flagged
  behavior).
