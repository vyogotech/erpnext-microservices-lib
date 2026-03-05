import pytest
import json
from unittest.mock import MagicMock, patch
import frappe
from frappe_microservice.core import MicroserviceApp

def test_validate_session_401():
    app = MicroserviceApp("test-app", central_site_url="http://central")
    app.flask_app.testing = True
    
    with app.flask_app.test_request_context():
        username, response = app._validate_session()
        assert username is None
        assert response[1] == 401
        
        data = json.loads(response[0].data)
        assert data['status'] == 'error'
        assert "Authentication required" in data['message']
        assert data['code'] == 401

@patch("frappe.set_user", create=True)
def test_secure_route_403(mock_set_user):
    app = MicroserviceApp("test-app")
    app.flask_app.testing = True
    client = app.flask_app.test_client()
    
    @app.secure_route('/test-403')
    def test_route(user):
        raise frappe.PermissionError("Custom forbidden message")
        
    with patch.object(MicroserviceApp, '_validate_session', return_value=("test_user", None)):
        response = client.get('/test-403')
        assert response.status_code == 403
        data = json.loads(response.data)
        assert data['status'] == 'error'
        assert data['message'] == "Custom forbidden message"
        assert data['code'] == 403

def test_secure_route_404():
    app = MicroserviceApp("test-app")
    app.flask_app.testing = True
    client = app.flask_app.test_client()
    
    @app.secure_route('/test-404')
    def test_route(user):
        raise frappe.DoesNotExistError("Sales Order SO-001 not found")
        
    with patch.object(MicroserviceApp, '_validate_session', return_value=("test_user", None)):
        response = client.get('/test-404')
        assert response.status_code == 404
        data = json.loads(response.data)
        assert data['status'] == 'error'
        assert data['message'] == "Sales Order SO-001 not found"
        assert data['code'] == 404

def test_isolate_microservice_apps_idempotency():
    """Verify that _isolate_microservice_apps doesn't wrap functions multiple times"""
    # Create fresh mocks for this test
    with patch("frappe.get_attr", create=True) as mock_get_attr, \
         patch("frappe.get_installed_apps", create=True) as mock_get_apps, \
         patch("frappe.get_doc_hooks", create=True) as mock_get_hooks, \
         patch("frappe.logger", create=True), \
         patch("frappe.cache", create=True):
        
        # Reset the global guard if set from previous tests
        if hasattr(frappe, "_microservice_isolation_applied"):
            delattr(frappe, "_microservice_isolation_applied")
            
        app = MicroserviceApp("test-app")
        
        # First call apply patches
        app._isolate_microservice_apps()
        patched_get_attr = frappe.get_attr
        
        # Second call should be a no-op due to guard
        app._isolate_microservice_apps()
        
        assert frappe.get_attr == patched_get_attr, "Function was re-wrapped despite guard!"
        assert getattr(frappe, "_microservice_isolation_applied") is True
