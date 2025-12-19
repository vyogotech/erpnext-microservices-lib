# Frappe Microservices Framework
# A Python library for building secure Frappe microservices

from flask import Flask, request, jsonify, g
from functools import wraps
from typing import Callable, Dict, List, Any
import frappe
import requests
import os
import logging
import traceback

# Configure basic logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


class NullContext:
    """A context manager that does nothing"""
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def set_attribute(self, *args): pass


def get_user_tenant_id(user_email=None):
    """
    Get tenant_id for a user (Standard Helper)

    Args:
        user_email: Email ID of the user. If None, tries frappe.session.user
    """
    logger = logging.getLogger("frappe_microservice.get_user_tenant_id")

    if not user_email:
        user_email = frappe.session.user if hasattr(
            frappe, 'session') else 'Guest'
        logger.debug(f"No user_email provided, resolved to: {user_email}")

    # CRITICAL FIX: Reject Administrator and Guest users to prevent SYSTEM tenant fallback
    if not user_email or user_email in ('Guest', 'Administrator'):
        logger.warning(
            f"Rejected user '{user_email}' - system users not allowed in tenant resolution")
        return None

    logger.info(f"Resolving tenant_id for user: {user_email}")

    try:
        # Use direct SQL query to avoid session context issues in microservices
        result = frappe.db.sql("""
            SELECT tenant_id FROM `tabUser`
            WHERE name = %s AND enabled = 1
            LIMIT 1
        """, (user_email,), as_dict=True)

        logger.debug(f"SQL query result for user '{user_email}': {result}")

        if result and result[0].get('tenant_id'):
            tenant_id = result[0]['tenant_id']
            logger.info(
                f"Found tenant_id '{tenant_id}' for user '{user_email}'")

            # Additional safety check: reject SYSTEM tenant
            if tenant_id == 'SYSTEM':
                logger.error(
                    f"CRITICAL: User '{user_email}' has SYSTEM tenant_id - rejecting to prevent data leakage")
                return None

            logger.info(
                f"Successfully resolved tenant_id: {tenant_id} for user: {user_email}")
            return tenant_id

        logger.warning(
            f"No tenant_id found for user '{user_email}' or user is disabled")
        return None
    except Exception as e:
        logger.error(f"Error in SQL query for user '{user_email}': {str(e)}")
        # Fallback to original method if SQL query fails
        try:
            tenant_id = frappe.db.get_value('User', user_email, 'tenant_id')
            logger.info(
                f"Fallback method resolved tenant_id: {tenant_id} for user: {user_email}")

            # Additional safety check: reject SYSTEM tenant
            if tenant_id == 'SYSTEM':
                logger.error(
                    f"CRITICAL: User '{user_email}' has SYSTEM tenant_id (fallback) - rejecting to prevent data leakage")
                return None
            return tenant_id
        except Exception as fallback_error:
            logger.error(
                f"Fallback method also failed for user '{user_email}': {str(fallback_error)}")
            return None


class DocumentHooks:
    """
    Document lifecycle hook system for microservices

    Provides Frappe-style hooks without modifying Frappe core.
    Perfect for microservices that can't use Frappe's app hooks.

    Supported events:
    - before_validate: Before validation starts
    - validate: During validation
    - before_insert: Before inserting into database
    - after_insert: After inserting into database
    - before_update: Before updating document
    - after_update: After updating document
    - before_save: Before saving (insert or update)
    - after_save: After saving (insert or update)
    - before_delete: Before deleting document
    - after_delete: After deleting document
    """

    def __init__(self, logger=None):
        self._hooks: Dict[str, Dict[str, List[Callable]]] = {}
        self.logger = logger or logging.getLogger(__name__)
        # Structure: {
        #   'Sales Order': {
        #       'before_insert': [func1, func2],
        #       'after_insert': [func3],
        #   },
        #   '*': {  # Global hooks for all doctypes
        #       'before_insert': [global_func],
        #   }
        # }

    def register(self, doctype: str, event: str, handler: Callable):
        """
        Register a hook handler

        Args:
            doctype: DocType name or '*' for all doctypes
            event: Lifecycle event (before_insert, after_insert, validate, etc.)
            handler: Function to call (receives doc as first argument)

        Usage:
            hooks.register('Sales Order', 'before_insert', add_tenant_id)
            hooks.register('*', 'before_insert', log_creation)
        """
        if doctype not in self._hooks:
            self._hooks[doctype] = {}

        if event not in self._hooks[doctype]:
            self._hooks[doctype][event] = []

        self._hooks[doctype][event].append(handler)
        self.logger.info(
            f"Registered hook: {doctype}.{event} -> {handler.__name__}")

    def get_hooks(self, doctype: str, event: str) -> List[Callable]:
        """Get all hooks for a doctype and event"""
        hooks = []

        # Global hooks (*) - run first
        if '*' in self._hooks and event in self._hooks['*']:
            hooks.extend(self._hooks['*'][event])

        # Doctype-specific hooks - run after global
        if doctype in self._hooks and event in self._hooks[doctype]:
            hooks.extend(self._hooks[doctype][event])

        return hooks

    def run_hooks(self, doc, event: str, raise_on_error: bool = True):
        """
        Run all hooks for a document and event

        Args:
            doc: Document object
            event: Event name
            raise_on_error: If True, raise exception on hook error
        """
        hooks = self.get_hooks(doc.doctype, event)

        if not hooks:
            return

        for hook in hooks:
            try:
                hook(doc)
            except Exception as e:
                self.logger.error(
                    f"Error in hook {hook.__name__} for {doc.doctype}.{event}: {e}", exc_info=True)
                if raise_on_error:
                    raise

    def decorator(self, doctype: str = '*', event: str = 'before_insert'):
        """
        Decorator for registering hooks

        Usage:
            @hooks.decorator('Sales Order', 'before_insert')
            def add_defaults(doc):
                doc.status = 'Draft'
        """
        def wrapper(func):
            self.register(doctype, event, func)
            return func
        return wrapper

    def list_hooks(self) -> Dict[str, Dict[str, List[str]]]:
        """List all registered hooks (for debugging)"""
        result = {}
        for doctype, events in self._hooks.items():
            result[doctype] = {}
            for event, handlers in events.items():
                result[doctype][event] = [h.__name__ for h in handlers]
        return result


