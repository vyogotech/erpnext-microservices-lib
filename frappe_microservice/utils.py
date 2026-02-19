# Deprecated: Use frappe_microservice.entrypoint instead
# This module is kept for backward compatibility only
from frappe_microservice.entrypoint import create_site_config as generate_site_config

__all__ = ['generate_site_config']