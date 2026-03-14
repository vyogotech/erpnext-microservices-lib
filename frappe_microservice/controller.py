"""
Document Controllers for Microservices

Provides Frappe-style controller classes without modifying Frappe core.
Controllers are automatically loaded and their methods are called during lifecycle events.

Usage:
    # controllers/sales_order.py
    from frappe_microservice.controller import DocumentController
    
    class SalesOrder(DocumentController):
        def validate(self):
            self.calculate_total()
        
        def before_insert(self):
            if not self.status:
                self.status = 'Draft'
"""

from typing import Dict, Type, Optional, Any, List, Set
import importlib
import os
import sys
import logging
from pathlib import Path
import frappe

# Get logger for this module
logger = logging.getLogger(__name__)


class DocumentController:
    """
    Base controller class - similar to frappe.model.document.Document
    
    Provides access to document fields via self.fieldname and self.doc
    All changes to self.fieldname are synced back to self.doc
    """
    
    def __init__(self, doc):
        """
        Initialize controller with document
        
        Args:
            doc: Frappe document object
        """
        self.doc = doc
        self._controller_name = self.__class__.__name__
    
    def __getattr__(self, key):
        """Get field from doc if not in controller"""
        if key.startswith('_') or key in ('doc', 'flags'):
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{key}'")
        return getattr(self.doc, key)
    
    def __setattr__(self, key, value):
        """Set field on both controller and doc"""
        if key in ('doc', '_controller_name') or key.startswith('_'):
            object.__setattr__(self, key, value)
        else:
            # Set on doc as well
            if hasattr(self, 'doc'):
                setattr(self.doc, key, value)
            object.__setattr__(self, key, value)
    
    def get(self, key, default=None):
        """Get field from doc"""
        return getattr(self.doc, key, default)
    
    def set(self, key, value):
        """Set field on doc"""
        setattr(self.doc, key, value)
        setattr(self, key, value)
    
    # ============================================
    # LIFECYCLE METHODS - Override in subclass
    # ============================================
    
    def validate(self):
        """Called during validation"""
        pass
    
    def before_validate(self):
        """Called before validation"""
        pass
    
    def before_insert(self):
        """Called before insert"""
        pass
    
    def after_insert(self):
        """Called after insert"""
        pass
    
    def before_save(self):
        """Called before save (insert or update)"""
        pass
    
    def after_save(self):
        """Called after save (insert or update)"""
        pass
    
    def before_update(self):
        """Called before update"""
        pass
    
    def after_update(self):
        """Called after update"""
        pass
    
    def on_update(self):
        """Called on update"""
        pass
    
    def before_delete(self):
        """Called before delete"""
        pass
    
    def on_trash(self):
        """Called before delete (Frappe convention)"""
        pass
    
    def after_delete(self):
        """Called after delete"""
        pass
    
    def on_cancel(self):
        """Called on cancel"""
        pass
    
    def on_submit(self):
        """Called on submit"""
        pass
    
    # ============================================
    # HELPER METHODS
    # ============================================
    
    def throw(self, message):
        """Throw validation error"""
        frappe.throw(message)
    
    def add_comment(self, comment_type='Comment', text=None):
        """Add comment to document"""
        self.doc.add_comment(comment_type, text)
    
    def has_value_changed(self, fieldname):
        """Check if field value changed"""
        if hasattr(self.doc, '_doc_before_save'):
            old_value = self.doc._doc_before_save.get(fieldname)
            new_value = self.doc.get(fieldname)
            return old_value != new_value
        return False
    
    def get_value_before_save(self, fieldname):
        """Get field value before save"""
        if hasattr(self.doc, '_doc_before_save'):
            return self.doc._doc_before_save.get(fieldname)
        return None