class TenantAwareDB:
    """
    Tenant-aware wrapper for Frappe ORM with hook support

    Automatically adds tenant_id to all queries for multi-tenant isolation.
    Provides document lifecycle hooks without modifying Frappe core.

    Usage:
        db = TenantAwareDB(get_tenant_id_func)

        # Register hooks
        @db.before_insert('Sales Order')
        def set_defaults(doc):
            doc.status = 'Draft'

        # Use with automatic tenant filtering
        users = db.get_all('User', filters={'status': 'active'})
    """

    def __init__(self, get_tenant_id_func, logger=None):
        """
        Initialize tenant-aware DB wrapper

        Args:
            get_tenant_id_func: Function that returns current tenant_id
        """
        self.get_tenant_id = get_tenant_id_func
        self.logger = logger or logging.getLogger(__name__)
        self.hooks = DocumentHooks(logger=self.logger)  # Hook system

    def _add_tenant_filter(self, filters):
        """Add tenant_id to filters dict or list"""
        tenant_id = self.get_tenant_id()

        if not tenant_id:
            raise ValueError(
                "No tenant_id found in context. Cannot query without tenant isolation.")

        if filters is None:
            return {'tenant_id': tenant_id}

        if isinstance(filters, dict):
            # Add tenant_id to dict filters
            filters = filters.copy()
            filters['tenant_id'] = tenant_id
            return filters

        if isinstance(filters, list):
            # Add tenant_id to list filters
            filters = filters.copy()
            filters.append(['tenant_id', '=', tenant_id])
            return filters

        return filters

    def get_all(self, doctype, filters=None, **kwargs):
        """
        Get all documents with automatic tenant_id filter

        Usage:
            users = db.get_all('User', filters={'status': 'active'})
        """
        try:
            from opentelemetry import trace
            tracer = trace.get_tracer(__name__)
        except ImportError:
            tracer = None

        with (tracer.start_as_current_span(f"db.get_all.{doctype}") if tracer else NullContext()) as span:
            if tracer and span:
                span.set_attribute("db.system", "mariadb")
                span.set_attribute("db.operation", "select")
                span.set_attribute("db.target", doctype)
            
            filters = self._add_tenant_filter(filters)
            return frappe.get_all(doctype, filters=filters, **kwargs)

    def get_list(self, doctype, filters=None, **kwargs):
        """Alias for get_all"""
        return self.get_all(doctype, filters=filters, **kwargs)

    def get_doc(self, doctype, name=None, verify_tenant=True, **kwargs):
        """
        Get a document and verify tenant ownership

        Args:
            doctype: DocType name
            name: Document name
            verify_tenant: If True, verifies tenant_id matches current tenant

        Usage:
            user = db.get_doc('User', 'USER-001')
            # Automatically verifies tenant_id
        """
        try:
            from opentelemetry import trace
            tracer = trace.get_tracer(__name__)
        except ImportError:
            tracer = None

        with (tracer.start_as_current_span(f"db.get_doc.{doctype}") if tracer else NullContext()) as span:
            if tracer and span:
                span.set_attribute("db.system", "mariadb")
                span.set_attribute("db.operation", "select")
                span.set_attribute("db.target", doctype)
                if name:
                    span.set_attribute("db.document.name", name)

            doc = frappe.get_doc(
                doctype, name, **kwargs) if name else frappe.get_doc(doctype, **kwargs)

            if verify_tenant and hasattr(doc, 'tenant_id'):
                tenant_id = self.get_tenant_id()
                if doc.tenant_id != tenant_id:
                    raise frappe.PermissionError(
                        f"Access denied: Document belongs to different tenant"
                    )

            return doc

    def delete_doc(self, doctype, name, verify_tenant=True, **kwargs):
        """
        Delete a document after verifying tenant ownership

        Args:
            doctype: DocType name
            name: Document name
            verify_tenant: If True, verifies tenant_id before deletion
        """
        if verify_tenant:
            # Verify tenant ownership before deleting
            self.get_doc(doctype, name, verify_tenant=True)

        return frappe.delete_doc(doctype, name, **kwargs)

    def count(self, doctype, filters=None):
        """Count documents with automatic tenant filter"""
        filters = self._add_tenant_filter(filters)
        return frappe.db.count(doctype, filters=filters)

    def exists(self, doctype, filters):
        """Check existence with automatic tenant filter"""
        filters = self._add_tenant_filter(filters)
        return frappe.db.exists(doctype, filters)

    def get_value(self, doctype, filters, fieldname, **kwargs):
        """Get value with automatic tenant filter"""
        filters = self._add_tenant_filter(filters)
        return frappe.db.get_value(doctype, filters, fieldname, **kwargs)

    def sql(self, query, values=None, **kwargs):
        """
        Raw SQL query with tenant_id validation

        Automatically validates that tenant_id is available before executing query.
        Developers must still manually add tenant_id filter to WHERE clause.

        Args:
            query: SQL query string
            values: Query parameters
            **kwargs: Additional parameters for frappe.db.sql()

        Raises:
            ValueError: If no tenant_id is available in context

        Usage:
            tenant_id = self.get_tenant_id()
            results = self.sql(
                "SELECT * FROM `tabSales Order` WHERE tenant_id = %s",
                tenant_id
            )
        """
        tenant_id = self.get_tenant_id()

        if not tenant_id:
            raise ValueError(
                "No tenant_id found in context. Cannot execute SQL query without tenant isolation.\n"
                "Ensure get_tenant_id_func is properly configured and user is authenticated."
            )

        # Execute query (developer is responsible for including tenant_id filter)
        return frappe.db.sql(query, values=values, **kwargs)

    def commit(self):
        """Commit transaction"""
        return frappe.db.commit()

    def rollback(self):
        """Rollback transaction"""
        return frappe.db.rollback()

    # ============================================
    # DOCUMENT CREATION WITH HOOKS
    # ============================================

    def new_doc(self, doctype, **kwargs):
        """
        Create new document with hooks and tenant_id

        Automatically injects tenant_id and runs before_validate hooks.
        Does NOT insert into database yet.

        Usage:
            doc = db.new_doc('Sales Order', customer='CUST-001')
            doc.insert()
        """
        tenant_id = self.get_tenant_id()

        if not tenant_id:
            raise ValueError(
                "No tenant_id in context. Cannot create document without tenant isolation.")

        # Create doc dict with tenant_id
        doc_dict = {
            "doctype": doctype,
            "tenant_id": tenant_id,
            **kwargs
        }

        doc = frappe.get_doc(doc_dict)

        # Run our custom before_validate hooks
        self.hooks.run_hooks(doc, 'before_validate', raise_on_error=True)

        return doc

    def insert_doc(self, doctype, data=None, run_hooks=True, **kwargs):
        """
        Create and insert document with hooks

        Hook execution order:
        1. before_validate (custom)
        2. validate (Frappe + custom)
        3. before_insert (Frappe + custom)
        4. [DB INSERT]
        5. after_insert (Frappe + custom)

        Args:
            doctype: DocType name
            data: Document data dict (optional, can pass as kwargs)
            run_hooks: If True, run custom hooks
            **kwargs: Document fields or insert() parameters

        Permission Handling:
            The ignore_permissions parameter can be passed in kwargs and will be forwarded
            to Frappe's doc.insert() method. However, use with caution:

            APPROPRIATE USAGE:
            - During signup/initialization (no user context exists)
            - System-level operations (migrations, scheduled tasks)
            - Administrative operations that must bypass user permissions

            INAPPROPRIATE USAGE:
            - Regular business operations (use role-based permissions instead)
            - User-initiated actions (rely on proper role assignment)
            - Microservice endpoints (should respect user permissions)

            BEST PRACTICE:
            Instead of using ignore_permissions, assign proper roles to users during
            signup (System Manager, Sales User, Stock User, etc.) and let Frappe's
            permission system handle access control.

        Usage:
            # With data dict
            doc = db.insert_doc('Sales Order', {'customer': 'CUST-001'})

            # With kwargs
            doc = db.insert_doc(
                'Sales Order', customer='CUST-001', items=[...])

            # With ignore_permissions (use sparingly!)
            doc = db.insert_doc(
                'User Permission', {'user': 'admin@example.com', 'allow': 'Company'},
                ignore_permissions=True)  # OK during signup
        """
        tenant_id = self.get_tenant_id()

        # Enhanced logging for debugging
        self.logger.info(
            f"TenantAwareDB.insert_doc() called for doctype: {doctype}")
        self.logger.info(f"Retrieved tenant_id: {tenant_id}")

        if not tenant_id:
            error_msg = "No tenant_id in context. Cannot create document without tenant isolation."
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Separate insert params from document fields
        insert_params = {}
        doc_fields = data.copy() if data else {}

        # Known insert() parameters
        insert_param_names = {'ignore_permissions', 'ignore_links', 'ignore_if_duplicate',
                              'ignore_mandatory', 'set_name', 'set_child_names'}

        for key in list(kwargs.keys()):
            if key in insert_param_names:
                insert_params[key] = kwargs.pop(key)

        # Remaining kwargs are document fields
        doc_fields.update(kwargs)
        self.logger.info(
            f"Document fields to be set: {list(doc_fields.keys())}")

        # Prevent tenant_id override
        if 'tenant_id' in doc_fields and doc_fields['tenant_id'] != tenant_id:
            raise frappe.PermissionError(
                f"Cannot create document for different tenant. Current: {tenant_id}, Requested: {doc_fields['tenant_id']}"
            )

        # Create doc with tenant_id
        doc_dict = {
            "doctype": doctype,
            "tenant_id": tenant_id,
            **doc_fields
        }

        self.logger.info(f"Creating document with tenant_id: {tenant_id}")
        doc = frappe.get_doc(doc_dict)

        # CRITICAL: Set tenant_id directly on doc object
        # This is required when tenant_id column was added via ALTER TABLE
        # (not in DocType metadata). Frappe's valid_columns only includes
        # fields from metadata, so fields added via ALTER TABLE won't be saved
        # during insert() unless set directly on the doc object.
        doc.tenant_id = tenant_id
        self.logger.info(
            f"Set tenant_id directly on doc object: {doc.tenant_id}")

        # Run custom hooks
        if run_hooks:
            # Before validate (our custom hook point)
            self.hooks.run_hooks(doc, 'before_validate')

            # Before insert (runs before Frappe's before_insert)
            self.hooks.run_hooks(doc, 'before_insert')

        try:
            from opentelemetry import trace
            tracer = trace.get_tracer(__name__)
        except ImportError:
            tracer = None

        with (tracer.start_as_current_span(f"db.insert.{doctype}") if tracer else NullContext()) as span:
            if tracer and span:
                span.set_attribute("db.system", "mariadb")
                span.set_attribute("db.operation", "insert")
                span.set_attribute("db.target", doctype)

            # Frappe's insert will call its own hooks + validate
            self.logger.info(
                f"Calling doc.insert() for {doctype} with params: {insert_params}")
            doc.insert(**insert_params)
            self.logger.info(
                f"doc.insert() completed. Document name: {getattr(doc, 'name', 'N/A')}")
            
            if tracer and span and hasattr(doc, 'name'):
                span.set_attribute("db.document.name", doc.name)

        # Safety check: Verify tenant_id was saved (required when column added via ALTER TABLE)
        # If tenant_id is not in DocType metadata, Frappe may not save it during insert()
        # even if set on the doc object. Use db_set as fallback.
        if hasattr(doc, 'name') and doc.name:
            self.logger.info(
                f"Verifying tenant_id was saved for {doctype} {doc.name}")
            saved_tenant_id = frappe.db.get_value(
                doctype, doc.name, 'tenant_id')
            self.logger.info(
                f"Saved tenant_id from DB: {saved_tenant_id}, Expected: {tenant_id}")

            if saved_tenant_id != tenant_id:
                self.logger.warning(
                    f"tenant_id was not saved for {doctype} {doc.name} during insert(). "
                    f"Expected: {tenant_id}, Found: {saved_tenant_id}. "
                    f"Setting directly via db_set. This indicates tenant_id column exists "
                    f"but is not in DocType metadata (added via ALTER TABLE)."
                )
                frappe.db.set_value(
                    doctype, doc.name, 'tenant_id', tenant_id, update_modified=False)
                # Reload doc to reflect the change
                doc.reload()
                self.logger.info(
                    f"Successfully set tenant_id via db_set for {doctype} {doc.name}")
            else:
                self.logger.info(
                    f"tenant_id correctly saved during insert for {doctype} {doc.name}")

        # Run custom hooks
        if run_hooks:
            # After insert (our custom hook point)
            self.hooks.run_hooks(doc, 'after_insert')

        return doc

    def update_doc(self, doctype, name, data, run_hooks=True, **kwargs):
        """
        Update document with hooks and tenant verification

        Hook execution order:
        1. [GET DOC - verify tenant]
        2. before_update (custom)
        3. validate (Frappe + custom)
        4. before_save (Frappe + custom)
        5. [DB UPDATE]
        6. after_save (Frappe + custom)
        7. after_update (custom)

        Args:
            doctype: DocType name
            name: Document name
            data: Fields to update
            run_hooks: If True, run custom hooks
            **kwargs: Passed to doc.save()

        Usage:
            doc = db.update_doc('Sales Order', 'SO-001',
                                {'status': 'Confirmed'})
        """
        # Get and verify tenant ownership
        doc = self.get_doc(doctype, name, verify_tenant=True)

        # Prevent tenant_id changes
        if 'tenant_id' in data:
            data = data.copy()
            del data['tenant_id']

        # Run custom before_update hooks
        if run_hooks:
            self.hooks.run_hooks(doc, 'before_update')

        # Update fields
        for key, value in data.items():
            if hasattr(doc, key):
                setattr(doc, key, value)

        # Frappe's save will call its own hooks
        doc.save(**kwargs)

        # Run custom after_update hooks
        if run_hooks:
            self.hooks.run_hooks(doc, 'after_update')

        return doc

    def delete_doc(self, doctype, name, verify_tenant=True, run_hooks=True, **kwargs):
        """
        Delete a document after verifying tenant ownership

        Hook execution order:
        1. [GET DOC - verify tenant]
        2. before_delete (custom)
        3. on_trash (Frappe)
        4. [DB DELETE]
        5. after_delete (Frappe + custom)

        Args:
            doctype: DocType name
            name: Document name
            verify_tenant: If True, verifies tenant_id before deletion
            run_hooks: If True, run custom hooks
            **kwargs: Passed to frappe.delete_doc()

        Usage:
            db.delete_doc('Sales Order', 'SO-001')
        """
        if verify_tenant or run_hooks:
            # Get document to verify tenant and/or run hooks
            doc = self.get_doc(doctype, name, verify_tenant=verify_tenant)

            # Run custom before_delete hooks
            if run_hooks:
                self.hooks.run_hooks(doc, 'before_delete')

        # Frappe's delete will call on_trash
        frappe.delete_doc(doctype, name, **kwargs)

        # Run custom after_delete hooks
        if run_hooks and doc:
            self.hooks.run_hooks(doc, 'after_delete')

    # ============================================
    # HOOK REGISTRATION METHODS
    # ============================================

    def on(self, doctype: str = '*', event: str = 'before_insert'):
        """
        Decorator for registering document hooks

        Usage:
            @app.tenant_db.on('Sales Order', 'before_insert')
            def set_defaults(doc):
                if not doc.status:
                    doc.status = 'Draft'

            @app.tenant_db.on('*', 'after_insert')
            def log_all_inserts(doc):
                print(f"Created: {doc.doctype} - {doc.name}")
        """
        return self.hooks.decorator(doctype, event)

    def before_validate(self, doctype: str = '*'):
        """Shortcut for before_validate hook"""
        return self.hooks.decorator(doctype, 'before_validate')

    def before_insert(self, doctype: str = '*'):
        """Shortcut for before_insert hook"""
        return self.hooks.decorator(doctype, 'before_insert')

    def after_insert(self, doctype: str = '*'):
        """Shortcut for after_insert hook"""
        return self.hooks.decorator(doctype, 'after_insert')

    def before_update(self, doctype: str = '*'):
        """Shortcut for before_update hook"""
        return self.hooks.decorator(doctype, 'before_update')

    def after_update(self, doctype: str = '*'):
        """Shortcut for after_update hook"""
        return self.hooks.decorator(doctype, 'after_update')

    def before_delete(self, doctype: str = '*'):
        """Shortcut for before_delete hook"""
        return self.hooks.decorator(doctype, 'before_delete')

    def after_delete(self, doctype: str = '*'):
        """Shortcut for after_delete hook"""
        return self.hooks.decorator(doctype, 'after_delete')

    def list_hooks(self) -> Dict[str, Dict[str, List[str]]]:
        """
        List all registered hooks (for debugging)

        Returns:
            Dict with structure: {doctype: {event: [handler_names]}}
        """
        return self.hooks.list_hooks()


