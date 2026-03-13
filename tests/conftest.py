import pytest
from unittest.mock import MagicMock, patch
from contextvars import ContextVar
import sys

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
mock_frappe._dict = dict
mock_frappe.local = MagicMock()
mock_frappe.session = MagicMock()

# frappe.utils.local._contextvar is used directly by app.py
_test_contextvar = ContextVar("frappe_local_test", default=None)
mock_utils_local = MagicMock()
mock_utils_local._contextvar = _test_contextvar

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


@pytest.fixture(autouse=True)
def setup_mocks():
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
    yield
    mock_frappe.reset_mock()


@pytest.fixture
def test_contextvar():
    """Expose the test ContextVar for assertions."""
    return _test_contextvar
