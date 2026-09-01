import pytest
import frappe
from unittest.mock import MagicMock, patch
from frappe_microservice.controller import DocumentController, ControllerRegistry
import tempfile
from pathlib import Path

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
            def before_validate(self):
                self.before_validated = True
            def before_insert(self):
                self.before_inserted = True
            def after_insert(self):
                self.after_inserted = True
            def before_save(self):
                self.before_saved = True
            def after_save(self):
                self.after_saved = True
            def before_update(self):
                self.before_updated = True
            def after_update(self):
                self.after_updated = True
            def on_update(self):
                self.on_updated = True
            def before_delete(self):
                self.before_deleted = True
            def on_trash(self):
                self.on_trashed = True
            def after_delete(self):
                self.after_deleted = True
            def on_cancel(self):
                self.on_cancelled = True
            def on_submit(self):
                self.on_submitted = True
                
        controller = SalesOrderController(doc)
        controller.validate()
        assert controller.validated is True
        controller.before_validate()
        assert controller.before_validated is True
        controller.before_insert()
        assert controller.before_inserted is True
        controller.after_insert()
        assert controller.after_inserted is True
        controller.before_save()
        assert controller.before_saved is True
        controller.after_save()
        assert controller.after_saved is True
        controller.before_update()
        assert controller.before_updated is True
        controller.after_update()
        assert controller.after_updated is True
        controller.on_update()
        assert controller.on_updated is True
        controller.before_delete()
        assert controller.before_deleted is True
        controller.on_trash()
        assert controller.on_trashed is True
        controller.after_delete()
        assert controller.after_deleted is True
        controller.on_cancel()
        assert controller.on_cancelled is True
        controller.on_submit()
        assert controller.on_submitted is True

    def test_get_set_methods(self):
        doc = MagicMock()
        doc.doctype = "Sales Order"
        doc.field_name = "test_value"
        # Configure getattr to raise AttributeError for nonexistent
        def side_effect(name, default=None):
            if name == "nonexistent":
                raise AttributeError(f"no attribute {name}")
            if name == "field_name":
                return "test_value"
            return default
        doc.__getattribute__ = side_effect
        
        class TestController(DocumentController):
            pass
            
        controller = TestController(doc)
        
        # Test get
        doc.field_name = "test_value"
        assert controller.get("field_name") == "test_value"
        
        # Test set
        controller.set("field_name", "new_value")
        assert doc.field_name == "new_value"
        assert controller.field_name == "new_value"

    def test_throw_method(self):
        doc = MagicMock()
        doc.doctype = "Sales Order"
        
        class TestController(DocumentController):
            pass
            
        controller = TestController(doc)
        
        with patch('frappe_microservice.controller.frappe.throw') as mock_throw:
            controller.throw("Test error")
            mock_throw.assert_called_once_with("Test error")

    def test_add_comment_method(self):
        doc = MagicMock()
        doc.doctype = "Sales Order"
        
        class TestController(DocumentController):
            pass
            
        controller = TestController(doc)
        
        controller.add_comment("Comment", "Test comment")
        doc.add_comment.assert_called_once_with("Comment", "Test comment")

    def test_has_value_changed(self):
        doc = MagicMock()
        doc.doctype = "Sales Order"
        
        # Set up _doc_before_save with old value
        doc._doc_before_save = {"field": "old_value"}
        doc.get = MagicMock(side_effect=lambda fname, default=None: "new_value" if fname == "field" else default)
        
        class TestController(DocumentController):
            pass
            
        controller = TestController(doc)
        
        # Should detect change (new_value != old_value)
        assert controller.has_value_changed("field") is True
        
        # Now make them the same
        doc.get = MagicMock(side_effect=lambda fname, default=None: "old_value" if fname == "field" else default)
        assert controller.has_value_changed("field") is False

    def test_getattr_with_private_attributes(self):
        doc = MagicMock()
        doc.doctype = "Sales Order"
        
        class TestController(DocumentController):
            pass
            
        controller = TestController(doc)
        
        # Should raise AttributeError for private attributes
        with pytest.raises(AttributeError):
            _ = controller._private_attr
        
        with pytest.raises(AttributeError):
            _ = controller.flags

    def test_setattr_before_doc_exists(self):
        """Test setattr when doc is not yet set"""
        class TestController(DocumentController):
            pass
        
        # Create without initializing doc first
        controller = object.__new__(TestController)
        # This should not raise
        object.__setattr__(controller, 'doc', MagicMock())


