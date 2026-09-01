"""
Minimal CLI entrypoint for containerized Frappe microservices.

When run as __main__:
  1. Ensures site_config.json exists (via site_config.create_site_config).
  2. Pre-syncs service doctypes to the DB once (via isolation.presync_service_doctypes).
  3. Execs Gunicorn with SERVICE_APP (e.g. server:app).

Step 2 is the "static block": it runs once per container before any Gunicorn
workers exist. When it succeeds, ``_DOCTYPES_PRESYNCED=1`` is set so workers
skip a second DB sync. When it fails or is skipped (e.g. DB not ready), that
flag is left unset so the first request can retry via MicroserviceApp.
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
    # Container-safe logging: set the stream-logging default before any
    # frappe.logger() call in the presync path, and chdir into the sites path
    # so any residual CWD-relative Frappe log path lands in the writable sites
    # volume.
    os.environ.setdefault("FRAPPE_STREAM_LOGGING", "1")
    sites_path = os.getenv("FRAPPE_SITES_PATH", "/app/sites")
    if os.path.isdir(sites_path):
        try:
            os.chdir(sites_path)
        except OSError as e:
            print(
                f"[entrypoint] WARNING: could not chdir to {sites_path}: {e}",
                file=sys.stderr,
            )

    create_site_config()
    # Belt-and-braces: also mkdir CWD-relative /app/<site>/logs for any path
    # that still uses file log handlers before workers start.
    from frappe_microservice.site_config import ensure_site_log_dirs

    ensure_site_log_dirs(sites_path=sites_path)

    # One-shot doctype + fixture DB sync (runs before any workers exist).
    # Only claim success when presync returns True — otherwise leave workers
    # free to sync on first request (first-boot DB race / connect skip).
    from frappe_microservice.presync import presync_service_doctypes

    presync_ok = bool(presync_service_doctypes())

    service_path = os.getenv("SERVICE_PATH", "/app/service")
    service_app = os.getenv("SERVICE_APP", "server:app")
    port = os.getenv("PORT", "8000")
    workers = os.getenv("GUNICORN_WORKERS", "4")
    timeout = os.getenv("GUNICORN_TIMEOUT", "120")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([service_path, env.get("PYTHONPATH", "")])
    if presync_ok:
        env["_DOCTYPES_PRESYNCED"] = "1"
    else:
        env.pop("_DOCTYPES_PRESYNCED", None)
        print(
            "[entrypoint] WARNING: DocType/fixture presync did not complete; "
            "workers will retry sync on first request",
            file=sys.stderr,
        )

    gunicorn = "/opt/venv/bin/gunicorn"
    args = [
        gunicorn,
        "--preload",
        f"--bind=0.0.0.0:{port}",
        f"--workers={workers}",
        "--worker-class=sync",
        "--worker-tmp-dir=/dev/shm",
        f"--timeout={timeout}",
        # Log to stdout/stderr so Kubernetes picks up every request (kubectl logs).
        "--access-logfile=-",
        "--error-logfile=-",
        service_app,
    ]

    try:
        os.execvpe(gunicorn, args, env)
    except Exception as e:
        print(f"Error starting Gunicorn: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
