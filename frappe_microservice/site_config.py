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

# create_site_config() runs multiple times per boot (app init, entrypoint,
# run()); warn about the permissive CORS default only once per process.
_allow_cors_warning_emitted = False


def _resolve_int_env(raw_value, default):
    """Cast env/arg port values; empty or whitespace-only strings fall back to default."""
    if raw_value is None:
        return int(default)
    if isinstance(raw_value, int):
        return raw_value
    text = str(raw_value).strip()
    if not text:
        return int(default)
    return int(text)


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
    resolved_redis_port = _resolve_int_env(
        redis_port if redis_port is not None else os.getenv('REDIS_PORT', '6379'),
        6379,
    )
    resolved_redis_queue_host = os.getenv('REDIS_QUEUE_HOST', resolved_redis_host)
    resolved_redis_cache_host = os.getenv('REDIS_CACHE_HOST', resolved_redis_host)
    resolved_redis_namespace = os.getenv('REDIS_NAMESPACE', None)

    # Frappe convention: site DB user equals db_name (see central-site
    # container-entrypoint writing /secrets/db.env). Defaulting to "frappe"
    # when DB_USER is unset causes MySQL 1045 against a site-named user only.
    resolved_db_name = (db_name or os.getenv('DB_NAME', '')).strip()
    resolved_db_user = (db_user or os.getenv('DB_USER', '') or resolved_db_name or 'frappe').strip()

    # Prefer explicit arg, then DB_HOST_PORT (K8s/service style), then DB_PORT.
    # Empty strings (e.g. DB_PORT="") must not raise ValueError on int().
    if db_port is not None:
        db_port_env = db_port
    else:
        db_port_env = os.getenv('DB_HOST_PORT') or os.getenv('DB_PORT', '3306')
    resolved_db_port = _resolve_int_env(db_port_env, 3306)

    # CORS: honor ALLOW_CORS when set; otherwise keep the historical '*' default
    # but warn (this default will change in 2.0).
    allow_cors_env = os.getenv('ALLOW_CORS')
    if allow_cors_env is not None:
        resolved_allow_cors = allow_cors_env
    else:
        resolved_allow_cors = '*'
        global _allow_cors_warning_emitted
        if not _allow_cors_warning_emitted:
            _allow_cors_warning_emitted = True
            print(
                "[site_config] WARNING: ALLOW_CORS not set; defaulting to '*'. "
                "This permissive default will change in 2.0 — set ALLOW_CORS "
                "explicitly (e.g. your app origin) in production.",
                file=sys.stderr,
            )

    config = {
        'db_host': db_host or os.getenv('DB_HOST', 'localhost'),
        'db_port': resolved_db_port,
        'db_name': resolved_db_name,
        'db_user': resolved_db_user,
        'db_password': db_password or os.getenv('DB_PASSWORD', 'changeme'),
        'redis_cache': f"redis://{resolved_redis_cache_host}:{resolved_redis_port}",
        'redis_queue': f"redis://{resolved_redis_queue_host}:{resolved_redis_port}",
        'redis_socketio': f"redis://{resolved_redis_host}:{resolved_redis_port}",
        'disable_async': False,
        'auto_insert_custom_fields': True,
        'allow_cors': resolved_allow_cors,
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


def ensure_site_log_dirs(site=None, sites_path=None):
    """
    Create site ``logs/`` dirs that Frappe may open relative to CWD.

    Frappe's ``create_handler`` (when not stream-logging) opens
    ``<site>/logs/<module>.log`` relative to the process CWD. Container
    WORKDIR is ``/app``, while site_config lives under ``FRAPPE_SITES_PATH``
    (``/app/sites``). Presync historically only mkdir'd under sites_path, so
    ``frappe.connect()`` failed with::

        FileNotFoundError: ... '/app/<site>/logs/database.log'

    and silently skipped fixture / SERVICE_POST_PRESYNC sync. Mirror the
    bases used by ``MicroserviceApp.__init__`` so entrypoint and presync are
    safe before the Flask app is constructed.
    """
    site = site or os.getenv("FRAPPE_SITE", "dev.localhost")
    sites_path = sites_path or os.getenv("FRAPPE_SITES_PATH", "/app/sites")
    bases = ["/app", "/app/sites", sites_path, os.getcwd()]
    seen = set()
    for base in bases:
        if not base:
            continue
        key = os.path.abspath(base)
        if key in seen:
            continue
        seen.add(key)
        try:
            os.makedirs(os.path.join(base, site, "logs"), exist_ok=True)
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
    # Must match entrypoint / MicroserviceApp / Containerfile defaults.
    # Historical default of '/app' created site_config under /app/<site> while
    # frappe.init used /app/sites — split brain for logs and site_config.
    frappe_sites_path = os.getenv('FRAPPE_SITES_PATH', '/app/sites')
    frappe_site = os.getenv('FRAPPE_SITE', 'dev.localhost')
    site_path = os.path.join(frappe_sites_path, frappe_site)
    config_file = os.path.join(site_path, 'site_config.json')

    # Ensure site and logs dirs exist so Frappe's get_logger() / database.log do not raise ENOENT
    ensure_site_log_dirs(site=frappe_site, sites_path=frappe_sites_path)
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
        # Only override allow_cors on an existing config when ALLOW_CORS is
        # explicitly set — never clobber operator edits with the '*' default.
        allow_cors_set = os.getenv("ALLOW_CORS") is not None
        if env_db or (not disk_db and env_db_name_set) or env_signals or allow_cors_set:
            merged = False
            merge_keys = [
                "db_host",
                "db_port",
                "db_name",
                "db_user",
                "db_password",
                "redis_cache",
                "redis_queue",
                "redis_socketio",
            ]
            if allow_cors_set:
                merge_keys.append("allow_cors")
            for key in merge_keys:
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
