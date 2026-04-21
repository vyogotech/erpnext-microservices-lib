"""
Site config helpers for Frappe microservices.

- create_site_config(...): Ensures site_config.json exists in the site directory.
  Used by MicroserviceApp.run() and by the Gunicorn entrypoint (main) before exec.
- _build_config_from_env, _write_config_fallback, _sync_encryption_key: helpers.

This module is separate from entrypoint so that the package can import
create_site_config without loading entrypoint (avoids RuntimeWarning when
running `python -m frappe_microservice.entrypoint`).
"""

import json
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
    Build a site_config dict from args or env (DB_*, REDIS_*, etc.).
    Supports REDIS_QUEUE_HOST / REDIS_CACHE_HOST and REDIS_NAMESPACE.
    Also reads ENCRYPTION_KEY env var to inject into the config when set.
    """
    resolved_redis_host = redis_host or os.getenv('REDIS_HOST', 'localhost')
    resolved_redis_port = int(redis_port or os.getenv('REDIS_PORT', '6379'))
    resolved_redis_queue_host = os.getenv('REDIS_QUEUE_HOST', resolved_redis_host)
    resolved_redis_cache_host = os.getenv('REDIS_CACHE_HOST', resolved_redis_host)
    resolved_redis_namespace = os.getenv('REDIS_NAMESPACE', None)

    # Frappe / Invox convention: site DB user equals db_name (see central-site
    # container-entrypoint writing /secrets/db.env). Defaulting to "frappe"
    # when DB_USER is unset causes MySQL 1045 against a site-named user only.
    resolved_db_name = (db_name or os.getenv('DB_NAME', '')).strip()
    resolved_db_user = (db_user or os.getenv('DB_USER', '') or resolved_db_name or 'frappe').strip()

    config = {
        'db_host': db_host or os.getenv('DB_HOST', 'localhost'),
        'db_port': int(db_port or os.getenv('DB_PORT', '3306')),
        'db_name': resolved_db_name,
        'db_user': resolved_db_user,
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

    encryption_key = os.getenv('ENCRYPTION_KEY')
    if encryption_key:
        config['encryption_key'] = encryption_key

    return config


def _sync_encryption_key(config, config_file):
    """
    Read /secrets/encryption_key.txt and merge into config and on-disk site_config.json.
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
            return config

        config['encryption_key'] = key

        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                disk_cfg = json.load(f)
            disk_cfg['encryption_key'] = key
            with open(config_file, 'w') as f:
                json.dump(disk_cfg, f, indent=2)

    except Exception as e:
        print(f"[site_config] WARNING: Could not sync encryption_key: {e}", file=sys.stderr)

    return config


def _write_config_fallback(site_path, config_file, config):
    """Write config as JSON to config_file when Frappe is not importable."""
    try:
        os.makedirs(site_path, exist_ok=True)
        os.makedirs(os.path.join(site_path, 'logs'), exist_ok=True)
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass


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
    exists, load and return it (after syncing encryption_key). Otherwise build
    config via _build_config_from_env, then use frappe.installer.make_site_config
    or _write_config_fallback. Returns the config dict.
    """
    frappe_sites_path = os.getenv('FRAPPE_SITES_PATH', '/app')
    frappe_site = os.getenv('FRAPPE_SITE', 'dev.localhost')
    site_path = os.path.join(frappe_sites_path, frappe_site)
    config_file = os.path.join(site_path, 'site_config.json')

    # Ensure site and logs dirs exist so Frappe's get_logger() / database.log do not raise ENOENT
    try:
        os.makedirs(site_path, exist_ok=True)
        os.makedirs(os.path.join(site_path, 'logs'), exist_ok=True)
    except OSError:
        pass

    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            config = json.load(f)
        # Stale site_config (e.g. image defaults) must not win over /secrets/db.env:
        # load-secrets runs before Gunicorn, so DB_* are in the environment here.
        env_config = _build_config_from_env(
            db_host, db_port, db_name, db_user, db_password, redis_host, redis_port,
        )
        env_db_name_set = (os.getenv("DB_NAME") or "").strip()
        if env_db_name_set and not (env_config.get("db_name") or "").strip():
            env_config = dict(env_config)
            env_config["db_name"] = env_db_name_set
            if not (env_config.get("db_user") or "").strip():
                env_config["db_user"] = (
                    (os.getenv("DB_USER") or "").strip() or env_db_name_set or "frappe"
                )

        disk_db = (config.get("db_name") or "").strip()
        env_db = (env_config.get("db_name") or "").strip()
        # Merge when env resolved a db name, or when disk has no db name but DB_NAME
        # is set in the environment (image defaults + later-injected secrets).
        # Also merge when DB_USER/DB_PASSWORD are set so user/password refresh matches.
        env_signals = any(
            (os.getenv(k) or "").strip() for k in ("DB_USER", "DB_PASSWORD")
        )
        if env_db or (not disk_db and env_db_name_set) or env_signals:
            merged = False
            for key in (
                "db_host",
                "db_port",
                "db_name",
                "db_user",
                "db_password",
                "redis_cache",
                "redis_queue",
                "redis_socketio",
            ):
                if config.get(key) != env_config.get(key):
                    config[key] = env_config[key]
                    merged = True
            if merged:
                try:
                    with open(config_file, "w") as f:
                        json.dump(config, f, indent=2)
                except OSError:
                    pass
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
        pass

    config = _sync_encryption_key(config, config_file)
    return config
