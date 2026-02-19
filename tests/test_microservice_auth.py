"""
Comprehensive unit tests for MicroserviceApp authentication

Tests cover:
- Session validation (OAuth2 and cookie-based)
- secure_route decorator (all branches)
- Error handling in authentication
- Request correlation IDs
- Rollback tracking
- Permission errors
- Validation errors
- Not found errors
- Generic errors
"""

import pytest
from unittest.mock import MagicMock, patch, Mock
import frappe
from flask import Flask, g
from frappe_microservice.core import MicroserviceApp
import uuid


class TestMicroserviceAppAuth:
    """Test authentication mechanisms"""
    
    @pytest.fixture
    def app(self):
        """Create test microservice app"""
        with patch('frappe.init'):
            with patch('frappe.connect'):
                app = MicroserviceApp("test-service", central_site_url="http://central")
                return app
    
    @patch('requests.get')
    def test_validate_oauth_token_success(self, mock_get, app):
        """Test successful OAuth2 token validation"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'message': {
                'email': 'user@example.com',
                'sub': 'user-123'
            }
        }
        mock_get.return_value = mock_response
        
        username, error = app._validate_oauth_token('valid-token')
        
        assert username == 'user@example.com'
        assert error is None
        mock_get.assert_called_once()
    
    @patch('requests.get')
    def test_validate_oauth_token_invalid(self, mock_get, app):
        """Test invalid OAuth2 token"""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response
        
        username, error = app._validate_oauth_token('invalid-token')
        
        assert username is None
        assert error is not None
        assert error[1] == 401  # HTTP status code
    
    @patch('requests.get')
    def test_validate_oauth_token_exception(self, mock_get, app):
        """Test OAuth2 validation with network error"""
        mock_get.side_effect = Exception("Network error")
        
        username, error = app._validate_oauth_token('token')
        
        assert username is None
        assert error is not None
        assert error[1] == 401
    
    @patch('requests.get')
    def test_validate_session_with_cookie(self, mock_get, app):
        """Test session validation with SID cookie"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'message': 'user@example.com'}
        mock_get.return_value = mock_response
        
        with app.flask_app.test_request_context(headers={'Cookie': 'sid=valid-sid'}):
            username, error = app._validate_session()
            
            assert username == 'user@example.com'
            assert error is None
    
    def test_validate_session_no_auth(self, app):
        """Test session validation with no authentication"""
        with app.flask_app.test_request_context():
            username, error = app._validate_session()
            
            assert username is None
            assert error is not None
            assert error[1] == 401


