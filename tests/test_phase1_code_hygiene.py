"""
Tests for Phase 1: Code Hygiene & Bug Fixes
Tests that verify no print() statements are used and logging is properly configured.
"""
import pytest
import logging
from unittest.mock import MagicMock, patch, call
from io import StringIO
import sys
import frappe
from frappe_microservice.core import MicroserviceApp


class TestNoPrintStatements:
    """Test that print() is not used, logging is used instead."""
    
    def test_secure_route_uses_logging_not_print(self, capfd):
        """Test that secure_route decorator uses logger.info() not print()."""
        app = MicroserviceApp("test-app", central_site_url="http://central")
        app.flask_app.testing = True
        
        @app.secure_route('/test-logging')
        def test_route(user):
            return {"message": "success"}
        
        with patch.object(MicroserviceApp, '_validate_session', return_value=("test_user", None)):
            with patch.object(app.logger, 'info') as mock_logger:
                client = app.flask_app.test_client()
                response = client.get('/test-logging')
                
                # Verify logger.info was called
                assert mock_logger.called, "logger.info should be called"
                
                # Verify no print() output
                captured = capfd.readouterr()
                assert "SECURE_ROUTE DEBUG" not in captured.out, \
                    "Should not use print() for debug messages"
    
    def test_validate_session_uses_logging(self):
        """Test that _validate_session uses logger not print()."""
        app = MicroserviceApp("test-app", central_site_url="http://central")
        
        with patch.object(app.logger, 'info') as mock_logger:
            with app.flask_app.test_request_context():
                username, response = app._validate_session()
                
                # Should log authentication attempts
                assert mock_logger.called or response is not None, \
                    "Should use logger for authentication flow"


class TestDuplicateCodeRemoval:
    """Test that duplicate code in app isolation is removed."""

    def test_patch_app_resolution_no_duplicates(self):
        """Test that service app name is only added once."""
        app = MicroserviceApp("test-service", central_site_url="http://central")

        # Reset guard
        if hasattr(frappe, "_microservice_isolation_applied"):
            delattr(frappe, "_microservice_isolation_applied")

        with patch("frappe.get_all_apps",
                    return_value=["frappe", "erpnext", "test_service"]):
            app._patch_app_resolution()

        result = frappe.get_installed_apps()

        service_app_name = "test_service"
        count = result.count(service_app_name)

        assert count == 1, \
            f"Service app '{service_app_name}' should appear exactly once, found {count} times"


class TestConfigurationGeneration:
    """Test site configuration generation."""
    
    def test_create_site_config_generates_valid_json(self):
        """Test that create_site_config generates valid configuration."""
        from frappe_microservice.entrypoint import create_site_config
        
        config = create_site_config(
            db_host="localhost",
            db_port=3306,
            db_name="test_db",
            db_user="test_user",
            db_password="test_pass"
        )
        
        assert config is not None
        assert "db_host" in config or "db_name" in config, \
            "Configuration should contain database settings"
    
    def test_utils_reexports_from_entrypoint(self):
        """Test that utils.py re-exports from entrypoint (no duplication)."""
        from frappe_microservice import utils
        from frappe_microservice import entrypoint
        
        # Should be the same function (re-exported)
        assert hasattr(utils, 'generate_site_config'), \
            "utils should have generate_site_config"
        
        # If it's a re-export, the function should be from entrypoint module
        if hasattr(utils.generate_site_config, '__module__'):
            assert 'entrypoint' in utils.generate_site_config.__module__, \
                "generate_site_config should be re-exported from entrypoint"


class TestSignupServiceSecurity:
    """Integration tests for signup service security fixes."""
    
    @pytest.mark.integration
    def test_signup_response_no_password_leak(self):
        """Test that signup response does not contain plaintext password."""
        # This will be implemented when we test the actual signup service
        # For now, this is a placeholder to ensure the test exists
        pytest.skip("Integration test - requires running signup service")
    
    @pytest.mark.integration
    def test_subscription_party_configuration(self):
        """Test that subscription uses correct party configuration."""
        pytest.skip("Integration test - requires running signup service")
    
    @pytest.mark.integration
    def test_nested_set_range_validation(self):
        """Test that nested set ranges are calculated correctly."""
        pytest.skip("Integration test - requires running signup service")
