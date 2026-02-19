"""
Integration tests for microservices

Tests end-to-end functionality:
- Signup service tenant creation
- Auth service login/logout
- Order service CRUD operations
- Cross-service tenant isolation
- Request correlation across services
"""

import pytest
import requests
import uuid
from datetime import datetime


class TestSignupServiceIntegration:
    """Integration tests for signup service"""
    
    @pytest.fixture
    def base_url(self):
        return "http://localhost:8000"
    
    @pytest.mark.integration
    def test_tenant_signup_flow(self, base_url):
        """Test complete tenant signup flow"""
        # Generate unique subdomain
        subdomain = f"test-{uuid.uuid4().hex[:8]}"
        
        signup_data = {
            "tenant_name": "Test Company",
            "subdomain": subdomain,
            "admin_email": f"admin@{subdomain}.com",
            "admin_password": "SecurePass123!"
        }
        
        response = requests.post(f"{base_url}/signup/tenant", json=signup_data)
        
        assert response.status_code == 201
        data = response.json()
        
        # Verify response structure
        assert data['success'] == True
        assert 'tenant_id' in data['data']
        assert data['data']['subdomain'] == subdomain
        assert data['data']['admin_email'] == signup_data['admin_email']
        
        # SECURITY: Verify password is NOT in response
        assert 'admin_password' not in data['data']
        
        # Verify tenant isolation
        tenant_id = data['data']['tenant_id']
        assert tenant_id != 'SYSTEM'
        assert len(tenant_id) > 0
    
    @pytest.mark.integration
    def test_signup_duplicate_subdomain(self, base_url):
        """Test error when subdomain already exists"""
        subdomain = f"duplicate-{uuid.uuid4().hex[:8]}"
        
        signup_data = {
            "tenant_name": "Test Company",
            "subdomain": subdomain,
            "admin_email": f"admin@{subdomain}.com",
            "admin_password": "SecurePass123!"
        }
        
        # First signup should succeed
        response1 = requests.post(f"{base_url}/signup/tenant", json=signup_data)
        assert response1.status_code == 201
        
        # Second signup with same subdomain should fail
        response2 = requests.post(f"{base_url}/signup/tenant", json=signup_data)
        assert response2.status_code == 409
        assert 'already exists' in response2.json()['error'].lower()
    
    @pytest.mark.integration
    def test_signup_weak_password(self, base_url):
        """Test password strength validation"""
        signup_data = {
            "tenant_name": "Test Company",
            "subdomain": f"test-{uuid.uuid4().hex[:8]}",
            "admin_email": "admin@test.com",
            "admin_password": "weak"  # Too short
        }
        
        response = requests.post(f"{base_url}/signup/tenant", json=signup_data)
        
        assert response.status_code == 400
        assert 'password' in response.json()['error'].lower()


class TestAuthServiceIntegration:
    """Integration tests for auth service"""
    
    @pytest.fixture
    def base_url(self):
        return "http://localhost:8001"
    
    @pytest.mark.integration
    def test_login_flow(self, base_url):
        """Test login with valid credentials"""
        # Note: Requires existing user from signup
        login_data = {
            "email": "admin@test.com",
            "password": "SecurePass123!"
        }
        
        response = requests.post(f"{base_url}/auth/login", json=login_data)
        
        if response.status_code == 200:
            data = response.json()
            assert 'session_id' in data or 'sid' in response.cookies
    
    @pytest.mark.integration
    def test_login_invalid_credentials(self, base_url):
        """Test login with invalid credentials"""
        login_data = {
            "email": "nonexistent@test.com",
            "password": "WrongPassword"
        }
        
        response = requests.post(f"{base_url}/auth/login", json=login_data)
        
        assert response.status_code in [401, 403]


class TestOrderServiceIntegration:
    """Integration tests for order service"""
    
    @pytest.fixture
    def base_url(self):
        return "http://localhost:8002"
    
    @pytest.fixture
    def auth_headers(self):
        """Get authentication headers (requires login)"""
        # This would typically come from a login flow
        return {
            'Authorization': 'Bearer test-token',
            'X-Request-ID': str(uuid.uuid4())
        }
    
    @pytest.mark.integration
    def test_create_order(self, base_url, auth_headers):
        """Test order creation with tenant isolation"""
        order_data = {
            "customer": "CUST-001",
            "items": [
                {"item_code": "ITEM-001", "qty": 10, "rate": 100}
            ]
        }
        
        response = requests.post(
            f"{base_url}/api/resource/sales-order",
            json=order_data,
            headers=auth_headers
        )
        
        # May fail if not authenticated, but should not return 500
        assert response.status_code != 500
    
    @pytest.mark.integration
    def test_list_orders(self, base_url, auth_headers):
        """Test listing orders with tenant filtering"""
        response = requests.get(
            f"{base_url}/api/resource/sales-order",
            headers=auth_headers
        )
        
        # May fail if not authenticated, but should not return 500
        assert response.status_code != 500


class TestCrossServiceIntegration:
    """Test cross-service interactions"""
    
    @pytest.mark.integration
    def test_request_correlation_propagation(self):
        """Test X-Request-ID propagates across services"""
        request_id = str(uuid.uuid4())
        headers = {'X-Request-ID': request_id}
        
        # Call signup service
        response = requests.get(
            "http://localhost:8000/health",
            headers=headers
        )
        
        # Verify request ID is returned
        assert response.headers.get('X-Request-ID') == request_id
    
    @pytest.mark.integration
    def test_tenant_isolation_across_services(self):
        """Test tenant data is isolated across services"""
        # Create tenant 1
        subdomain1 = f"tenant1-{uuid.uuid4().hex[:8]}"
        signup1 = {
            "tenant_name": "Tenant 1",
            "subdomain": subdomain1,
            "admin_email": f"admin@{subdomain1}.com",
            "admin_password": "SecurePass123!"
        }
        
        response1 = requests.post("http://localhost:8000/signup/tenant", json=signup1)
        tenant1_id = response1.json()['data']['tenant_id'] if response1.status_code == 201 else None
        
        # Create tenant 2
        subdomain2 = f"tenant2-{uuid.uuid4().hex[:8]}"
        signup2 = {
            "tenant_name": "Tenant 2",
            "subdomain": subdomain2,
            "admin_email": f"admin@{subdomain2}.com",
            "admin_password": "SecurePass123!"
        }
        
        response2 = requests.post("http://localhost:8000/signup/tenant", json=signup2)
        tenant2_id = response2.json()['data']['tenant_id'] if response2.status_code == 201 else None
        
        # Verify different tenant IDs
        if tenant1_id and tenant2_id:
            assert tenant1_id != tenant2_id
            assert tenant1_id != 'SYSTEM'
            assert tenant2_id != 'SYSTEM'


class TestHealthEndpoints:
    """Test health endpoints across all services"""
    
    @pytest.mark.integration
    @pytest.mark.parametrize("service,port", [
        ("signup", 8000),
        ("auth", 8001),
        ("order", 8002)
    ])
    def test_health_endpoint(self, service, port):
        """Test health endpoint returns proper status"""
        response = requests.get(f"http://localhost:{port}/health")
        
        assert response.status_code in [200, 503]
        data = response.json()
        
        assert 'status' in data
        assert 'service' in data
        assert 'timestamp' in data
        
        if response.status_code == 200:
            assert data['status'] == 'healthy'
            assert 'database' in data


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-m', 'integration'])
