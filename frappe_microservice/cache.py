"""
Shared Redis Cache Isolation Module.

This module provides utilities to isolate the Redis cache keys used by different
microservices that share the same Redis instance and MariaDB database name.
Normally, Frappe prefixes all keys with the site's database name (e.g. `f"{db_name}|"`).
When multiple microservices and the central site share the same DB name, their cache
entries (such as `app_hooks` and `doctype_meta:<name>`) collide, causing import
errors or stale schema resolution.

This module resolves that by monkey-patching Frappe's `RedisWrapper.make_key` to inject
a service-specific namespace prefix (e.g., `expense_service:`) to all non-shared cache keys.
"""

import logging

# We import types dynamically inside functions to avoid early import conflicts
# before Frappe's framework or site config are fully initialized.

logger = logging.getLogger(__name__)


def patch_redis_make_key(service_name: str | None, service_logger=None) -> None:
    """Monkey-patch RedisWrapper.make_key to namespace cache keys by service name.

    If `shared=True` is requested on key lookup, the prefix is skipped to allow
    accessing global cache values where sharing is intentional.

    Args:
        service_name: Name of the service (e.g., 'expense-service').
        service_logger: Logger instance to print trace information.
    """
    if not service_name:
        return

    log = service_logger or logger

    try:
        from frappe.utils.redis_wrapper import RedisWrapper

        # 1. Check if patch is already applied to avoid double patching/infinite recursion
        if getattr(RedisWrapper, "_microservice_key_patched", False):
            return

        original_make_key = RedisWrapper.make_key
        service_namespace = service_name.replace("-", "_")  # e.g. "expense_service"

        # 2. Define the wrapper around original make_key
        def microservice_make_key(instance, key, user=None, shared=False):
            # We only namespace keys that are NOT marked as explicitly shared
            if not shared:
                # Keys can be string or bytes in Frappe. Handle both.
                if isinstance(key, bytes):
                    key_str = key.decode("utf-8", errors="ignore")
                else:
                    key_str = str(key)

                # Avoid double-prepending the namespace if it's already there
                if not key_str.startswith(f"{service_namespace}:"):
                    key = f"{service_namespace}:{key_str}"

            # Pass the modified key down to the original make_key function.
            # The original make_key will format the final key as:
            # f"{self.db_name}|{key}" -> e.g. b"_25554c96261fc106|expense_service:app_hooks"
            return original_make_key(instance, key, user=user, shared=shared)

        # 3. Apply the monkey-patch
        RedisWrapper.make_key = microservice_make_key
        RedisWrapper._microservice_key_patched = True

        log.info(
            f"Patched RedisWrapper.make_key to namespace cache keys with '{service_namespace}'"
        )

    except Exception as e:
        log.warning("Failed to patch RedisWrapper.make_key: %s", e)
