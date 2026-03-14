from unittest.mock import MagicMock, patch
import sys


class _dict(dict):
    """Minimal stand-in for frappe._dict (dict with attribute access)."""
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


# Mock frappe BEFORE importing any modules that use it
mock_frappe = MagicMock()
mock_frappe.PermissionError = type('PermissionError', (Exception,), {'__module__': 'frappe'})
mock_frappe.DoesNotExistError = type('DoesNotExistError', (Exception,), {'__module__': 'frappe'})
mock_frappe.ValidationError = type('ValidationError', (Exception,), {'__module__': 'frappe'})
mock_frappe.AuthenticationError = type('AuthenticationError', (Exception,), {'__module__': 'frappe'})
mock_frappe.LinkValidationError = type('LinkValidationError', (Exception,), {'__module__': 'frappe'})
mock_frappe._dict = _dict
mock_frappe.local = MagicMock()
mock_frappe.session = MagicMock()

# Ensure name 'frappe' and its submodules are in sys.modules
sys.modules['frappe'] = mock_frappe

# app.py imports from frappe.utils.local
mock_utils = MagicMock()
mock_utils_local = MagicMock()
sys.modules['frappe.utils'] = mock_utils
sys.modules['frappe.utils.local'] = mock_utils_local

# isolation.py lazy-imports from frappe.core.doctype; app.py patches frappe.core.doctype.version
mock_core = MagicMock()
sys.modules['frappe.core'] = mock_core
sys.modules['frappe.core.doctype'] = MagicMock()
sys.modules['frappe.core.doctype.version'] = MagicMock()
sys.modules['frappe.core.doctype.version.version'] = MagicMock()

# central.py imports FrappeClient from frappe.frappeclient
mock_frappeclient = MagicMock()
mock_frappeclient.FrappeClient = MagicMock()
sys.modules['frappe.frappeclient'] = mock_frappeclient

def before_scenario(context, scenario):
    """Reset mocks before each scenario"""
    mock_frappe.reset_mock()
    # Mock specific methods that are used in core.py
    mock_frappe.init = MagicMock()
    mock_frappe.set_user = MagicMock()
    mock_frappe.get_all = MagicMock(return_value=[])
    mock_frappe.get_doc = MagicMock()
    mock_frappe.db = MagicMock()
    mock_frappe.cache = MagicMock()
    
    # Provide a patcher helper for steps
    context.patch_create = lambda target, **kwargs: patch(target, create=True, **kwargs)

def after_scenario(context, scenario):
    if hasattr(context, 'patcher'):
        context.patcher.stop()