class ControllerRegistry:
    """
    Registry for document controllers
    
    Auto-discovers and loads controllers from a directory
    """
    
    def __init__(self):
        self._controllers: Dict[str, Type[DocumentController]] = {}
        self._controller_paths: list = []
        self._scanned_paths: Set[str] = set()
    
    def register_controller(self, doctype: str, controller_class: Type[DocumentController]):
        """
        Backwards-compatible alias for register().

        Args:
            doctype: DocType name
            controller_class: Controller class (subclass of DocumentController)
        """
        return self.register(doctype, controller_class)

    def register(self, doctype: str, controller_class: Type[DocumentController]):
        """
        Register a controller class for a doctype
        
        Args:
            doctype: DocType name
            controller_class: Controller class (subclass of DocumentController)
        """
        self._controllers[doctype] = controller_class
        logger.info(f"✅ [Registry] Registered: {doctype} -> {controller_class.__name__} (id={id(self)})")
    
    def get_controller(self, doctype: str) -> Optional[Type[DocumentController]]:
        """Get controller class for doctype"""
        return self._controllers.get(doctype)
    
    def has_controller(self, doctype: str) -> bool:
        """Check if controller exists for doctype"""
        return doctype in self._controllers
    
    def add_controller_path(self, path: str):
        """Add a directory to search for controllers"""
        if path not in self._controller_paths:
            self._controller_paths.append(path)
            logger.info(f"✅ Added controller path: {path}")

    def discover_controllers(self, directory: str):
        """
        Backwards-compatible alias for auto_discover_controllers().
        """
        return self.auto_discover_controllers(directory)
    
    def auto_discover_controllers(self, directory: str):
        """
        Auto-discover controllers from a directory
        
        Looks for Python files and tries to import controller classes.
        File naming convention: sales_order.py -> SalesOrder class
        """
        if not directory or not os.path.exists(directory):
            return

        # Singleton guard: if we've already scanned this exact path in THIS process,
        # we can skip it. Note: In standard RQ forking workers, this will still
        # run once per job in the child process unless discovery is triggered 
        # in the parent process setup.
        if directory in self._scanned_paths:
            return

        parent_dir = os.path.dirname(directory)
        dir_name = os.path.basename(directory)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        if directory not in sys.path:
            sys.path.insert(0, directory)
            
        logger.info(f"🔍 [Registry] Discovering in: {directory} (id={id(self)})")
        
        from frappe.model.document import Document as FrappeDocument
        valid_bases = (DocumentController, FrappeDocument)
        
        registered = []
        for file_path in Path(directory).glob('*.py'):
            if file_path.name.startswith('_'):
                continue
            
            module_name = file_path.stem
            doctype = self._filename_to_doctype(module_name)
            class_name = self._filename_to_classname(module_name)
            
            try:
                # Use standard import so the module identity is canonical
                qualified_name = f"{dir_name}.{module_name}"
                module = importlib.import_module(qualified_name)
                
                if hasattr(module, class_name):
                    controller_class = getattr(module, class_name)
                    
                    if isinstance(controller_class, type) and issubclass(controller_class, valid_bases):
                        # Only log and register if NOT already present
                        if doctype not in self._controllers:
                            self.register(doctype, controller_class)
                            registered.append(doctype)
                        else:
                            # Re-verify identity to be safe
                            if self._controllers[doctype] != controller_class:
                                self.register(doctype, controller_class)
                                registered.append(doctype)
                
            except Exception as e:
                logger.error(f"❌ Error loading controller from {file_path.name}: {e}")

        if registered:
            logger.info(f"✅ Registered controllers: {', '.join(registered)}")
            
        self._scanned_paths.add(directory)

    def setup_controllers(self, app):
        """
        Backwards-compatible no-op setup hook for tests and integrations.
        """
        logger.info("✅ Controller registry setup completed")
    
    def _filename_to_doctype(self, filename: str) -> str:
        """
        Convert filename to DocType name
        sales_order -> Sales Order
        """
        return ' '.join(word.capitalize() for word in filename.split('_'))
    
    def _filename_to_classname(self, filename: str) -> str:
        """
        Convert filename to class name
        sales_order -> SalesOrder
        """
        return ''.join(word.capitalize() for word in filename.split('_'))
    
    def list_controllers(self) -> Dict[str, str]:
        """List all registered controllers"""
        return {
            doctype: controller.__name__ 
            for doctype, controller in self._controllers.items()
        }
    
    def create_controller_instance(self, doc) -> Optional[DocumentController]:
        """
        Create controller instance for document
        
        Args:
            doc: Frappe document object
        
        Returns:
            Controller instance or None if no controller registered
        """
        controller_class = self.get_controller(doc.doctype)
        if controller_class:
            return controller_class(doc)
        return None


# Global controller registry
_registry = ControllerRegistry()


def register_controller(doctype: str):
    """
    Decorator to register a controller class
    
    Usage:
        @register_controller('Sales Order')
        class SalesOrder(DocumentController):
            def validate(self):
                pass
    """
    def wrapper(cls):
        get_controller_registry().register(doctype, cls)
        return cls
    return wrapper


def get_controller_registry() -> ControllerRegistry:
    """Get the global controller registry (ensures sharing across modules)"""
    if not hasattr(frappe, "_microservice_registry"):
        frappe._microservice_registry = ControllerRegistry()
        logger.info(f"✨ [Registry] Initialized NEW global registry (id={id(frappe._microservice_registry)})")
    return frappe._microservice_registry


def setup_controllers(app, controllers_directory: str = None):
    """
    Setup controllers for a microservice app
    
    Args:
        app: MicroserviceApp instance
        controllers_directory: Directory containing controller files
    
    Usage:
        from frappe_microservice import create_microservice
        from frappe_microservice.controller import setup_controllers
        
        app = create_microservice("my-service")
        setup_controllers(app, "./controllers")
    """
    registry = get_controller_registry()
    if controllers_directory and os.path.exists(controllers_directory):
        registry.auto_discover_controllers(controllers_directory)
    
    # Hook the registry into TenantAwareDB
    app.tenant_db.controller_registry = registry
    
    # Register hooks to call controller methods
    _register_controller_hooks(app.tenant_db)
    
    logger.info(f"✅ Controllers setup complete. Registered: {list(registry._controllers.keys())}")


def _register_controller_hooks(tenant_db):
    """Register hooks that call controller methods"""
    
    # Map of hook events to controller method names
    event_method_map = {
        'before_validate': 'before_validate',
        'validate': 'validate',
        'before_insert': 'before_insert',
        'after_insert': 'after_insert',
        'before_save': 'before_save',
        'after_save': 'after_save',
        'before_update': 'before_update',
        'after_update': 'after_update',
        'before_delete': 'before_delete',
        'on_trash': 'on_trash',
        'after_delete': 'after_delete',
    }
    
    for event, method_name in event_method_map.items():
        # Create a closure for each event
        def make_handler(method_name):
            def handler(doc):
                controller = _registry.create_controller_instance(doc)
                if controller:
                    method = getattr(controller, method_name, None)
                    if method and callable(method):
                        method()
                        # Sync any changes back to doc
                        for key, value in controller.__dict__.items():
                            if not key.startswith('_') and key not in ('doc',):
                                setattr(doc, key, value)
            return handler
        
        # Register as global hook
        tenant_db.on('*', event)(make_handler(method_name))


__all__ = [
    'DocumentController',
    'ControllerRegistry',
    'register_controller',
    'get_controller_registry',
    'setup_controllers',
]