class MicroserviceApp:
    """
    Base class for Frappe microservices

    Features:
    - Automatic Frappe initialization
    - Session-based authentication via Central Site
    - Secure-by-default endpoints
    - Automatic database connection management
    - Built-in error handling
    - Tenant-aware database queries

    Usage:
        app = MicroserviceApp(
            name="orders-service",
            central_site_url="http://central-site:8000",
            frappe_site="dev.localhost",
            tenant_field='tenant_id'
        )

        @app.secure_route('/orders', methods=['GET'])
        def list_orders(user):
            # Automatically filters by tenant_id
            return {"data": app.tenant_db.get_all('Sales Order')}
    """

    def __init__(self,
                 name,
                 central_site_url=None,
                 frappe_site=None,
                 sites_path=None,
                 db_host=None,
                 port=8000,
                 tenant_field='tenant_id',
                 get_tenant_id_func=None,
                 load_framework_hooks=None,
                 log_level=None,
                 otel_exporter_url=None):
        """
        Initialize microservice with configuration

        Args:
            load_framework_hooks: Control which framework hooks to load. Can be:
                - List of app names: ['frappe', 'erpnext'] - Load specific apps
                - 'full': Load frappe + erpnext hooks (backward compatibility)
                - 'frappe-only': Load only frappe hooks (backward compatibility)
                - 'none': Load only microservice hooks (backward compatibility)
                - None: Default to ['frappe', 'erpnext'] for backward compatibility
            log_level: logging level (e.g. logging.INFO, "DEBUG"). Defaults to LOG_LEVEL env var.
            otel_exporter_url: OTLP exporter URL. Defaults to OTEL_EXPORTER_OTLP_ENDPOINT env var.
        """
        self.name = name
        self.port = port
        self.tenant_field = tenant_field

        # Initialize Flask app
        self.flask_app = Flask(name)

        # Setup logging first
        self.log_level = log_level or os.getenv('LOG_LEVEL', 'INFO')
        self._setup_logging()

        # Setup OTEL
        self.otel_exporter_url = otel_exporter_url or os.getenv(
            'OTEL_EXPORTER_OTLP_ENDPOINT')
        self._setup_otel()

        # Convert string values to lists for backward compatibility
        if load_framework_hooks is None or load_framework_hooks == 'full':
            self.load_framework_hooks = ['frappe', 'erpnext']
        elif load_framework_hooks == 'frappe-only':
            self.load_framework_hooks = ['frappe']
        elif load_framework_hooks == 'none':
            self.load_framework_hooks = []
        elif isinstance(load_framework_hooks, list):
            self.load_framework_hooks = load_framework_hooks
        else:
            raise ValueError(
                f"load_framework_hooks must be a list of app names or one of ['full', 'frappe-only', 'none'], "
                f"got '{load_framework_hooks}'")

        # Frappe configuration
        self.frappe_site = frappe_site or os.getenv(
            'FRAPPE_SITE', 'site1.local')
        self.sites_path = sites_path or os.getenv(
            'FRAPPE_SITES_PATH', '/home/frappe/frappe-bench/sites')
        self.db_host = db_host or os.getenv('DB_HOST')

        # Central Site for authentication
        self.central_site_url = central_site_url or os.getenv(
            'CENTRAL_SITE_URL', 'http://central-site:8000')

        # Custom tenant_id getter function
        self._custom_get_tenant_id = get_tenant_id_func

        # Initialize tenant-aware DB wrapper
        self.tenant_db = TenantAwareDB(
            self._get_current_tenant_id, logger=self.logger)

        # Register middleware
        self._setup_middleware()

        # Add built-in routes
        self._register_built_in_routes()

    def _setup_logging(self):
        """Configure logging level and format"""
        # Convert string log level to logging constant
        if isinstance(self.log_level, str):
            self.log_level = getattr(logging, self.log_level.upper(), logging.INFO)

        # Configure root logger
        logging.getLogger().setLevel(self.log_level)
        
        # Configure Flask app logger
        self.flask_app.logger.setLevel(self.log_level)
        self.logger = self.flask_app.logger
        
        self.logger.info(f"Logging initialized at level: {logging.getLevelName(self.log_level)}")

    def _setup_otel(self):
        """Configure OpenTelemetry tracing"""
        if not self.otel_exporter_url:
            self.logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set, tracing disabled.")
            return

        try:
            from opentelemetry import trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.instrumentation.flask import FlaskInstrumentor
            from opentelemetry.sdk.resources import SERVICE_NAME, Resource

            # Set up resource
            resource = Resource(attributes={
                SERVICE_NAME: self.name
            })

            # Set up tracer provider
            provider = TracerProvider(resource=resource)
            processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=self.otel_exporter_url))
            provider.add_span_processor(processor)
            trace.set_tracer_provider(provider)

            # Instrument Flask
            FlaskInstrumentor().instrument_app(self.flask_app)
            
            self.logger.info(f"OpenTelemetry tracing enabled. Sending to: {self.otel_exporter_url}")

        except ImportError:
            self.logger.warning("OpenTelemetry libraries not found. Run 'pip install \"frappe-microservice[otel]\"' to enable tracing.")
        except Exception as e:
            self.logger.error(f"Failed to initialize OpenTelemetry: {e}")

    def _get_current_tenant_id(self):
        """
        Get current tenant_id from Flask g context
        Override this or provide get_tenant_id_func in __init__
        """
        if self._custom_get_tenant_id:
            return self._custom_get_tenant_id()

        # Try to get from Flask g context
        if hasattr(g, 'tenant_id'):
            return g.tenant_id

        return None

    def set_tenant_id(self, tenant_id):
        """Set tenant_id in Flask g context for current request"""
        g.tenant_id = tenant_id

    def _setup_middleware(self):
        """Set up Flask middleware for Frappe context"""

        @self.flask_app.before_request
        def setup_frappe_context():
            """Initialize Frappe for each request/thread"""
            # Check if frappe.local exists and has site
            needs_init = False
            try:
                needs_init = not hasattr(frappe, 'local') or not hasattr(
                    frappe.local, 'site') or not frappe.local.site
            except (AttributeError, RuntimeError):
                needs_init = True

            if needs_init:
                # Initialize frappe properly
                frappe.init(site=self.frappe_site, sites_path=self.sites_path)

                # Override DB host if specified (after init, before connect)
                if self.db_host:
                    frappe.local.conf.db_host = self.db_host
                    self.logger.info(f"Overriding DB host to: {self.db_host}")

                # Connect to database WITHOUT setting Administrator as default user
                # This prevents fallback to Administrator/SYSTEM tenant
                frappe.connect(set_admin_as_user=False)

                # Initialize session as Guest (will be set to actual user after validation)
                if hasattr(frappe, 'session'):
                    frappe.session.user = 'Guest'
                    frappe.session.sid = None
                    self.logger.debug(
                        "Initialized session as Guest (will be set after validation)")

                # CRITICAL: Override get_installed_apps to only return microservice apps
                # Only frappe, erpnext, and current service - no Central site apps
                self._isolate_microservice_apps()

                # Set up minimal module map to avoid app dependencies (already done by frappe.init)
                if not hasattr(frappe.local, 'module_app'):
                    frappe.local.module_app = {}
                if not hasattr(frappe.local, 'app_modules'):
                    frappe.local.app_modules = {}

            # Set up request context
            frappe.local.form_dict = frappe._dict()
            frappe.local.request_ip = request.remote_addr

        @self.flask_app.after_request
        def cleanup_frappe_context(response):
            """Clean up after request (but preserve session for logging/debugging)"""
            try:
                if hasattr(frappe, 'local') and hasattr(frappe.local, 'form_dict'):
                    frappe.local.form_dict.clear()

                # Commit any pending transactions
                if hasattr(frappe, 'db') and frappe.db:
                    frappe.db.commit()

                # Log session state for debugging (but don't clear it - it's request-scoped)
                if hasattr(frappe, 'session'):
                    self.logger.debug(
                        f"Request completed: user={frappe.session.user}, sid={frappe.session.sid}")
            except Exception as e:
                self.logger.warning(f"Cleanup warning: {e}", exc_info=True)

            return response

    def _register_built_in_routes(self):
        """Register standard microservice endpoints"""

        @self.flask_app.route('/health', methods=['GET'])
        def health():
            return jsonify({
                "status": "healthy",
                "service": self.name,
                "site": self.frappe_site
            })

    def _isolate_microservice_apps(self):
        """
        Override frappe.get_installed_apps() to control which framework hooks are loaded.
        This ensures hooks are only loaded from specified apps based on load_framework_hooks setting.

        load_framework_hooks can be a list of app names to load, e.g., ['frappe', 'erpnext']
        This allows fine-grained control over which apps' hooks are loaded.
        """
        # Use the list of apps from configuration
        microservice_apps = self.load_framework_hooks.copy(
        ) if self.load_framework_hooks else []

        # Log which apps are being loaded
        if microservice_apps:
            self.logger.info(
                f"Hook loading mode: Loading hooks from apps: {microservice_apps}")
        else:
            self.logger.info(f"Hook loading mode: NONE (microservice only)")

        # Add current service name if it's not already in the list
        # Convert service name to app name format (e.g., "orders-service" -> "orders_service")
        service_app_name = self.name.replace('-', '_')
        if service_app_name not in microservice_apps:
            microservice_apps.append(service_app_name)

        # Add current service name if it's not already in the list
        # Convert service name to app name format (e.g., "orders-service" -> "orders_service")
        service_app_name = self.name.replace('-', '_')
        if service_app_name not in microservice_apps:
            microservice_apps.append(service_app_name)

        # Store original function
        original_get_installed_apps = frappe.get_installed_apps

        # Create wrapper that filters to only microservice apps
        def microservice_get_installed_apps(*, _ensure_on_bench: bool = False):
            """Filter installed apps to only microservice apps based on load_framework_hooks setting"""
            # Get all installed apps from database
            installed = original_get_installed_apps(_ensure_on_bench=False)

            # Filter to only apps in microservice's allowed list
            filtered = [app for app in installed if app in microservice_apps]

            # If _ensure_on_bench is True, also filter by what's actually on bench
            if _ensure_on_bench:
                all_apps = frappe.cache().get_value("all_apps", frappe.get_all_apps)
                filtered = [app for app in filtered if app in all_apps]

            # Always ensure frappe is first
            if 'frappe' in filtered:
                filtered.remove('frappe')
                filtered.insert(0, 'frappe')

            return filtered

        # Monkey-patch frappe.get_installed_apps
        frappe.get_installed_apps = microservice_get_installed_apps
        self.logger.info(
            f"Microservice app isolation enabled: {microservice_apps}")

    def _validate_session(self):
        """
        Validate user session by calling Central Site API

        Uses Frappe's built-in session validation endpoint instead of 
        directly querying the Sessions table. This is more reliable and
        handles all session security checks (expiry, CSRF, etc.)

        Returns:
            tuple: (username, error_response)
            If valid: (username, None)
            If invalid: (None, error_response)
        """
        try:
            session_cookies = request.cookies
            self.logger.debug(
                f"Session validation - cookies: {dict(session_cookies)}")

            # Extract sid from cookies
            sid = session_cookies.get('sid')

            if not sid or sid == 'Guest':
                self.logger.info(
                    "Session validation - no valid sid, rejecting")
                return None, (jsonify({
                    "status": "error",
                    "message": f"Authentication required. Please login at Central Site: {self.central_site_url}/api/method/login",
                    "type": "Unauthorized",
                    "code": 401
                }), 401)

            # Call Central Site API to validate session
            # This uses Frappe's built-in session validation logic
            try:
                response = requests.get(
                    f'{self.central_site_url}/api/method/frappe.auth.get_logged_user',
                    cookies=session_cookies,
                    timeout=5,
                    headers={'Accept': 'application/json'}
                )

                self.logger.debug(
                    f"Session validation - Central Site response: {response.status_code}")

                if response.status_code == 200:
                    user_info = response.json()
                    username = user_info.get('message')

                    if username and username != 'Guest':
                        self.logger.info(
                            f"Session validation - valid user: {username}")

                        # CRITICAL: Properly set up the frappe session context for this request
                        frappe.set_user(username)
                        frappe.session.sid = sid

                        # Ensure session data is loaded
                        if hasattr(frappe, 'local') and hasattr(frappe.local, 'session'):
                            frappe.local.session.data = frappe._dict()

                        self.logger.debug(
                            f"Session context set: user={username}, sid={sid}")
                        return username, None
                    else:
                        self.logger.info(
                            "Session validation - user is Guest or invalid")
                else:
                    self.logger.info(
                        f"Session validation - Central Site returned {response.status_code}")

            except requests.exceptions.RequestException as api_error:
                self.logger.error(
                    f"Session validation - API call failed: {api_error}")
            except Exception as api_error:
                self.logger.error(
                    f"Session validation - API response error: {api_error}")

            # Invalid or expired session
            # CRITICAL FIX: Clear any fallback session context
            if hasattr(frappe, 'session'):
                frappe.session.user = 'Guest'
                frappe.session.sid = None
                self.logger.debug("Cleared invalid session context")

            return None, (jsonify({
                "status": "error",
                "message": f"Authentication required. Please login at Central Site: {self.central_site_url}/api/method/login",
                "type": "Unauthorized",
                "code": 401
            }), 401)

        except Exception as e:
            self.logger.error(f"Session validation error: {e}", exc_info=True)

            # CRITICAL FIX: Clear any fallback session context on error
            if hasattr(frappe, 'session'):
                frappe.session.user = 'Guest'
                frappe.session.sid = None
                self.logger.debug(
                    "Cleared session context after validation error")

            return None, (jsonify({
                "status": "error",
                "message": "Authentication service error. Please try again later.",
                "type": "AuthenticationError",
                "code": 401
            }), 401)

    def secure_route(self, rule, **options):
        """
        Combined decorator for routing + authentication

        Usage:
            @app.secure_route('/orders', methods=['GET'])
            def list_orders(user):
                # user is automatically injected
                return {"data": [...]}
        """
        def decorator(f):
            @wraps(f)
            def wrapper(*args, **kwargs):
                # Add direct debug logging
                print(f"SECURE_ROUTE DEBUG: Method {f.__name__} called")
                self.logger.info(
                    f"SECURE_ROUTE DEBUG: Method {f.__name__} called")

                # Validate session
                username, error_response = self._validate_session()

                print(
                    f"SECURE_ROUTE DEBUG: _validate_session returned: {username}, {error_response is not None}")
                self.logger.info(
                    f"SECURE_ROUTE DEBUG: _validate_session returned: {username}, {error_response is not None}")

                if error_response:
                    print(f"SECURE_ROUTE DEBUG: Returning error response")
                    return error_response

                # CRITICAL: Verify frappe.session.user matches validated username
                if hasattr(frappe, 'session') and frappe.session.user != username:
                    self.logger.warning(
                        f"Session mismatch detected: frappe.session.user={frappe.session.user}, validated={username}. Fixing...")
                    frappe.set_user(username)

                # Store user in Flask g context
                g.current_user = username
                self.logger.debug(
                    f"Set g.current_user = {username}, frappe.session.user = {frappe.session.user}")

                # Auto-resolve and set tenant_id if not already set
                if not hasattr(g, 'tenant_id') or not g.tenant_id:
                    tenant_id = get_user_tenant_id(username)
                    if tenant_id:
                        g.tenant_id = tenant_id
                        self.logger.debug(
                            f"Set g.tenant_id = {tenant_id} for user {username}")
                    else:
                        self.logger.warning(
                            f"No tenant_id found for user {username}")

                # Inject user as first parameter
                try:
                    print(
                        f"SECURE_ROUTE DEBUG: Calling function with user = {username}")
                    result = f(username, *args, **kwargs)

                    # Auto-convert dict to JSON response
                    if isinstance(result, dict):
                        return jsonify(result)

                    return result

                except (frappe.PermissionError, frappe.AuthenticationError) as e:
                    frappe.db.rollback()
                    self.logger.warning(
                        f"Access denied in {f.__name__}: {str(e)}")
                    return jsonify({
                        "status": "error",
                        "message": str(e) or "You do not have permission to access this resource.",
                        "type": "PermissionError",
                        "code": 403
                    }), 403

                except (frappe.DoesNotExistError, frappe.LinkValidationError) as e:
                    frappe.db.rollback()
                    self.logger.warning(
                        f"Resource not found in {f.__name__}: {str(e)}")
                    # Try to extract doctype from error if possible
                    return jsonify({
                        "status": "error",
                        "message": str(e) or "The requested resource was not found.",
                        "type": "DoesNotExistError",
                        "code": 404
                    }), 404

                except (frappe.ValidationError, ValueError, TypeError, KeyError) as e:
                    frappe.db.rollback()
                    self.logger.warning(
                        f"Invalid request in {f.__name__}: {str(e)}")
                    return jsonify({
                        "status": "error",
                        "message": f"Invalid input data: {str(e)}",
                        "type": type(e).__name__,
                        "code": 400
                    }), 400

                except Exception as e:
                    # Rollback on error
                    try:
                        frappe.db.rollback()
                    except Exception as rb_e:
                        self.logger.error(
                            f"Rollback failed: {rb_e}", exc_info=True)

                    self.logger.error(
                        f"Endpoint error in {f.__name__}: {e}\n{traceback.format_exc()}")

                    return jsonify({
                        "status": "error",
                        "message": "An internal server error occurred.",
                        "type": type(e).__name__,
                        "code": 500,
                        "details": traceback.format_exc() if self.flask_app.debug else None
                    }), 500

            # Register the wrapped function with Flask
            self.flask_app.route(rule, **options)(wrapper)
            return wrapper

        return decorator

    def route(self, rule, **options):
        """Standard Flask route (no authentication)"""
        return self.flask_app.route(rule, **options)

    def register_resource(self, doctype, base_path=None, methods=None, custom_handlers=None):
        """
        Register RESTful resource routes for a DocType (Frappe-style /api/resource pattern)

        Automatically creates standard CRUD endpoints:
        - GET    /api/resource/{doctype}           - List documents
        - POST   /api/resource/{doctype}           - Create document
        - GET    /api/resource/{doctype}/{name}    - Get document
        - PUT    /api/resource/{doctype}/{name}    - Update document
        - DELETE /api/resource/{doctype}/{name}    - Delete document

        Args:
            doctype: DocType name (e.g., "Sales Order")
            base_path: Custom base path (default: /api/resource)
            methods: List of HTTP methods to enable (default: all)
            custom_handlers: Dict of method -> handler function overrides

        Usage:
            # Simple - enable all CRUD
            app.register_resource("Sales Order")

            # Custom base path
            app.register_resource("Customer", base_path="/api/customers")

            # Only specific methods
            app.register_resource("Item", methods=['GET'])

            # Custom handlers
            app.register_resource("Payment", custom_handlers={
                'POST': my_custom_create_handler
            })
        """
        base_path = base_path or '/api/resource'
        methods = methods or ['GET', 'POST', 'PUT', 'DELETE']
        custom_handlers = custom_handlers or {}

        # Normalize doctype for URL (lowercase, hyphenated)
        doctype_url = doctype.lower().replace(' ', '-')

        # List endpoint: GET /api/resource/{doctype}
        if 'GET' in methods:
            if 'list' in custom_handlers:
                list_handler = custom_handlers['list']
            else:
                # Create unique function name to avoid Flask endpoint conflicts
                def make_list_handler(dt):
                    def handler(user):
                        from flask import request
                        filters = {}

                        # Parse query parameters as filters
                        for key, value in request.args.items():
                            if key not in ['fields', 'limit', 'offset', 'order_by']:
                                filters[key] = value

                        # Get fields, limit, order_by from query params
                        fields = request.args.get(
                            'fields', '*').split(',') if request.args.get('fields') else None
                        limit = int(request.args.get('limit', 20))
                        offset = int(request.args.get('offset', 0))
                        order_by = request.args.get(
                            'order_by', 'modified desc')

                        # Use tenant_db for automatic tenant isolation
                        documents = self.tenant_db.get_all(
                            dt,
                            filters=filters,
                            fields=fields,
                            limit_start=offset,
                            limit_page_length=limit,
                            order_by=order_by
                        )

                        return {
                            "data": documents,
                            "doctype": dt
                        }
                    return handler

                list_handler = make_list_handler(doctype)

            # Set unique endpoint name
            endpoint_name = f'list_{doctype_url.replace("-", "_")}'
            self.secure_route(f'{base_path}/{doctype_url}',
                              methods=['GET'], endpoint=endpoint_name)(list_handler)

            # Get single: GET /api/resource/{doctype}/{name}
            if 'get' in custom_handlers:
                get_handler = custom_handlers['get']
            else:
                def make_get_handler(dt):
                    def handler(user, name):
                        try:
                            # Use tenant_db for automatic tenant ownership verification
                            doc = self.tenant_db.get_doc(dt, name)
                            return doc.as_dict()
                        except frappe.PermissionError:
                            return {"error": "Access denied"}, 403
                        except frappe.DoesNotExistError:
                            return {"error": f"{dt} not found"}, 404
                    return handler

                get_handler = make_get_handler(doctype)

            endpoint_name = f'get_{doctype_url.replace("-", "_")}'
            self.secure_route(f'{base_path}/{doctype_url}/<name>',
                              methods=['GET'], endpoint=endpoint_name)(get_handler)

        # Create: POST /api/resource/{doctype}
        if 'POST' in methods:
            if 'post' in custom_handlers:
                create_handler = custom_handlers['post']
            else:
                def make_create_handler(dt):
                    def handler(user):
                        from flask import request
                        data = request.json

                        if not data:
                            return {"error": "Request body required"}, 400

                        # Use tenant_db to automatically inject tenant_id
                        doc = self.tenant_db.insert_doc(dt, data)

                        return {
                            "success": True,
                            "doctype": dt,
                            "name": doc.name
                        }, 201
                    return handler

                create_handler = make_create_handler(doctype)

            endpoint_name = f'create_{doctype_url.replace("-", "_")}'
            self.secure_route(f'{base_path}/{doctype_url}',
                              methods=['POST'], endpoint=endpoint_name)(create_handler)

        # Update: PUT /api/resource/{doctype}/{name}
        if 'PUT' in methods:
            if 'put' in custom_handlers:
                update_handler = custom_handlers['put']
            else:
                def make_update_handler(dt):
                    def handler(user, name):
                        from flask import request

                        try:
                            data = request.json

                            if not data:
                                return {"error": "Request body required"}, 400

                            # Use tenant_db for automatic tenant verification
                            doc = self.tenant_db.update_doc(dt, name, data)

                            return {
                                "success": True,
                                "doctype": dt,
                                "name": doc.name
                            }
                        except frappe.PermissionError:
                            return {"error": "Access denied"}, 403
                        except frappe.DoesNotExistError:
                            return {"error": f"{dt} not found"}, 404
                    return handler

                update_handler = make_update_handler(doctype)

            endpoint_name = f'update_{doctype_url.replace("-", "_")}'
            self.secure_route(f'{base_path}/{doctype_url}/<name>',
                              methods=['PUT'], endpoint=endpoint_name)(update_handler)

        # Delete: DELETE /api/resource/{doctype}/{name}
        if 'DELETE' in methods:
            if 'delete' in custom_handlers:
                delete_handler = custom_handlers['delete']
            else:
                def make_delete_handler(dt):
                    def handler(user, name):
                        try:
                            # Use tenant_db for automatic tenant verification
                            self.tenant_db.delete_doc(dt, name)
                            return {
                                "success": True,
                                "doctype": dt,
                                "message": f"{dt} deleted"
                            }
                        except frappe.PermissionError:
                            return {"error": "Access denied"}, 403
                        except frappe.DoesNotExistError:
                            return {"error": f"{dt} not found"}, 404
                    return handler

                delete_handler = make_delete_handler(doctype)

            endpoint_name = f'delete_{doctype_url.replace("-", "_")}'
            self.secure_route(f'{base_path}/{doctype_url}/<name>',
                              methods=['DELETE'], endpoint=endpoint_name)(delete_handler)

    @property
    def db(self):
        """Direct access to Frappe database"""
        return frappe.db

    def run(self, **kwargs):
        """Start the microservice"""
        self.logger.info("=" * 60)
        self.logger.info(f"Starting {self.name}")
        self.logger.info("=" * 60)
        self.logger.info(f"Site: {self.frappe_site}")
        self.logger.info(f"Central Site: {self.central_site_url}")
        self.logger.info(f"Port: {self.port}")
        self.logger.info("=" * 60)

        self.flask_app.run(
            host='0.0.0.0',
            port=self.port,
            debug=False,
            **kwargs
        )


# Convenience function for quick setup
def create_microservice(name, **config):
    """
    Quick setup for a microservice

    Usage:
        app = create_microservice("orders-service")

        @app.secure_route('/orders')
        def list_orders(user):
            return {"data": []}

        app.run()
    """
    return MicroserviceApp(name, **config)
