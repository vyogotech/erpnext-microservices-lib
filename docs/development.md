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
