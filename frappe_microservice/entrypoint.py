"""
Minimal entrypoint helper for Frappe microservices.
Handles site_config.json creation and server startup.
"""

import json
import os
import sys
from pathlib import Path


def create_site_config():
    """Create site_config.json from environment variables if it doesn't exist."""
    frappe_sites_path = os.getenv('FRAPPE_SITES_PATH', '/app/sites')
    frappe_site = os.getenv('FRAPPE_SITE', 'dev.localhost')
    site_path = Path(frappe_sites_path) / frappe_site
    config_file = site_path / 'site_config.json'

    if config_file.exists():
        return

    # Create directory if needed
    site_path.mkdir(parents=True, exist_ok=True)

    # Build config from environment variables
    config = {
        'db_host': os.getenv('DB_HOST', 'localhost'),
        'db_port': int(os.getenv('DB_PORT', '3306')),
        'db_name': os.getenv('DB_NAME', ''),
        'db_user': os.getenv('DB_USER', 'frappe'),
        'db_password': os.getenv('DB_PASSWORD', 'changeme'),
        'redis_cache': f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}",
        'redis_queue': f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}",
        'redis_socketio': f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}",
        'disable_async': False,
        'auto_insert_custom_fields': True,
        'allow_cors': '*'
    }

    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)


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
