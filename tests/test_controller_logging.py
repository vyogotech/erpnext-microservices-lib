"""
Tests for controller registry logging.
Verifies that controller.py uses logging instead of print statements.
"""
import pytest
import logging
from unittest.mock import MagicMock, patch
from frappe_microservice.controller import ControllerRegistry


class TestControllerLogging:
    """Test that ControllerRegistry uses logging instead of print()."""
    
    def test_register_controller_uses_logging(self, capfd):
        """Test that register_controller uses logger not print()."""
        registry = ControllerRegistry()
        
        # Mock a controller class
        mock_controller = MagicMock()
        mock_controller.__name__ = "TestController"
        
        with patch('frappe_microservice.controller.logging.getLogger') as mock_get_logger:
            mock_logger = MagicMock()
            mock_get_logger.return_value = mock_logger
            
            registry.register_controller("Test", mock_controller)
            
            # Verify no print() output with emoji
            captured = capfd.readouterr()
            assert "✅" not in captured.out, \
                "Should not use print() with emoji, use logger instead"
    
    def test_add_controller_path_uses_logging(self, capfd):
        """Test that add_controller_path uses logger not print()."""
        registry = ControllerRegistry()
        
        with patch('os.path.exists', return_value=True):
            registry.add_controller_path("/fake/path")
            
            # Verify no print() output
            captured = capfd.readouterr()
            assert "✅" not in captured.out, \
                "Should not use print() for path registration"
    
    def test_discover_controllers_uses_logging(self, capfd):
        """Test that discover_controllers uses logger not print()."""
        registry = ControllerRegistry()
        
        with patch('os.path.exists', return_value=False):
            registry.discover_controllers("/nonexistent/path")
            
            # Verify no print() output with warning emoji
            captured = capfd.readouterr()
            assert "⚠️" not in captured.out and "❌" not in captured.out, \
                "Should use logger.warning/error instead of print()"
    
    def test_setup_controllers_uses_logging(self, capfd):
        """Test that setup_controllers uses logger not print()."""
        registry = ControllerRegistry()
        mock_app = MagicMock()
        
        registry.setup_controllers(mock_app)
        
        # Verify no print() output
        captured = capfd.readouterr()
        assert "✅" not in captured.out, \
            "Should use logger.info for setup completion"
