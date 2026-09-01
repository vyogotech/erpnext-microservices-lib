# ============================================
# STAGE 1: Builder (compile dependencies)
# ============================================
# Builder stage uses Python 3.14 for Frappe v16 compatibility
FROM python:3.14-slim AS builder

# Install ONLY build tools (will be discarded in final image)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    pkg-config \
    gcc \
    default-libmysqlclient-dev

# Create virtual environment for cleaner installation
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Frozen version pins — explicitly aligned with central-site (frappe/erpnext:v16.29.0)
ARG FRAPPE_VERSION=v16.28.0
ARG ERPNEXT_VERSION=v16.29.0

# Install frappe and erpnext WITHOUT their dependency closure, then install only
# the dependencies a headless service actually needs.
#
# Frappe declares ~75 dependencies in pyproject.toml (it ships no requirements.txt
# to override), sized for a full Desk install -- PDF printing, Excel import, 2FA,
# full-text search, an interactive console, Google integrations. Installing all of
# them and uninstalling the unused ones afterwards wastes the download and makes
# the build log claim we ship packages we do not.
#
# resolve_frappe_deps.py reads what frappe and erpnext *declare*, from their
# installed metadata, so a version bump picks up new dependencies automatically.
# It drops the names in scripts/excluded_packages.txt -- the only hand-maintained
# part. Verify any change with scripts/check_pruned_deps.py.
COPY scripts/resolve_frappe_deps.py scripts/excluded_packages.txt scripts/extra_packages.txt /tmp/deps/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps git+https://github.com/frappe/frappe.git@${FRAPPE_VERSION} && \
    pip install --no-deps git+https://github.com/frappe/erpnext.git@${ERPNEXT_VERSION} && \
    EXCLUDED_PACKAGES=/tmp/deps/excluded_packages.txt \
    EXTRA_PACKAGES=/tmp/deps/extra_packages.txt \
        python /tmp/deps/resolve_frappe_deps.py > /tmp/deps/frappe-deps.txt && \
    pip install -r /tmp/deps/frappe-deps.txt && \
    #remove all client side files for erpnext and frappe to reduce image size - remove  public folder and .js, .css, .map files
    find /opt/venv/lib/python3.14/site-packages/frappe/public -type f \( -name '*.js' -o -name '*.css' -o -name '*.map' \) -delete && \
    find /opt/venv/lib/python3.14/site-packages/erpnext/public -type f \( -name '*.js' -o -name '*.css' -o -name '*.map' \) -delete && \
    rm -rf /opt/venv/lib/python3.14/site-packages/frappe/public/build && \
    rm -rf /opt/venv/lib/python3.14/site-packages/erpnext/public/build
# Patch Frappe: bundled_asset() must handle get_assets_json() returning None (e.g. headless/microservice)
RUN sed -i 's/bundled_assets = get_assets_json()/bundled_assets = get_assets_json() or {}/' /opt/venv/lib/python3.14/site-packages/frappe/utils/jinja_globals.py

# ---------------------------------------------------------------------------
# Drop gettext translation catalogues (frappe/locale is 46MB of a 73MB package;
# erpnext ships its own). These are UI string translations only -- 33 .po files
# per app, no currency, locale or formatting data in them.
#
# Nothing reads them today: the library never sets frappe.local.lang and `lang`
# is never written to site_config, so frappe falls back to
# `local.lang = local.conf.lang or "en"` on every request and serves English
# regardless. Removing them is a no-op, not a trade-off.
#
# Currency is unaffected -- it does not come from here:
#   conversion  -> ERPNext `Currency Exchange` doctype / external API
#   formatting  -> `Currency` doctype + `currency_precision` default (DB reads)
#   in words    -> `Currency.fraction*` (DB) + the num2words package
#
# If server-side translation is ever needed, shipping .po files is not enough on
# its own -- secure_route must set frappe.local.lang per request first, after
# _restore_frappe_local() resets frappe.local. Then reinstate only the languages
# actually sold into (~1.4MB each) rather than all 33. See docs/dependency-slimming.md.
RUN SP=/opt/venv/lib/python3.14/site-packages && \
    BEFORE=$(du -sm $SP | cut -f1) && \
    rm -rf $SP/frappe/locale $SP/erpnext/locale $SP/frappe/public $SP/erpnext/public && \
    AFTER=$(du -sm $SP | cut -f1) && \
    echo "=== removed locale + public assets: ${BEFORE}MB -> ${AFTER}MB ==="

# Pre-install third-party Python dependencies before copying source code to preserve build cache across local code edits
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
    gunicorn>=22.0 \
    pyjwt==2.8.0 \
    requests==2.32.0 \
    redis==7.2.1 \
    pymysql==1.1.1

# Install the microservice library
COPY . /tmp/frappe-microservice-lib
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /tmp/frappe-microservice-lib && \
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
    ln -s /opt/venv/lib/python3.14/site-packages/erpnext /app/sites/apps/erpnext && \
    ln -s /opt/venv/lib/python3.14/site-packages/frappe /app/sites/apps/frappe

# ============================================
# STAGE 2: Runtime (Debian slim, stable wheels)
# ============================================
FROM python:3.14-slim

# Install ONLY runtime dependencies (no build tools).
# git is deliberately absent: the only Frappe callers are build.py (asset
# building) and utils/change_log.py (the About-dialog version lookup), both of
# which shell out to `git -C ../apps/<app>` -- a path that does not exist in
# this image, so git was already non-functional here. GitPython is pruned too.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    libmariadb3

WORKDIR /app

# Copy Python packages from builder (instead of rebuilding)
COPY --from=builder /opt/venv /opt/venv

# Copy pre-created site structure from builder
COPY scripts/check_pruned_deps.py /app/scripts/check_pruned_deps.py
COPY --from=builder /app/sites /app/sites
COPY --from=builder /app/dev.localhost /app/dev.localhost
COPY --from=builder /logs /logs

# Fix permissions to allow non-root users (e.g., in OpenShift/Kubernetes) to write logs and site configs
RUN mkdir -p /app/logs && chmod -R 777 /app /logs

# Set environment variables
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FRAPPE_SITES_PATH=/app/sites \
    FRAPPE_SITE=dev.localhost \
    PYTHONPATH="/app/sites/apps:$PYTHONPATH" \
    FRAPPE_STREAM_LOGGING=1

EXPOSE 8000

# Default entrypoint: library discovers app via SERVICE_PATH / SERVICE_APP env
ENTRYPOINT ["/opt/venv/bin/python", "-m", "frappe_microservice.entrypoint"]
