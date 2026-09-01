"""Unit tests for Redis key namespacing and isolation."""

import sys
from unittest.mock import MagicMock, patch

from frappe_microservice.cache import patch_redis_make_key


def test_patch_redis_make_key_early_return():
    """Verify that calling patch_redis_make_key with None or empty service name does nothing."""
    with patch("frappe.utils.redis_wrapper.RedisWrapper", create=True) as mock_wrapper:
        patch_redis_make_key(None)
        # Since it returns early, the mock should never be touched or loaded
        assert not mock_wrapper.mock_calls


def test_patch_redis_make_key_success():
    """Verify that RedisWrapper.make_key is correctly patched and namespaces keys."""

    # Create a mock RedisWrapper class
    class DummyRedisWrapper:
        _microservice_key_patched = False
        db_name = "test_db"

        def make_key(self, key, user=None, shared=False):
            if shared:
                return key
            if user:
                key = f"{key}|{user}"
            return f"{self.db_name}|{key}".encode("utf-8")

    # Patch the class in sys.modules / frappe.utils.redis_wrapper
    dummy_wrapper_module = MagicMock()
    dummy_wrapper_module.RedisWrapper = DummyRedisWrapper

    with patch.dict(sys.modules, {"frappe.utils.redis_wrapper": dummy_wrapper_module}):
        # Apply patch
        logger_mock = MagicMock()
        patch_redis_make_key("expense-service", service_logger=logger_mock)

        assert DummyRedisWrapper._microservice_key_patched is True
        logger_mock.info.assert_called_once()

        # Test the patched method behavior
        instance = DummyRedisWrapper()

        # 1. Unshared string key
        key_1 = instance.make_key("app_hooks", shared=False)
        assert key_1 == b"test_db|expense_service:app_hooks"

        # 2. Shared string key (should NOT be namespaced)
        key_2 = instance.make_key("app_hooks", shared=True)
        assert key_2 == "app_hooks"

        # 3. Unshared bytes key
        key_3 = instance.make_key(b"doctype_meta:Item", shared=False)
        assert key_3 == b"test_db|expense_service:doctype_meta:Item"

        # 4. Already prefixed key (should NOT double-prefix)
        key_4 = instance.make_key("expense_service:app_hooks", shared=False)
        assert key_4 == b"test_db|expense_service:app_hooks"

        # 5. Key with user
        key_5 = instance.make_key("app_hooks", user="Administrator", shared=False)
        assert key_5 == b"test_db|expense_service:app_hooks|Administrator"


def test_patch_redis_make_key_idempotent():
    """Verify that patching is idempotent and won't re-apply if already patched."""

    class DummyRedisWrapper:
        _microservice_key_patched = True

        def make_key(self, key, user=None, shared=False):
            return key

    dummy_wrapper_module = MagicMock()
    dummy_wrapper_module.RedisWrapper = DummyRedisWrapper

    with patch.dict(sys.modules, {"frappe.utils.redis_wrapper": dummy_wrapper_module}):
        logger_mock = MagicMock()
        patch_redis_make_key("expense-service", service_logger=logger_mock)

        # Should not log any success since it's already patched
        logger_mock.info.assert_not_called()


def test_patch_redis_make_key_exception_handling():
    """Verify that exceptions during patching are caught and logged as warnings."""
    # Trigger an ImportError/AttributeError on import/getattr
    with patch(
        "frappe.utils.redis_wrapper.RedisWrapper",
        side_effect=ImportError("mock import error"),
        create=True,
    ):
        logger_mock = MagicMock()
        patch_redis_make_key("expense-service", service_logger=logger_mock)
        logger_mock.warning.assert_called_once()
        assert (
            "Failed to patch RedisWrapper.make_key"
            in logger_mock.warning.call_args[0][0]
        )
