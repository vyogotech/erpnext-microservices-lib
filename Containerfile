# ============================================
# STAGE 1: Builder (compile dependencies)
# ============================================
FROM python:3.11-slim AS builder

# Install ONLY build tools (will be discarded in final image)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    pkg-config \
    gcc \
    default-libmysqlclient-dev && \
    rm -rf /var/lib/apt/lists/*

# Create virtual environment for cleaner installation
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ARG for flexible versioning
ARG FRAPPE_VERSION=version-15
ARG ERPNEXT_VERSION=version-15

# Install frappe and erpnext framework from git
RUN pip install --no-cache-dir git+https://github.com/frappe/frappe.git@${FRAPPE_VERSION} && \
    pip install --no-cache-dir git+https://github.com/frappe/erpnext.git@${ERPNEXT_VERSION} && \
    #remove all client side files for erpnext and frappe to reduce image size - remove  public folder and .js, .css, .map files
    find /opt/venv/lib/python3.11/site-packages/frappe/public -type f \( -name '*.js' -o -name '*.css' -o -name '*.map' \) -delete && \
    find /opt/venv/lib/python3.11/site-packages/erpnext/public -type f \( -name '*.js' -o -name '*.css' -o -name '*.map' \) -delete && \
    rm -rf /opt/venv/lib/python3.11/site-packages/frappe/public/build && \
    rm -rf /opt/venv/lib/python3.11/site-packages/erpnext/public/build
# Install the microservice library
COPY . /tmp/frappe-microservice-lib
RUN pip install --no-cache-dir /tmp/frappe-microservice-lib && \
    pip install --no-cache-dir \
    pyjwt==2.8.0 \
    requests==2.32.0 \
    redis==4.5.5 \
    pymysql==1.1.1 && \
    rm -rf /tmp/frappe-microservice-lib && \
    find /opt/venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true && \
    find /opt/venv -type f -name '*.pyc' -delete && \
    find /opt/venv -type f -name '*.pyo' -delete

# Create site structure and link ERPNext as Frappe app
RUN mkdir -p /logs && \
    mkdir -p /app/sites/dev.localhost/logs && \
    mkdir -p /app/sites/dev.localhost/public/files && \
    mkdir -p /app/sites/dev.localhost/private/files && \
    mkdir -p /app/sites/dev.localhost/locks && \
    mkdir -p /app/dev.localhost/logs && \
    echo "frappe" > /app/sites/apps.txt && \
    echo "erpnext" >> /app/sites/apps.txt && \
    # Link pip-installed ERPNext as Frappe app (no redundant cloning!)
    mkdir -p /app/sites/apps && \
    ln -s /opt/venv/lib/python3.11/site-packages/erpnext /app/sites/apps/erpnext && \
    ln -s /opt/venv/lib/python3.11/site-packages/frappe /app/sites/apps/frappe && \
    # Create minimal saas_platform stub (required by ERPNext hooks)
    mkdir -p /opt/venv/lib/python3.11/site-packages/saas_platform && \
    echo "# Minimal stub for ERPNext compatibility" > /opt/venv/lib/python3.11/site-packages/saas_platform/__init__.py

# Create utils.py stub
RUN printf 'class Utils:\n    @staticmethod\n    def set_tenant_id(*args, **kwargs):\n        """Stub function for ERPNext compatibility"""\n        pass\n\n    @staticmethod\n    def get_tenant_id(*args, **kwargs):\n        """Stub function for ERPNext compatibility"""\n        return None\n\n# Also provide module-level functions for backward compatibility\ndef set_tenant_id(*args, **kwargs):\n    """Stub function for ERPNext compatibility"""\n    pass\n\ndef get_tenant_id(*args, **kwargs):\n    """Stub function for ERPNext compatibility"""\n    return None\n' > /opt/venv/lib/python3.11/site-packages/saas_platform/utils.py

# ============================================
# STAGE 2: Runtime (Debian slim, stable wheels)
# ============================================
FROM python:3.11-slim

# Install ONLY runtime dependencies (no build tools)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libmariadb3 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Python packages from builder (instead of rebuilding)
COPY --from=builder /opt/venv /opt/venv

# Copy pre-created site structure from builder
COPY --from=builder /app/sites /app/sites
COPY --from=builder /app/dev.localhost /app/dev.localhost
COPY --from=builder /logs /logs

# Set environment variables
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FRAPPE_SITES_PATH=/app/sites \
    FRAPPE_SITE=dev.localhost \
    PYTHONPATH="/app/sites/apps:$PYTHONPATH"

EXPOSE 8000

# Default entrypoint for microservices (can be overridden)
ENTRYPOINT ["/opt/venv/bin/python", "/app/entrypoint.py"]




