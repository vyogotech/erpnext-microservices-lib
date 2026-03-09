import pytest
from unittest.mock import MagicMock, patch
import sys

# Mock frappe BEFORE importing any modules that use it
mock_frappe = MagicMock()
mock_frappe.PermissionError = type('PermissionError', (Exception,), {'__module__': 'frappe'})
mock_frappe.DoesNotExistError = type('DoesNotExistError', (Exception,), {'__module__': 'frappe'})
mock_frappe.ValidationError = type('ValidationError', (Exception,), {'__module__': 'frappe'})
mock_frappe.AuthenticationError = type('AuthenticationError', (Exception,), {'__module__': 'frappe'})
mock_frappe.LinkValidationError = type('LinkValidationError', (Exception,), {'__module__': 'frappe'})
mock_frappe._dict = dict

# Add local to mock_frappe
mock_frappe.local = MagicMock()
mock_frappe.session = MagicMock()

# Ensure name 'frappe' is in sys.modules
sys.modules['frappe'] = mock_frappe
sys.modules['frappe.frappeclient'] = MagicMock()
sys.modules['frappe.modules'] = MagicMock()
sys.modules['frappe.modules.import_file'] = MagicMock()
sys.modules['frappe.model'] = MagicMock()
sys.modules['frappe.model.base_document'] = MagicMock()
sys.modules['frappe.model.document'] = MagicMock()

@pytest.fixture(autouse=True)
def setup_mocks():
    """Reset mocks between tests"""
    mock_frappe.reset_mock()
    # Mock specific methods that are used in core.py
    mock_frappe.init = MagicMock()
    mock_frappe.set_user = MagicMock()
    mock_frappe.get_all = MagicMock(return_value=[])
    mock_frappe.get_doc = MagicMock()
    mock_frappe.db = MagicMock()
    mock_frappe.cache = MagicMock()
    yield
    mock_frappe.reset_mock()
