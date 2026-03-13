"""
Minimal CLI entrypoint for containerized Frappe microservices.

When run as __main__:
  1. Ensures site_config.json exists (via site_config.create_site_config).
  2. Pre-syncs service doctypes to the DB once (via isolation.presync_service_doctypes).
  3. Execs Gunicorn with SERVICE_APP (e.g. server:app).

Step 2 is the "static block": it runs exactly once per container before any
Gunicorn workers exist, so doctypes are synced once no matter how many workers.
Multiple pods are safe because the sync is idempotent (check-then-create).

Gunicorn is started with --preload so the app is loaded once in the master
before workers fork. That way Frappe init and isolation patches run once per
container; each worker gets its own copy of the Frappe local state on first
request (no shared mutable state).

Use in containers: ENTRYPOINT ["python", "-m", "frappe_microservice.entrypoint"]
"""

import os
import sys

from frappe_microservice.site_config import create_site_config


def main():
    """
    Ensure site_config.json exists, pre-sync doctypes + fixtures, then exec Gunicorn.
    Env: SERVICE_PATH, SERVICE_APP, PORT, GUNICORN_WORKERS, GUNICORN_TIMEOUT,
         DOCTYPES_PATH, SERVICE_NAME, FRAPPE_SITE, FRAPPE_SITES_PATH.
    Fixtures auto-discovered at <SERVICE_PATH>/fixtures/ (convention over config).
    """
    create_site_config()

    # One-shot doctype + fixture DB sync (runs before any workers exist)
    from frappe_microservice.isolation import presync_service_doctypes
    presync_service_doctypes()

    service_path = os.getenv('SERVICE_PATH', '/app/service')
    service_app = os.getenv('SERVICE_APP', 'server:app')
    port = os.getenv('PORT', '8000')
    workers = os.getenv('GUNICORN_WORKERS', '4')
    timeout = os.getenv('GUNICORN_TIMEOUT', '120')

    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join([service_path, env.get('PYTHONPATH', '')])
    env['_DOCTYPES_PRESYNCED'] = '1'

    gunicorn = '/opt/venv/bin/gunicorn'
    args = [
        gunicorn,
        '--preload',
        f'--bind=0.0.0.0:{port}',
        f'--workers={workers}',
        '--worker-class=sync',
        '--worker-tmp-dir=/dev/shm',
        f'--timeout={timeout}',
        service_app,
    ]

    try:
        os.execvpe(gunicorn, args, env)
    except Exception as e:
        print(f"Error starting Gunicorn: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
