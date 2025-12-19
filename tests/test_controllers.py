import pytest
import frappe
from unittest.mock import MagicMock
from frappe_microservice.controller import DocumentController, ControllerRegistry

class TestDocumentController:
    def test_attr_sync(self):
        # Mock frappe document
        doc = MagicMock()
        doc.doctype = "Sales Order"
        doc.customer = "CUST-001"
        
        # Initialize controller
        class SalesOrderController(DocumentController):
            pass
            
        controller = SalesOrderController(doc)
        
        # Check getattr
        assert controller.customer == "CUST-001"
        
        # Check setattr sync
        controller.customer = "CUST-002"
        assert doc.customer == "CUST-002"
        assert controller.customer == "CUST-002"

    def test_lifecycle_methods(self):
        doc = MagicMock()
        doc.doctype = "Sales Order"
        
        class SalesOrderController(DocumentController):
            def validate(self):
                self.validated = True
                
        controller = SalesOrderController(doc)
        controller.validate()
        assert controller.validated is True


class TestControllerRegistry:
    def test_register_and_get(self):
        registry = ControllerRegistry()
        
        class TestController(DocumentController):
            pass
            
        registry.register("Test Doc", TestController)
        assert registry.get_controller("Test Doc") == TestController
        assert registry.has_controller("Test Doc") is True
        assert registry.has_controller("Non Existent") is False

    def test_create_instance(self):
        registry = ControllerRegistry()
        class TestController(DocumentController):
            pass
        registry.register("Test Doc", TestController)
        
        doc = MagicMock()
        doc.doctype = "Test Doc"
        
        instance = registry.create_controller_instance(doc)
        assert isinstance(instance, TestController)
        assert instance.doc == doc
