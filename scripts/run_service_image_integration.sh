#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Run tests/integration/ from inside the built SERVICE IMAGE.
#
# Why this exists: the normal integration run (scripts/run_integration_tests.sh)
# executes inside the central-site bench container, which has the full unpruned
# Frappe install. It validates the library but says nothing about the image we
# actually ship -- a broken dependency prune would still show all green there.
#
# This harness runs the same suite from the service image, against central-site's
# MariaDB and Redis, exactly as a microservice connects in production.
#
# Usage:
#   ./scripts/run_service_image_integration.sh
#   SERVICE_IMAGE=localhost/my-image:tag ./scripts/run_service_image_integration.sh
#   KEEP_RUNNING=1 ./scripts/run_service_image_integration.sh
#
# EXPECTED RESULT: 12 passed, 9 skipped, 0 failed.
#
# The 9 skips are all of test_tenant_id_stamping.py. Every test there asserts on
# tenancy_app's "*" before_insert hook, which service images deliberately do
# not ship (see tenant.py -- the library's get_valid_dict patch replaces it), so
# the module's premise does not hold here. Its autouse _requires_tenancy_app
# fixture checks both that the app is recorded as installed AND that this process
# can import it; running against a central-site database makes those diverge.
#
# ONE SUITE PER STACK. Do not run this and scripts/run_integration_tests.sh
# against the same live containers: both mutate the shared site database and each
# leaves state that breaks the other (verified in both directions -- a prior
# central-site run turns this into 10 failures, and vice versa). The teardown
# below does `down -v` so every invocation starts clean; KEEP_RUNNING=1 defeats
# that, so use it only for a single run you intend to debug.
# ---------------------------------------------------------------------------
set -euo pipefail

COMPOSE_FILE="docker-compose.service-integration.yml"
export SERVICE_IMAGE="${SERVICE_IMAGE:-localhost/frappe-microservice-lib:latest}"
MAX_WAIT=40
WAIT_INTERVAL=10

if command -v docker &>/dev/null; then COMPOSE="docker compose"
elif command -v podman &>/dev/null; then COMPOSE="podman compose"
else echo "FATAL: neither docker nor podman found" >&2; exit 1
fi

log()  { printf "\n=== %s ===\n" "$*"; }
fail() { echo "FATAL: $*" >&2; exit 1; }

cleanup() {
    if [[ "${KEEP_RUNNING:-}" == "1" ]]; then
        log "KEEP_RUNNING=1 -- containers left up"
    else
        log "Tearing down"
        $COMPOSE -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    fi
}
trap cleanup EXIT

log "Starting central-site + service ($SERVICE_IMAGE)"
$COMPOSE -f "$COMPOSE_FILE" up -d

BENCH_PY=/home/frappe/frappe-bench/env/bin/python
SITE_CFG=/home/frappe/frappe-bench/sites/dev.localhost/site_config.json

log "Waiting for central-site (up to $((MAX_WAIT * WAIT_INTERVAL))s)"
READY=0
for i in $(seq 1 "$MAX_WAIT"); do
    if $COMPOSE -f "$COMPOSE_FILE" exec -T central-site "$BENCH_PY" -c "
import frappe
frappe.init(site='dev.localhost', sites_path='/home/frappe/frappe-bench/sites')
frappe.connect(); frappe.db.sql('SELECT 1'); frappe.destroy()" >/dev/null 2>&1; then
        READY=1; break
    fi
    printf "  attempt %d/%d ...\n" "$i" "$MAX_WAIT"
    sleep "$WAIT_INTERVAL"
done
[[ "$READY" == "1" ]] || fail "central-site not ready"

# The service connects with the site's own DB credentials, discovered here rather
# than hard-coded -- they are generated per site.
read_cfg() {
    $COMPOSE -f "$COMPOSE_FILE" exec -T central-site "$BENCH_PY" -c \
        "import json;print(json.load(open('$SITE_CFG'))['$1'])" 2>/dev/null | tr -d '\r'
}
DB_NAME="$(read_cfg db_name)"
DB_PASSWORD="$(read_cfg db_password)"
[[ -n "$DB_NAME" ]] || fail "could not read db_name from central-site"
log "Service will use db=$DB_NAME on central-site"

log "Installing pytest into the service image"
$COMPOSE -f "$COMPOSE_FILE" exec -T service /opt/venv/bin/pip install --quiet pytest

log "Writing the service's site_config.json"
$COMPOSE -f "$COMPOSE_FILE" exec -T \
    -e DB_NAME="$DB_NAME" -e DB_USER="$DB_NAME" -e DB_PASSWORD="$DB_PASSWORD" \
    service /opt/venv/bin/python -c "
import os
p = '/app/sites/dev.localhost/site_config.json'
if os.path.exists(p):
    os.remove(p)
from frappe_microservice.site_config import create_site_config
create_site_config()"

log "Running tests/integration/ from the service image"
$COMPOSE -f "$COMPOSE_FILE" exec -T \
    -e DB_NAME="$DB_NAME" -e DB_USER="$DB_NAME" -e DB_PASSWORD="$DB_PASSWORD" \
    service /opt/venv/bin/python -m pytest /mnt/lib/tests/integration/ \
        --confcutdir=/mnt/lib/tests/integration \
        -c /mnt/lib/tests/integration/pytest.ini \
        -v --tb=short -p no:cacheprovider

log "Done"
