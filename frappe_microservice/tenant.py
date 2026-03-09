"""
Tenant isolation: TenantAwareDB, get_user_tenant_id, NullContext.

This module provides multi-tenant data isolation for microservices:
- NullContext: No-op context manager used when OpenTelemetry is not available
  (so code can do "with tracer.start_as_current_span(...) if tracer else NullContext()").
- get_user_tenant_id(user_email): Resolves the tenant_id for a user from the User
  doctype (direct SQL then fallback to get_value). Rejects Guest and SYSTEM; returns None on failure.
- TenantAwareDB: Wrapper around Frappe DB that injects tenant_id into all filters,
  verifies tenant ownership on get_doc/set_value, and runs DocumentHooks on insert/update/delete.
  Used as app.tenant_db so that endpoints never forget to scope by tenant.
"""

import logging
from typing import Callable, Dict, List

import frappe

from frappe_microservice.hooks import DocumentHooks


class NullContext:
    """
    No-op context manager. Use when optional OpenTelemetry spans are not available:
    'with (tracer.start_as_current_span(...) if tracer else NullContext())'.
    set_attribute is a no-op for compatibility with span.set_attribute.
    """
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def set_attribute(self, *args): pass


def get_user_tenant_id(user_email=None):
    """
    Resolve tenant_id for a user from the User doctype. Used by secure_route to
    set g.tenant_id after authentication. Tries direct SQL first, then
    frappe.db.get_value. Returns None for Guest, missing user, disabled user,
    or SYSTEM tenant (security: SYSTEM must not be used for data access).
    """
    logger = logging.getLogger("frappe_microservice.get_user_tenant_id")

    if not user_email:
        user_email = getattr(
            getattr(frappe, 'session', None), 'user', 'Guest'
        ) or 'Guest'
        logger.debug(f"No user_email provided, resolved to: {user_email}")

    # Reject Guest users
    if not user_email or user_email == 'Guest':
        logger.warning(
            f"Rejected user '{user_email}' - Guest users not allowed in tenant resolution")
        return None

    logger.info(f"Resolving tenant_id for user: {user_email}")

    try:
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

            if tenant_id == 'SYSTEM':
                logger.error(
                    f"CRITICAL: User '{user_email}' has SYSTEM tenant_id - rejecting to prevent data leakage")
                return None

            logger.info(
                f"Successfully resolved tenant_id: {tenant_id} for user: {user_email}")
            return tenant_id

        if user_email == 'Administrator':
            logger.warning(
                f"Administrator user has no tenant_id. Please run setup_default_tenant_for_administrator() to create tenant-0 and link Administrator to it.")
        else:
            logger.warning(
                f"No tenant_id found for user '{user_email}' or user is disabled")
        return None
    except Exception as e:
        logger.error(f"Error in SQL query for user '{user_email}': {str(e)}")
        try:
            tenant_id = frappe.db.get_value('User', user_email, 'tenant_id')
            logger.info(
                f"Fallback method resolved tenant_id: {tenant_id} for user: {user_email}")

            if tenant_id == 'SYSTEM':
                logger.error(
                    f"CRITICAL: User '{user_email}' has SYSTEM tenant_id (fallback) - rejecting to prevent data leakage")
                return None

            if tenant_id:
                logger.info(
                    f"Fallback method successfully resolved tenant_id: {tenant_id} for user: {user_email}")
                return tenant_id
            return None
        except Exception as fallback_error:
            logger.error(
                f"Fallback method also failed for user '{user_email}': {str(fallback_error)}")
            return None


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

    def __init__(self, get_tenant_id_func, logger=None, verify_tenant_on_insert=True):
        """
        Initialize tenant-aware DB wrapper

        Args:
            get_tenant_id_func: Function that returns current tenant_id
            logger: Optional logger instance
            verify_tenant_on_insert: If True, verify tenant_id after insert (default: True)
                                    Set to False for performance in high-trust environments
        """
        self.get_tenant_id = get_tenant_id_func
        self.logger = logger or logging.getLogger(__name__)
        self.hooks = DocumentHooks(logger=self.logger)
        self.verify_tenant_on_insert = verify_tenant_on_insert

    def _add_tenant_filter(self, filters):
        """
        Inject the current tenant_id into the filters so queries are always
        tenant-scoped. Accepts None, dict, list, or str (document name).
        Raises ValueError if tenant_id is missing, SYSTEM, or filters type
        is unsupported.
        """
        tenant_id = self.get_tenant_id()

        if not tenant_id:
            raise ValueError(
                "No tenant_id found in context. Cannot query without tenant isolation.")

        if tenant_id == 'SYSTEM':
            raise ValueError("SYSTEM tenant_id is not allowed")

        if filters is None:
            return {'tenant_id': tenant_id}

        if isinstance(filters, str):
            return {'name': filters, 'tenant_id': tenant_id}

        if isinstance(filters, dict):
            filters = filters.copy()
            filters['tenant_id'] = tenant_id
            return filters

        if isinstance(filters, list):
            filters = filters.copy()
            filters.append(['tenant_id', '=', tenant_id])
            return filters

        raise TypeError(
            f"Unsupported filters type {type(filters).__name__}. "
            f"Expected None, str, dict, or list."
        )

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
        """Alias for get_all; same behaviour with tenant filter applied."""
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
                        f"Access denied: Document does not belong to current tenant"
                    )

            return doc


    def count(self, doctype, filters=None):
        """Count documents matching filters, always scoped to current tenant."""
        filters = self._add_tenant_filter(filters)
        return frappe.db.count(doctype, filters=filters)

    def exists(self, doctype, filters):
        """Return True if a document exists matching filters in the current tenant."""
        filters = self._add_tenant_filter(filters)
        return frappe.db.exists(doctype, filters)

    def get_value(self, doctype, filters, fieldname, **kwargs):
        """Get a single field value with tenant filter. filters can be dict or name string."""
        if isinstance(filters, str):
            filters = {"name": filters}
        filters = self._add_tenant_filter(filters)
        return frappe.db.get_value(doctype, filters, fieldname, **kwargs)

    def set_value(self, doctype, name, fieldname, value=None, **kwargs):
        """Update a single field; verifies tenant ownership first."""
        doc = self.get_doc(doctype, name, verify_tenant=True)
        if not doc:
            raise frappe.PermissionError(
                f"Document {doctype} {name} not found or belongs to another tenant"
            )
        return frappe.db.set_value(doctype, name, fieldname, value, **kwargs)

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

        return frappe.db.sql(query, values=values, **kwargs)

    def commit(self):
        """Commit the current Frappe DB transaction."""
        if not getattr(frappe, 'db', None):
            self.logger.warning("commit() called but frappe.db is not available")
            return
        return frappe.db.commit()

    def rollback(self):
        """Roll back the current Frappe DB transaction."""
        if not getattr(frappe, 'db', None):
            self.logger.warning("rollback() called but frappe.db is not available")
            return
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

        doc_dict = {
            "doctype": doctype,
            "tenant_id": tenant_id,
            **kwargs
        }

        doc = frappe.get_doc(doc_dict)
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
            doc = db.insert_doc('Sales Order', {'customer': 'CUST-001'})
            doc = db.insert_doc('Sales Order', customer='CUST-001', items=[...])
            doc = db.insert_doc(
                'User Permission', {'user': 'admin@example.com', 'allow': 'Company'},
                ignore_permissions=True)
        """
        tenant_id = self.get_tenant_id()

        self.logger.info(
            f"TenantAwareDB.insert_doc() called for doctype: {doctype}")
        self.logger.info(f"Retrieved tenant_id: {tenant_id}")

        if not tenant_id:
            error_msg = "No tenant_id in context. Cannot create document without tenant isolation."
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        insert_params = {}
        doc_fields = data.copy() if data else {}

        insert_param_names = {'ignore_permissions', 'ignore_links', 'ignore_if_duplicate',
                              'ignore_mandatory', 'set_name', 'set_child_names'}

        for key in list(kwargs.keys()):
            if key in insert_param_names:
                insert_params[key] = kwargs.pop(key)

        doc_fields.update(kwargs)
        self.logger.info(
            f"Document fields to be set: {list(doc_fields.keys())}")

        if 'tenant_id' in doc_fields and doc_fields['tenant_id'] != tenant_id:
            raise frappe.PermissionError(
                f"Cannot create document for different tenant. Current: {tenant_id}, Requested: {doc_fields['tenant_id']}"
            )

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

        if run_hooks:
            self.hooks.run_hooks(doc, 'before_validate')
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

            self.logger.info(
                f"Calling doc.insert() for {doctype} with params: {insert_params}")
            doc.insert(**insert_params)
            self.logger.info(
                f"doc.insert() completed. Document name: {getattr(doc, 'name', 'N/A')}")

            if tracer and span and hasattr(doc, 'name'):
                span.set_attribute("db.document.name", doc.name)

        if self.verify_tenant_on_insert and hasattr(doc, 'name') and doc.name:
            saved_tenant = frappe.db.get_value(doctype, doc.name, 'tenant_id')
            self.logger.info(
                f"Post-insert verification: saved tenant_id = {saved_tenant}")

            if saved_tenant != tenant_id:
                error_msg = (
                    f"CRITICAL: tenant_id mismatch after insert! "
                    f"Expected: {tenant_id}, Got: {saved_tenant}"
                )
                self.logger.error(error_msg)
                raise ValueError(error_msg)

            self.logger.info(
                f"Tenant isolation verified for {doctype}/{doc.name}")
        elif not self.verify_tenant_on_insert:
            self.logger.debug(
                "Skipped post-insert tenant verification (verify_tenant_on_insert=False)")

        if run_hooks:
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
        doc = self.get_doc(doctype, name, verify_tenant=True)

        if not data or not isinstance(data, dict):
            raise ValueError("update_doc requires a non-empty dict of fields to update")

        if 'tenant_id' in data:
            data = data.copy()
            del data['tenant_id']

        if run_hooks:
            self.hooks.run_hooks(doc, 'before_update')

        for key, value in data.items():
            if hasattr(doc, key):
                setattr(doc, key, value)

        doc.save(**kwargs)

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
        doc = None
        if verify_tenant or run_hooks:
            doc = self.get_doc(doctype, name, verify_tenant=verify_tenant)

            if run_hooks:
                self.hooks.run_hooks(doc, 'before_delete')

        if doc is not None:
            doc.delete(**kwargs)
        else:
            frappe.delete_doc(doctype, name, **kwargs)

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
        """Decorator to register a before_validate hook for the given doctype."""
        return self.hooks.decorator(doctype, 'before_validate')

    def before_insert(self, doctype: str = '*'):
        """Decorator to register a before_insert hook for the given doctype."""
        return self.hooks.decorator(doctype, 'before_insert')

    def after_insert(self, doctype: str = '*'):
        """Decorator to register an after_insert hook for the given doctype."""
        return self.hooks.decorator(doctype, 'after_insert')

    def before_update(self, doctype: str = '*'):
        """Decorator to register a before_update hook for the given doctype."""
        return self.hooks.decorator(doctype, 'before_update')

    def after_update(self, doctype: str = '*'):
        """Decorator to register an after_update hook for the given doctype."""
        return self.hooks.decorator(doctype, 'after_update')

    def before_delete(self, doctype: str = '*'):
        """Decorator to register a before_delete hook for the given doctype."""
        return self.hooks.decorator(doctype, 'before_delete')

    def after_delete(self, doctype: str = '*'):
        """Decorator to register an after_delete hook for the given doctype."""
        return self.hooks.decorator(doctype, 'after_delete')

    def list_hooks(self) -> Dict[str, Dict[str, List[str]]]:
        """
        List all registered hooks (for debugging)

        Returns:
            Dict with structure: {doctype: {event: [handler_names]}}
        """
        return self.hooks.list_hooks()
