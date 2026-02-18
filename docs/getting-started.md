# Getting Started

## Prerequisites

- **Central Site**: A running Frappe site that acts as the authentication provider.
- **`saas_platform` App**: The Central Site MUST have the `saas_platform` app installed, as it provides the `tenant_id` field for users.

## Installation

### From Source
```bash
git clone https://github.com/varkrish/frappe-microservice-lib.git
cd frappe-microservice-lib
pip install -e .
```

### Production Deployment (Docker)
The framework is designed to be built into a Docker image using the provided `Containerfile`. See [CI/CD & Release Strategy](ci-cd.md) for automated tagging details.

## Quick Start

### Basic Microservice
```python
from frappe_microservice import create_microservice

# Initialize microservice
app = create_microservice("my-service")

# Create a secure endpoint (authentication required via Central Site)
@app.secure_route('/hello', methods=['GET'])
def hello(user):
    """User is automatically injected after authentication"""
    return {"message": f"Hello {user}!"}

# Start the service
if __name__ == '__main__':
    app.run()
```

### Automatic CRUD with Resource API
The most powerful feature is the ability to expose any Frappe DocType as a REST resource with a single line:

```python
from frappe_microservice import create_microservice

app = create_microservice("orders-service")

# Register a DocType for automatic CRUD
app.register_resource("Sales Order")

# This automatically creates secure, tenant-isolated endpoints:
# GET    /api/resource/sales-order        - List
# POST   /api/resource/sales-order        - Create
# GET    /api/resource/sales-order/{name} - Get
# PUT    /api/resource/sales-order/{name} - Update
# DELETE /api/resource/sales-order/{name} - Delete

app.run()
```

## Configuration

The framework uses environment variables for easy containerization:

| Variable | Description | Default |
|----------|-------------|---------|
| `FRAPPE_SITE` | The Frappe site name | |
| `FRAPPE_SITES_PATH` | Path to the bench sites directory | |
| `CENTRAL_SITE_URL` | URL of the Central Site for Auth | |
| `DB_HOST` | Database host for the service | |
| `LOG_LEVEL` | Logging level (DEBUG, INFO, etc.) | `INFO` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP Exporter URL | |
