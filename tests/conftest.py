import pytest
from unittest.mock import MagicMock, patch
from contextvars import ContextVar
import sys


class FrappeDict(dict):
    """Minimal replica of frappe._dict: a dict that also allows attribute access."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self[key] = value

    def __delattr__(self, key):
        try:
            del self[key]
        except KeyError:
            raise AttributeError(key)


# ---------------------------------------------------------------------------
# Build a minimal mock of frappe and all sub-modules it tries to import.
# Must happen BEFORE any frappe_microservice code is imported.
# ---------------------------------------------------------------------------
mock_frappe = MagicMock()
mock_frappe.PermissionError = type('PermissionError', (Exception,), {'__module__': 'frappe'})
mock_frappe.DoesNotExistError = type('DoesNotExistError', (Exception,), {'__module__': 'frappe'})
mock_frappe.ValidationError = type('ValidationError', (Exception,), {'__module__': 'frappe'})
mock_frappe.AuthenticationError = type('AuthenticationError', (Exception,), {'__module__': 'frappe'})
mock_frappe.LinkValidationError = type('LinkValidationError', (Exception,), {'__module__': 'frappe'})
mock_frappe._dict = FrappeDict
mock_frappe.local = MagicMock()
mock_frappe.session = MagicMock()

# frappe.utils.local._contextvar is used directly by app.py
_test_contextvar = ContextVar("frappe_local_test", default=None)
mock_utils_local = MagicMock()
mock_utils_local._contextvar = _test_contextvar

mock_cache_manager = MagicMock()
mock_cache_manager.reset_metadata_version = MagicMock()

sys.modules['frappe'] = mock_frappe
sys.modules['frappe.utils'] = MagicMock()
sys.modules['frappe.utils.local'] = mock_utils_local
sys.modules['frappe.frappeclient'] = MagicMock()
sys.modules['frappe.modules'] = MagicMock()
sys.modules['frappe.modules.import_file'] = MagicMock()
sys.modules['frappe.model'] = MagicMock()
sys.modules['frappe.model.base_document'] = MagicMock()
sys.modules['frappe.model.document'] = MagicMock()
sys.modules['frappe.installer'] = MagicMock()
sys.modules['frappe.database'] = MagicMock()
sys.modules['frappe.cache_manager'] = mock_cache_manager


@pytest.fixture(autouse=True)
def setup_mocks(monkeypatch):
    """Reset mocks between tests and provide a clean frappe.local state."""
    mock_frappe.reset_mock()
    mock_frappe.init = MagicMock()
    mock_frappe.connect = MagicMock()
    mock_frappe.set_user = MagicMock()
    mock_frappe.get_all = MagicMock(return_value=[])
    mock_frappe.get_doc = MagicMock()
    mock_frappe.db = MagicMock()
    mock_frappe.local = MagicMock()
    mock_frappe.local.db = MagicMock()
    mock_frappe.cache = MagicMock()
    _test_contextvar.set({})

    # _service_app_is_importable uses importlib.util.find_spec to decide whether
    # to include the service app in the allowed-apps list.  In unit tests,
    # fake service names (signup_service, order_service, ...) are not real
    # Python packages; always return a truthy spec so the service app is included.
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: MagicMock())

    yield
    mock_frappe.reset_mock()


@pytest.fixture
def test_contextvar():
    """Expose the test ContextVar for assertions."""
    return _test_contextvar
