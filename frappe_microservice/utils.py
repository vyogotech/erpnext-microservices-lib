import json
import os
import sys
from pathlib import Path

# Instead of depending on the whole frappe framework just for utils,
# we re-implement the minimal needed functionality here for generating site_config.json based on the env
def generate_site_config():
    """Generate site_config.json from environment variables."""
    frappe_sites_path = os.getenv('FRAPPE_SITES_PATH', '/app/sites')
    frappe_site = os.getenv('FRAPPE_SITE', 'dev.localhost')
    site_path = Path(frappe_sites_path) / frappe_site
    config_file = site_path / 'site_config.json'

    if config_file.exists():
        return

    # Create directory if needed
    site_path.mkdir(parents=True, exist_ok=True)

    # Build config from environment variables
    allow_cors = os.getenv('ALLOW_CORS')
    if allow_cors is None:
        allow_cors = ''  # Default to empty string if not set in environment variables

    config = {
        'db_host': os.getenv('DB_HOST', 'localhost'),
        'db_port': int(os.getenv('DB_PORT', '3306')),
        'db_name': os.getenv('DB_NAME', ''),
        'db_user': os.getenv('DB_USER', 'frappe'),
        'db_password': os.getenv('DB_PASSWORD', ''),  # Fetch from environment variable
        'redis_cache': f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}",
        'redis_queue': f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}",
        'redis_socketio': f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}",
        'disable_async': False,
        'auto_insert_custom_fields': True,
        'allow_cors': allow_cors
    }

    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)