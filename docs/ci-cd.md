# CI/CD & Release

The pipeline lives in `.github/workflows/ci.yml` and runs four jobs in a strict
chain. Each gates the next, so an image is only published if both the unit and
integration suites passed.

```
test ──► integration-test ──► build ──► verify-image
```

**Triggers**: pushes to `main`, `release`, `develop`, `version-*`; tags `v*`;
pull requests against those branches; and manual dispatch.

## The jobs

### `test`

Python **3.10**. Installs the package with `pip install -e ".[dev]"`, then:

```bash
flake8 frappe_microservice/
PYTHONPATH=. pytest -m "not integration" --alluredir=allure-results tests/
PYTHONPATH=. behave -f allure_behave.formatter:AllureFormatter -o allure-results features/
```

Allure results are uploaded as an artifact (1-day retention, non-blocking).

### `integration-test`

Brings up `docker-compose.integration.yml` — the public
`docker.io/vyogo/erpnext:sne-version-16` single-node image, with the repository
bind-mounted at `/mnt/lib` and `MULTI_TENANT=0`.

It waits up to 400s for the site to answer `SELECT 1`, bootstraps ERPNext test
data if no Company exists, installs the library into the bench virtualenv, and
runs `tests/integration/` with branch coverage.

**This job uses no registry credentials, deliberately.** `GITHUB_TOKEN` is
restricted on pull requests from forks, so requiring a login would make the
integration suite unrunnable for outside contributors. That is why the base image
is public on Docker Hub rather than private on GHCR.

### `build`

Builds with `docker/setup-buildx-action` + `docker/build-push-action`. It builds
twice:

1. **Verification build** (`push: false`, `load: true`) — then runs
   `scripts/check_pruned_deps.py` inside the image. That script walk-imports
   every Frappe and ERPNext module to prove the dependency pruning did not remove
   something a controller reaches transitively. It also reports image size.
2. **Publish build** (`push: true`) — largely served from the first build's
   buildx cache.

Platform is **`linux/amd64` only**. arm64 via QEMU cost roughly 15 minutes per
build; the intended path when it is needed is a native runner per architecture
pushing `:tag-amd64` / `:tag-arm64`, stitched with
`docker buildx imagetools create`.

### `verify-image`

Runs only on manual dispatch, or on a push to `main` or `release`. Pulls the
image that was just published — by its SHA tag, not a rebuild — and re-runs
`tests/integration/` *inside* it via
`scripts/run_service_image_integration.sh`. Expected: **12 passed, 9 skipped**.

The 9 skips are all of `test_tenant_id_stamping.py`; a service image does not
ship a tenancy provider app.

## Image tags

The image name is derived from the repository:
`ghcr.io/vyogotech/erpnext-microservices-lib`.

Tags come from `docker/metadata-action`:

| Event | Tags produced |
|---|---|
| push `main` | `latest`, `sha-<short>`, `sha-<full>` |
| push `develop` / `version-*` | branch name, `sha-<short>`, `sha-<full>` |
| push `release` | `sha-<short>`, `sha-<full>` only |
| push tag `v*` | the tag name (e.g. `v1.5.0`), `sha-<short>`, `sha-<full>` |
| pull request | `pr-<number>` — and it **is** pushed |

Two things worth internalising:

- **`latest` only ever means `main`.** A `v*` tag push does *not* move `latest`.
- **Every non-PR build gets both SHA tags.** These are the immutable references —
  use `sha-<full>` for reproducible deploys, never `latest`.

!!! note "Fork pull requests cannot push"
    The `build` job pushes on pull requests, but `GITHUB_TOKEN` is read-only for
    packages on fork PRs, so that step fails for outside contributors — the same
    constraint `integration-test` is designed around.

## Framework versions

The Containerfile takes `FRAPPE_VERSION` and `ERPNEXT_VERSION` build arguments,
pinned by default to immutable tags:

```
FRAPPE_VERSION=v16.28.0
ERPNEXT_VERSION=v16.29.0
```

Branches override them:

| Branch / event | FRAPPE_VERSION | ERPNEXT_VERSION |
|---|---|---|
| `develop`, or a PR based on it | `develop` | `develop` |
| `version-15*`, or a PR based on it | `version-15` | `version-15` |
| everything else (`main`, `release`, `version-16`, tags, dispatch) | `v16.28.0` | `v16.29.0` |

There is **no** `version-16` special case — that branch builds against the pinned
v16 tags, which is what "version-16" means in practice.

!!! warning "The library requires Frappe v16"
    `frappe_microservice.app` imports `frappe.utils.local`, which does not exist
    in Frappe v15. A `version-15` build fails at import. That override exists for
    the maintenance branch, not for `main`.

## Documentation

`.github/workflows/docs.yml` runs on pushes to `main`: installs
`mkdocs-material` and publishes to `gh-pages` with `mkdocs gh-deploy --force`.

## Running the pipeline locally

`act` runs jobs on your machine (requires Docker or Podman):

```bash
act -j test --container-architecture linux/arm64
```

`.actrc` maps `ubuntu-latest` to `catthehacker/ubuntu:act-latest`. The
`integration-test` job needs a container runtime and pulls a ~1.5 GB image, so it
is slow locally — the scripts in
[Development](development.md#integration-tests) are the better path.
