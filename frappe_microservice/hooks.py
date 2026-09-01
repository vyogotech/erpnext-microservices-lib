"""
Document lifecycle hook system for microservices.

This module provides DocumentHooks: a registry of callbacks that run at specific
document lifecycle events (before_validate, before_insert, after_insert, etc.)
without modifying Frappe core. Used by TenantAwareDB so that microservices can
add defaults, validation, or side effects (e.g. set tenant_id, send notifications)
when documents are created, updated, or deleted. Supports both doctype-specific
hooks and global hooks (doctype='*').
"""

import logging
from typing import Callable, Dict, List


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
        """
        Initialize the hook registry. _hooks is nested: doctype -> event -> list of callables.
        Use register() or the decorator() to add handlers; use run_hooks() to invoke them.
        """
        self._hooks: Dict[str, Dict[str, List[Callable]]] = {}
        self.logger = logger or logging.getLogger(__name__)

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
        """
        Return the ordered list of handlers for this doctype and event. Global
        hooks (doctype='*') are returned first, then doctype-specific hooks.
        """
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
        Invoke all registered handlers for doc.doctype and the given event. Each
        handler is called with (doc). If raise_on_error is True, the first exception
        is re-raised after logging; otherwise exceptions are only logged.
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
        Decorator that registers the decorated function as a hook for the given
        doctype and event. Returns the function unchanged so it can still be used
        elsewhere. Example: @hooks.decorator('Sales Order', 'before_insert').
        """
        def wrapper(func):
            self.register(doctype, event, func)
            return func
        return wrapper

    def list_hooks(self) -> Dict[str, Dict[str, List[str]]]:
        """
        Return a dict of doctype -> event -> list of handler __name__ strings.
        Useful for debugging and tests to see what hooks are registered.
        """
        result = {}
        for doctype, events in self._hooks.items():
            result[doctype] = {}
            for event, handlers in events.items():
                result[doctype][event] = [h.__name__ for h in handlers]
        return result
