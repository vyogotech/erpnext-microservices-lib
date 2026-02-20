"""
Minimal entrypoint helper for Frappe microservices.
Handles site_config.json creation and server startup.
"""

import json
import os
import sys
from pathlib import Path


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
    Create site_config.json from explicit args or environment variables.

    Returns:
        dict: The generated configuration.
    """
    # Setup paths according to how Frappe Operator stores sites
    frappe_sites_path = os.getenv('FRAPPE_SITES_PATH', '/app/sites/frappe-sites')
    frappe_site = os.getenv('FRAPPE_SITE', 'dev.localhost')
    site_path = Path(frappe_sites_path) / frappe_site
    config_file = site_path / 'site_config.json'

    if config_file.exists():
        with open(config_file, 'r') as f:
            return json.load(f)

    # Build config from explicit args or environment variables
    resolved_db_host = db_host or os.getenv('DB_HOST', 'localhost')
    resolved_db_port = int(db_port or os.getenv('DB_PORT', '3306'))
    resolved_db_name = db_name or os.getenv('DB_NAME', '')
    resolved_db_user = db_user or os.getenv('DB_USER', 'frappe')
    resolved_db_password = db_password or os.getenv('DB_PASSWORD', 'changeme')
    resolved_redis_host = redis_host or os.getenv('REDIS_HOST', 'localhost')
    resolved_redis_port = int(redis_port or os.getenv('REDIS_PORT', '6379'))

    # Create directory if needed (may fail outside container)
    try:
        site_path.mkdir(parents=True, exist_ok=True)
        # Frappe expects the site directory to be within its CWD.
        # Ensure it exists in CWD as well by symlinking it.
        app_site_link = Path(os.getcwd()) / frappe_site
        if not app_site_link.exists() and not app_site_link.is_symlink():
            os.symlink(site_path, app_site_link)

        logs_path = site_path / "logs"
        logs_path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Return config without writing if filesystem is read-only
        return {
            'db_host': resolved_db_host,
            'db_port': resolved_db_port,
            'db_name': resolved_db_name,
            'db_user': resolved_db_user,
            'db_password': resolved_db_password,
            'redis_cache': f"redis://{resolved_redis_host}:{resolved_redis_port}",
            'redis_queue': f"redis://{resolved_redis_host}:{resolved_redis_port}",
            'redis_socketio': f"redis://{resolved_redis_host}:{resolved_redis_port}",
            'disable_async': False,
            'auto_insert_custom_fields': True,
            'allow_cors': '*'
        }

    config = {
        'db_host': resolved_db_host,
        'db_port': resolved_db_port,
        'db_name': resolved_db_name,
        'db_user': resolved_db_user,
        'db_password': resolved_db_password,
        'redis_cache': f"redis://{resolved_redis_host}:{resolved_redis_port}",
        'redis_queue': f"redis://{resolved_redis_host}:{resolved_redis_port}",
        'redis_socketio': f"redis://{resolved_redis_host}:{resolved_redis_port}",
        'disable_async': False,
        'auto_insert_custom_fields': True,
        'allow_cors': '*'
    }

    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)

    return config


def run_app(app):
    """
    Start a Frappe microservice Flask app.

    This should be called after importing your Flask app:
        from server import app
        from frappe_microservice.entrypoint import run_app
        run_app(app)
    """
    try:
        # Create site_config.json if needed
        create_site_config()

        # Get port from environment or use default. Host is controlled
        # by the microservice factory to avoid duplicate kwargs.
        port = int(os.getenv('PORT', '8001'))
        host = os.getenv('HOST', '0.0.0.0')

        print(f"Starting Frappe microservice on {host}:{port}...")
        # Let the microservice factory or app wrapper handle all run args.
        app.run()
    except Exception as e:
        print(f"Error starting service: {e}", file=sys.stderr)
        sys.exit(1)
