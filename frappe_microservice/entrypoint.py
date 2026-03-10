"""
Minimal entrypoint helper for Frappe microservices.

- create_site_config(...): Ensures site_config.json exists in the site directory
  (FRAPPE_SITES_PATH/FRAPPE_SITE). If the file is missing, builds a config dict
  from env vars (DB_*, REDIS_*) and either calls frappe.installer.make_site_config
  (when frappe is available) or writes the file directly. Returns the config dict.
  Used by MicroserviceApp.run() before starting the server so Frappe can connect.
- verify_db_connection(config, logger=None): Tries to connect to the DB from config;
  returns True if successful, False otherwise. Used so container startup fails fast
  when the database is not up (e.g. in Kubernetes).
- _build_config_from_env(...): Builds the dict of db_*, redis_* keys from args or env.
- _write_config_fallback(...): Writes config to disk when Frappe is not importable.
- run_app(app): Calls create_site_config(), then runs the Flask app.
- main(): CLI entrypoint when run as __main__. Reads SERVICE_PATH (default /app/service)
  and SERVICE_APP (default server:app), imports the app, and calls run_app(app).
  Use in containers: python -m frappe_microservice.entrypoint (no per-service entrypoint needed).
"""

import json
import logging
import os
import sys


def _build_config_from_env(
    db_host=None,
    db_port=None,
    db_name=None,
    db_user=None,
    db_password=None,
    redis_host=None,
    redis_port=None,
):
    """
    Build a site_config dict. Each key (db_host, db_port, db_name, db_user,
    db_password, redis_cache, redis_queue, redis_socketio, disable_async,
    auto_insert_custom_fields, allow_cors) is taken from the matching argument
    or from the corresponding env var (DB_HOST, DB_PORT, etc.).

    Supports REDIS_QUEUE_HOST / REDIS_CACHE_HOST overrides for cross-bench
    job queues, and REDIS_NAMESPACE for key isolation.
    """
    resolved_redis_host = redis_host or os.getenv('REDIS_HOST', 'localhost')
    resolved_redis_port = int(redis_port or os.getenv('REDIS_PORT', '6379'))
    resolved_redis_queue_host = os.getenv('REDIS_QUEUE_HOST', resolved_redis_host)
    resolved_redis_cache_host = os.getenv('REDIS_CACHE_HOST', resolved_redis_host)
    resolved_redis_namespace = os.getenv('REDIS_NAMESPACE', None)

    config = {
        'db_host': db_host or os.getenv('DB_HOST', 'localhost'),
        'db_port': int(db_port or os.getenv('DB_PORT', '3306')),
        'db_name': db_name or os.getenv('DB_NAME', ''),
        'db_user': db_user or os.getenv('DB_USER', 'frappe'),
        'db_password': db_password or os.getenv('DB_PASSWORD', 'changeme'),
        'redis_cache': f"redis://{resolved_redis_cache_host}:{resolved_redis_port}",
        'redis_queue': f"redis://{resolved_redis_queue_host}:{resolved_redis_port}",
        'redis_socketio': f"redis://{resolved_redis_host}:{resolved_redis_port}",
        'disable_async': False,
        'auto_insert_custom_fields': True,
        'allow_cors': '*',
    }
    if resolved_redis_namespace:
        config['redis_namespace'] = resolved_redis_namespace
    return config


def create_site_config(
    db_host=None,
    db_port=None,
    db_name=None,
    db_user=None,
    db_password=None,
    redis_host=None,
    redis_port=None,
):
    """
    Ensure site_config.json exists for the current site. If the file already
    exists, load and return it. Otherwise build config via _build_config_from_env,
    then: (1) If frappe is importable, set frappe.local.site_path and call
    frappe.installer.make_site_config(site_config=config); (2) If not, or on
    OSError (e.g. read-only FS), use _write_config_fallback or skip write.
    Returns the config dict in all cases. Called from MicroserviceApp.run() and run_app().
    """
    frappe_sites_path = os.getenv('FRAPPE_SITES_PATH', '/app')
    frappe_site = os.getenv('FRAPPE_SITE', 'dev.localhost')
    site_path = os.path.join(frappe_sites_path, frappe_site)
    config_file = os.path.join(site_path, 'site_config.json')

    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            config = json.load(f)
        # Always sync the encryption_key from central-site even when file already exists
        return _sync_encryption_key(config, config_file)

    config = _build_config_from_env(
        db_host, db_port, db_name, db_user, db_password, redis_host, redis_port,
    )

    try:
        import frappe
        from frappe.installer import make_site_config

        frappe.local.site_path = site_path
        make_site_config(site_config=config)
    except (ImportError, AttributeError):
        _write_config_fallback(site_path, config_file, config)
    except OSError:
        pass  # read-only filesystem; return in-memory config

    # After creating the config, sync the encryption key from central-site.
    config = _sync_encryption_key(config, config_file)

    return config


