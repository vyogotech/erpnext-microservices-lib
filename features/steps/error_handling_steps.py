from behave import given, when, then
from unittest.mock import MagicMock, patch
import json
import frappe
from frappe_microservice.core import MicroserviceApp

@given('a microservice app is running')
def step_impl(context):
    context.app = MicroserviceApp("test-app")
    context.app.flask_app.testing = True
    context.client = context.app.flask_app.test_client()

@when('a request is made to an endpoint that raises "{exception_name}"')
def step_impl(context, exception_name):
    # Map exception name to actual Frappe exception
    exception_map = {
        "DoesNotExistError": frappe.DoesNotExistError,
        "PermissionError": frappe.PermissionError,
        "ValidationError": frappe.ValidationError
    }
    
    exception_class = exception_map.get(exception_name, Exception)
    
    # Register a temporary secure route that raises the exception
    @context.app.secure_route('/test-error', methods=['GET'])
    def error_route(user):
        raise exception_class("Test detail message")
    
    # Mock _validate_session to bypass auth for the test
    with patch.object(MicroserviceApp, '_validate_session', return_value=("test_user", None)):
        context.response = context.client.get('/test-error')

@then('the status code should be {status_code:d}')
def step_impl(context, status_code):
    assert context.response.status_code == status_code

@then('the response should contain message "{msg}"')
def step_impl(context, msg):
    data = json.loads(context.response.data)
    assert data['message'] == msg
