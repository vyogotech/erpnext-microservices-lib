# CI/CD & Release Strategy

Automated pipelines are managed via GitHub Actions to ensure high code quality and consistent releases.

## Pipeline Overview

The main workflow (`.github/workflows/ci.yml`) executes the following steps:
1.  **Test**: Runs Pytest (TDD) and Behave (BDD).
2.  **Report**: Generates and uploads Allure test reports.
3.  **Build & Publish**: Builds the OCI image using **Buildah** and publishes it to **GitHub Container Registry (GHCR)**.

> [!TIP]
> Images are automatically tagged based on the branch (e.g., `latest`, `develop`, `version-15`) and are stored at `ghcr.io/<owner>/frappe-microservice-lib`.

The framework follows a Frappe-style branching model for managing versions:

| Branch | Image Tag | Build Argument (`${FRAAPE_VERSION}`) | Purpose |
|--------|-----------|-------------------------------------|---------|
| `develop` | `develop` | `develop` | Active development |
| `version-15` | `version-15` | `version-15` | Stable V15 release |
| `main` | `latest` | `version-15` | Current stable release |
| Tags (`v*`) | `latest` | `version-15` | Production tags |

### Dynamic Build Arguments
The `Containerfile` uses `ARG` for framework versions. The CI pipeline automatically passes the branch name as the version to ensure the library built for a specific branch depends on the correct core framework version.

## Testing Locally with Act

You can run the entire pipeline on your local machine using `act`.

```bash
# Run the test job locally
act -j test --container-architecture linux/arm64
```
*(Requires `act` and a Docker-compatible engine like Podman)*