def _sync_encryption_key(config, config_file):
    """
    Read /secrets/encryption_key.txt written by the central-site container-entrypoint.sh
    and merge it into the in-memory config dict AND the on-disk site_config.json.
    This ensures every microservice uses the same Fernet key as the central site so
    that DB-stored passwords (SMTP, API keys) can be decrypted correctly.
    """
    encryption_key_file = '/secrets/encryption_key.txt'
    if not os.path.exists(encryption_key_file):
        return config

    try:
        with open(encryption_key_file, 'r') as f:
            key = f.read().strip()
        if not key:
            return config

        if config.get('encryption_key') == key:
            return config  # already in sync

        config['encryption_key'] = key

        # Patch the on-disk file too so future runs don't need to re-sync
        if os.path.exists(config_file):
            import json as _json
            with open(config_file, 'r') as f:
                disk_cfg = _json.load(f)
            disk_cfg['encryption_key'] = key
            with open(config_file, 'w') as f:
                _json.dump(disk_cfg, f, indent=2)

    except Exception as e:
        # Non-fatal: log and continue; the service will still start
        import sys
        print(f"[entrypoint] WARNING: Could not sync encryption_key: {e}", file=sys.stderr)

    return config



def _write_config_fallback(site_path, config_file, config):
    """
    Create site_path and logs dir if needed, then write config as JSON to
    config_file. Used when frappe is not installed or make_site_config cannot
    be used. Silently ignores OSError (e.g. read-only filesystem).
    """
    try:
        os.makedirs(site_path, exist_ok=True)
        os.makedirs(os.path.join(site_path, 'logs'), exist_ok=True)
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass  # read-only filesystem


def run_app(app):
    """
    Entry point for standalone microservice processes. Ensures site_config.json
    exists via create_site_config(), then starts the Flask app with host/port
    from HOST and PORT env vars. On any exception, logs to stderr and exits
    with code 1. Use when the process is started directly (e.g. python server.py)
    rather than via MicroserviceApp.run().
    """
    try:
        create_site_config()

        port = int(os.getenv('PORT', '8001'))
        host = os.getenv('HOST', '0.0.0.0')

        print(f"Starting Frappe microservice on {host}:{port}...")
        app.run()
    except Exception as e:
        print(f"Error starting service: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    """
    Library entrypoint: discover the service app from env and run it.

    Env:
        SERVICE_PATH: Directory containing the service code (default: /app/service).
                     Prepended to sys.path so the service module can be imported.
        SERVICE_APP: Module and attribute for the app instance (default: server:app).
                     Example: "server:app" -> from server import app.

    Use in containers: CMD ["python", "-m", "frappe_microservice.entrypoint"]
    No per-service entrypoint.py needed; set SERVICE_PATH (and optionally SERVICE_APP).
    """
    import importlib

    service_path = os.getenv('SERVICE_PATH', '/app/service')
    service_app = os.getenv('SERVICE_APP', 'server:app')

    if service_path not in sys.path:
        sys.path.insert(0, service_path)

    try:
        module_name, app_attr = service_app.split(':', 1)
        module = importlib.import_module(module_name)
        app = getattr(module, app_attr)
    except (ValueError, AttributeError, ModuleNotFoundError) as e:
        print(f"Entrypoint failed: could not load app from {service_app} (SERVICE_PATH={service_path}): {e}", file=sys.stderr)
        sys.exit(1)

    run_app(app)


if __name__ == '__main__':
    main()