class TestSecureRouteDecorator:
    """Test secure_route decorator - all branches"""
    
    @pytest.fixture
    def app(self):
        """Create test microservice app"""
        with patch('frappe.init'):
            with patch('frappe.connect'):
                app = MicroserviceApp("test-service", central_site_url="http://central")
                return app
    
    def test_secure_route_success(self, app):
        """Test successful authenticated request"""
        @app.secure_route('/test', methods=['GET'])
        def test_endpoint(user):
            return {'user': user, 'message': 'success'}
        
        with app.flask_app.test_request_context('/test'):
            # Mock successful authentication
            with patch.object(app, '_validate_session', return_value=('user@example.com', None)):
                with patch('frappe_microservice.core.get_user_tenant_id', return_value='tenant-123'):
                    g.request_id = str(uuid.uuid4())
                    response = test_endpoint()

                    assert response.json['user'] == 'user@example.com'
                    assert response.json['message'] == 'success'
    
    def test_secure_route_auth_failure(self, app):
        """Test request with authentication failure"""
        @app.secure_route('/test', methods=['GET'])
        def test_endpoint(user):
            return {'user': user}
        
        with app.flask_app.test_request_context('/test'):
            # Mock failed authentication
            error_response = ({'error': 'Unauthorized'}, 401)
            with patch.object(app, '_validate_session', return_value=(None, error_response)):
                g.request_id = str(uuid.uuid4())
                response = test_endpoint()
                
                assert response == error_response
    
    def test_secure_route_permission_error(self, app):
        """Test PermissionError handling"""
        @app.secure_route('/test', methods=['GET'])
        def test_endpoint(user):
            raise frappe.PermissionError("Access denied")
        
        with app.flask_app.test_request_context('/test'):
            with patch.object(app, '_validate_session', return_value=('user@example.com', None)):
                with patch('frappe_microservice.core.get_user_tenant_id', return_value='tenant-123'):
                    with patch('frappe.db.rollback'):
                        g.request_id = str(uuid.uuid4())
                        g._frappe_rolled_back = False
                        
                        response, status_code = test_endpoint()
                        
                        assert status_code == 403
                        assert response.json['status'] == 'error'
                        assert 'request_id' in response.json
                        assert g._frappe_rolled_back == True
    
    def test_secure_route_not_found_error(self, app):
        """Test DoesNotExistError handling"""
        @app.secure_route('/test', methods=['GET'])
        def test_endpoint(user):
            raise frappe.DoesNotExistError("Document not found")
        
        with app.flask_app.test_request_context('/test'):
            with patch.object(app, '_validate_session', return_value=('user@example.com', None)):
                with patch('frappe_microservice.core.get_user_tenant_id', return_value='tenant-123'):
                    with patch('frappe.db.rollback'):
                        g.request_id = str(uuid.uuid4())
                        g._frappe_rolled_back = False
                        
                        response, status_code = test_endpoint()
                        
                        assert status_code == 404
                        assert response.json['status'] == 'error'
                        assert 'request_id' in response.json
                        assert g._frappe_rolled_back == True
    
    def test_secure_route_validation_error(self, app):
        """Test ValidationError handling"""
        @app.secure_route('/test', methods=['GET'])
        def test_endpoint(user):
            raise frappe.ValidationError("Invalid data")
        
        with app.flask_app.test_request_context('/test'):
            with patch.object(app, '_validate_session', return_value=('user@example.com', None)):
                with patch('frappe_microservice.core.get_user_tenant_id', return_value='tenant-123'):
                    with patch('frappe.db.rollback'):
                        g.request_id = str(uuid.uuid4())
                        g._frappe_rolled_back = False
                        
                        response, status_code = test_endpoint()
                        
                        assert status_code == 400
                        assert response.json['status'] == 'error'
                        assert 'request_id' in response.json
                        assert g._frappe_rolled_back == True
    
    def test_secure_route_generic_error(self, app):
        """Test generic Exception handling"""
        @app.secure_route('/test', methods=['GET'])
        def test_endpoint(user):
            raise Exception("Unexpected error")
        
        with app.flask_app.test_request_context('/test'):
            with patch.object(app, '_validate_session', return_value=('user@example.com', None)):
                with patch('frappe_microservice.core.get_user_tenant_id', return_value='tenant-123'):
                    with patch('frappe.db.rollback'):
                        g.request_id = str(uuid.uuid4())
                        g._frappe_rolled_back = False
                        
                        response, status_code = test_endpoint()
                        
                        assert status_code == 500
                        assert response.json['status'] == 'error'
                        assert 'request_id' in response.json
                        assert g._frappe_rolled_back == True
    
    def test_secure_route_dict_response_auto_jsonify(self, app):
        """Test automatic JSON conversion for dict responses"""
        @app.secure_route('/test', methods=['GET'])
        def test_endpoint(user):
            return {'data': 'test'}
        
        with app.flask_app.test_request_context('/test'):
            with patch.object(app, '_validate_session', return_value=('user@example.com', None)):
                with patch('frappe_microservice.core.get_user_tenant_id', return_value='tenant-123'):
                    g.request_id = str(uuid.uuid4())
                    response = test_endpoint()
                    
                    # Should be auto-converted to JSON response
                    assert response.json == {'data': 'test'}


class TestRequestCorrelation:
    """Test request correlation ID propagation"""
    
    @pytest.fixture
    def app(self):
        """Create test microservice app"""
        with patch('frappe.init'):
            with patch('frappe.connect'):
                app = MicroserviceApp("test-service", central_site_url="http://central")
                return app
    
    def test_request_id_generated(self, app):
        """Test request ID is generated if not provided"""
        with app.flask_app.test_request_context('/health'):
            # Trigger before_request
            app.flask_app.preprocess_request()
            
            assert hasattr(g, 'request_id')
            assert len(g.request_id) > 0
    
    def test_request_id_propagated(self, app):
        """Test request ID is propagated from header"""
        request_id = str(uuid.uuid4())
        
        with app.flask_app.test_request_context('/health', headers={'X-Request-ID': request_id}):
            # Trigger before_request
            app.flask_app.preprocess_request()
            
            assert g.request_id == request_id


class TestRollbackTracking:
    """Test rollback tracking to prevent double-commit"""
    
    @pytest.fixture
    def app(self):
        """Create test microservice app"""
        with patch('frappe.init'):
            with patch('frappe.connect'):
                app = MicroserviceApp("test-service", central_site_url="http://central")
                return app
    
    def test_rollback_flag_set_on_error(self, app):
        """Test rollback flag is set when error occurs"""
        @app.secure_route('/test', methods=['GET'])
        def test_endpoint(user):
            raise Exception("Error")
        
        with app.flask_app.test_request_context('/test'):
            with patch.object(app, '_validate_session', return_value=('user@example.com', None)):
                with patch('frappe_microservice.core.get_user_tenant_id', return_value='tenant-123'):
                    with patch('frappe.db.rollback'):
                        g._frappe_rolled_back = False
                        
                        test_endpoint()
                        
                        assert g._frappe_rolled_back == True
    
    @patch('frappe.db.commit')
    def test_commit_skipped_after_rollback(self, mock_commit, app):
        """Test commit is skipped if rollback occurred"""
        with app.flask_app.test_request_context('/health'):
            g._frappe_rolled_back = True
            
            # Trigger after_request
            response = app.flask_app.make_response(('OK', 200))
            app.flask_app.process_response(response)
            
            # Commit should not be called
            mock_commit.assert_not_called()


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--cov=frappe_microservice.core', '--cov-report=html'])