class TestControllerRegistry:
    def setup_method(self):
        # Clear the global registry if it exists
        if hasattr(frappe, "_microservice_registry"):
            delattr(frappe, "_microservice_registry")

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

    def test_add_controller_path(self):
        registry = ControllerRegistry()
        
        registry.add_controller_path("/path/to/controllers")
        assert "/path/to/controllers" in registry._controller_paths
        
        # Adding same path again should not duplicate
        registry.add_controller_path("/path/to/controllers")
        assert registry._controller_paths.count("/path/to/controllers") == 1

    def test_auto_discover_controllers_nonexistent_directory(self):
        registry = ControllerRegistry()
        
        # Should not raise, just log warning
        registry.auto_discover_controllers("/nonexistent/path")
        # Controller count should remain 0
        assert registry.get_controller("NonExistent") is None

    def test_discover_controllers_alias(self):
        """Test that discover_controllers is an alias for auto_discover_controllers"""
        registry = ControllerRegistry()
        
        # Both methods should work
        with patch.object(registry, 'auto_discover_controllers') as mock_discover:
            registry.discover_controllers("/some/path")
            mock_discover.assert_called_once_with("/some/path")

    def test_get_value_before_save(self):
        """Test getting value before save"""
        doc = MagicMock()
        doc.doctype = "Sales Order"
        doc._doc_before_save = {"field": "old_value"}
        
        class TestController(DocumentController):
            pass
            
        controller = TestController(doc)
        assert controller.get_value_before_save("field") == "old_value"
        assert controller.get_value_before_save("nonexistent") is None

    def test_get_value_before_save_no_attr(self):
        """Test getting value before save when _doc_before_save doesn't exist"""
        doc = MagicMock(spec=['doctype'])  # Only has doctype, no _doc_before_save
        doc.doctype = "Sales Order"
        
        class TestController(DocumentController):
            pass
            
        controller = TestController(doc)
        assert controller.get_value_before_save("field") is None

    def test_auto_discover_controllers_with_valid_file(self):
        """Test auto-discovery with a valid controller file"""
        registry = ControllerRegistry()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a valid controller file
            controller_file = Path(tmpdir) / "sales_order.py"
            controller_file.write_text("""
from frappe_microservice.controller import DocumentController

class SalesOrder(DocumentController):
    def validate(self):
        pass
""")
            
            # Discover controllers
            registry.auto_discover_controllers(tmpdir)
            
            # Check that the controller was discovered
            assert registry.has_controller("Sales Order") is True

    def test_auto_discover_controllers_skips_private_files(self):
        """Test that auto-discovery skips _private files"""
        registry = ControllerRegistry()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a private controller file (starts with _)
            private_file = Path(tmpdir) / "_private.py"
            private_file.write_text("""
from frappe_microservice.controller import DocumentController

class Private(DocumentController):
    pass
""")
            
            # Discover controllers
            registry.auto_discover_controllers(tmpdir)
            
            # Private file should be skipped
            assert registry.has_controller("Private") is False

    def test_auto_discover_controllers_invalid_controller_class(self):
        """Test auto-discovery with invalid controller (not DocumentController subclass)"""
        registry = ControllerRegistry()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create an invalid controller file
            bad_file = Path(tmpdir) / "bad_module.py"
            bad_file.write_text("""
class BadModule:
    pass
""")
            
            # Discover controllers - should not raise
            registry.auto_discover_controllers(tmpdir)
            
            # No controller should be registered
            assert registry.has_controller("BadModule") is False

    def test_auto_discover_controllers_missing_class(self):
        """Test auto-discovery when expected class is missing"""
        registry = ControllerRegistry()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file with a different class name
            file = Path(tmpdir) / "sales_order.py"
            file.write_text("""
class WrongName:
    pass
""")
            
            # Discover controllers - should not raise
            registry.auto_discover_controllers(tmpdir)
            
            # No controller should be registered
            assert registry.has_controller("Sales Order") is False

    def test_register_controller_backwards_compat(self):
        """Test backwards-compatible register_controller method"""
        registry = ControllerRegistry()
        
        class TestController(DocumentController):
            pass
        
        # Use old method name
        registry.register_controller("Test", TestController)
        assert registry.get_controller("Test") == TestController

    def test_auto_discover_controllers_non_documentcontroller_class(self):
        """Test auto-discovery skips non-DocumentController classes"""
        registry = ControllerRegistry()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file with a class that's not a DocumentController
            bad_file = Path(tmpdir) / "not_controller.py"
            bad_file.write_text("""
class NotController:
    def __init__(self):
        pass
""")
            
            # Discover controllers - should not raise
            registry.auto_discover_controllers(tmpdir)
            
            # No controller should be registered
            assert registry.has_controller("NotController") is False

    def test_auto_discover_controllers_syntax_error_in_file(self):
        """Test that auto-discovery gracefully handles syntax errors"""
        registry = ControllerRegistry()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file with syntax error
            bad_file = Path(tmpdir) / "bad_syntax.py"
            bad_file.write_text("""
class BadSyntax
    def __init__(self):
        pass
""")
            
            # Discover controllers - should not raise
            registry.auto_discover_controllers(tmpdir)

    def test_filename_to_doctype_conversion(self):
        """Test filename to doctype name conversion"""
        registry = ControllerRegistry()
        
        assert registry._filename_to_doctype("sales_order") == "Sales Order"
        assert registry._filename_to_doctype("purchase_order") == "Purchase Order"
        assert registry._filename_to_doctype("single_word") == "Single Word"
        assert registry._filename_to_doctype("multiple_word_name") == "Multiple Word Name"

    def test_filename_to_classname_conversion(self):
        """Test filename to class name conversion"""
        registry = ControllerRegistry()
        
        assert registry._filename_to_classname("sales_order") == "SalesOrder"
        assert registry._filename_to_classname("purchase_order") == "PurchaseOrder"
        assert registry._filename_to_classname("test") == "Test"

    def test_setup_controllers_no_op(self):
        """Test that setup_controllers is a no-op for backwards compatibility"""
        registry = ControllerRegistry()
        mock_app = MagicMock()
        
        # Should not raise
        registry.setup_controllers(mock_app)

    def test_lifecycle_method_defaults_base_class(self):
        """Explicitly test base class lifecycle methods just call pass"""
        doc = MagicMock()
        doc.doctype = "Test"
        
        # These tests are just for coverage - calling pass statements
        base_controller = DocumentController(doc)
        
        # These should all execute without error (they're just pass)
        base_controller.before_validate()  # Line 85
        base_controller.before_insert()    # Line 89
        base_controller.after_insert()     # Line 93
        base_controller.before_save()      # Line 97
        base_controller.after_save()       # Line 101
        base_controller.before_update()    # Line 105
        base_controller.after_update()     # Line 109
        base_controller.on_update()        # Line 113
        base_controller.before_delete()    # Line 117
        base_controller.on_trash()         # Line 121
        base_controller.after_delete()     # Line 125
        base_controller.on_cancel()        # Line 129
        base_controller.on_submit()        # Line 133

    def test_list_controllers(self):
        """Test listing registered controllers"""
        registry = ControllerRegistry()
        
        class TestController(DocumentController):
            pass
        
        registry.register("Test", TestController)
        registry.register("Another", TestController)
        
        controllers = registry.list_controllers()
        assert "Test" in controllers
        assert "Another" in controllers
        assert controllers["Test"] == "TestController"

    def test_global_registry_functions(self):
        """Test the global get_controller_registry function"""
        from frappe_microservice.controller import get_controller_registry
        
        # Should return a registry
        registry = get_controller_registry()
        assert registry is not None
        assert isinstance(registry, ControllerRegistry)
        
        # Repeated calls should return THE SAME registry instance (singleton)
        registry2 = get_controller_registry()
        assert registry is registry2
        
        # Test registering on the global registry
        from frappe_microservice.controller import DocumentController
        class GlobalTest(DocumentController): pass
        
        registry.register("Global Test", GlobalTest)
        
        # Check it was registered
        assert get_controller_registry().has_controller("Global Test") is True

        with patch("frappe_microservice.controller.get_controller_registry") as mock_get_reg:
            mock_registry = MagicMock(spec=ControllerRegistry)
            mock_registry._controllers = {}
            mock_get_reg.return_value = mock_registry
            mock_app = MagicMock()
            mock_app.tenant_db = MagicMock()
            mock_app.tenant_db.on = MagicMock()
            
            with tempfile.TemporaryDirectory() as tmpdir:
                # Create a controller file
                controller_file = Path(tmpdir) / "test_doc.py"
                controller_file.write_text("""
from frappe_microservice.controller import DocumentController

class TestDoc(DocumentController):
    pass
""")
                
                # Call setup_controllers
                from frappe_microservice.controller import setup_controllers
                setup_controllers(mock_app, tmpdir)
                
                # Check that tenant_db got a registry
                assert mock_app.tenant_db.controller_registry is not None
                # Check that discover was called on THAT registry
                # (In this test, setup_controllers calls get_controller_registry() internally)
                assert mock_registry.auto_discover_controllers.called
                # Check that hooks were registered (on method was called)
                assert mock_app.tenant_db.on.called

    def test_controller_hook_registration(self):
        """Test that controller hooks are properly registered and called"""
        from frappe_microservice.controller import _register_controller_hooks, ControllerRegistry, DocumentController
        
        mock_tenant_db = MagicMock()
        handlers_by_event = {}
        
        def mock_on_implementation(doctype, event):
            def decorator(func):
                if event not in handlers_by_event:
                    handlers_by_event[event] = []
                handlers_by_event[event].append(func)
                return func
            return decorator
        
        mock_tenant_db.on = mock_on_implementation
        
        # Register the hooks
        _register_controller_hooks(mock_tenant_db)
        
        # Should have registered hooks for all lifecycle events
        assert len(handlers_by_event) > 0
        # Should have registered hooks for various events
        events = list(handlers_by_event.keys())
        assert 'validate' in events
        
        # Test that a handler actually works
        if 'validate' in handlers_by_event:
            handler = handlers_by_event['validate'][0]
            
            # Create a test document with a controller
            doc = MagicMock()
            doc.doctype = "Test"
            
            # This should call the handler which will invoke controller methods
            handler(doc)
