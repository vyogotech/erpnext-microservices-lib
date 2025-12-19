from behave import given, when, then
from unittest.mock import MagicMock, patch
import frappe
from frappe_microservice.core import MicroserviceApp

@given('the current user belongs to "{tenant_id}"')
def step_impl(context, tenant_id):
    context.tenant_id = tenant_id
    # Directly mock the resolution on the db instance
    context.app.tenant_db.get_tenant_id = MagicMock(return_value=tenant_id)

@when('the app queries all "{doctype}"')
def step_impl(context, doctype):
    with patch('frappe.get_all', create=True) as mock_get_all:
        with context.app.flask_app.app_context():
            context.app.tenant_db.get_all(doctype)
        context.mock_get_all = mock_get_all

@then('the database filter should include "tenant_id" = "{tenant_id}"')
def step_impl(context, tenant_id):
    args, kwargs = context.mock_get_all.call_args
    filters = kwargs.get('filters', {})
    assert filters.get('tenant_id') == tenant_id

@when('the app attempts to get "{doctype}" named "{name}" belonging to "{owner_tenant}"')
def step_impl(context, doctype, name, owner_tenant):
    # Mock a document that belongs to owner_tenant
    mock_doc = MagicMock()
    mock_doc.doctype = doctype
    mock_doc.name = name
    mock_doc.tenant_id = owner_tenant
    
    with patch('frappe.get_doc', create=True, return_value=mock_doc):
        # Register a route to test the full secure_route wrapper
        @context.app.secure_route('/test-mt-get', methods=['GET'])
        def mt_get_route(user):
            return context.app.tenant_db.get_doc(doctype, name).as_dict()

        with patch.object(MicroserviceApp, '_validate_session', return_value=("test_user", None)):
            context.response = context.client.get('/test-mt-get')
            # If the call within the route raises PermissionError, 
            # secure_route catches it and returns 403

@then('a "{exception_name}" should be raised')
def step_impl(context, exception_name):
    # In secure_route, exceptions are caught and converted to JSON
    # So we check the status code or the JSON 'type'
    data = context.response.get_json()
    assert data['type'] == exception_name

@then('no "{exception_name}" should be raised')
def step_impl(context, exception_name):
    assert context.response.status_code == 200
