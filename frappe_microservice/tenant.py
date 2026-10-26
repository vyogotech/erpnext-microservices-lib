"""
Tenant isolation: TenantAwareDB, get_user_tenant_id, NullContext,
patch_valid_dict_for_tenant_id.

This module provides multi-tenant data isolation for microservices:
- NullContext: No-op context manager used when OpenTelemetry is not available
  (so code can do "with tracer.start_as_current_span(...) if tracer else NullContext()").
- get_user_tenant_id(user_email): Resolves the tenant_id for a user from the User
  doctype (direct SQL then fallback to get_value). Rejects Guest and SYSTEM; returns None on failure.
- TenantAwareDB: Wrapper around Frappe DB that injects tenant_id into all filters,
  verifies tenant ownership on get_doc/set_value, and runs DocumentHooks on insert/update/delete.
  Used as app.tenant_db so that endpoints never forget to scope by tenant.
- patch_valid_dict_for_tenant_id(): Monkey-patches BaseDocument.get_valid_dict
  so that tenant_id (added via ALTER TABLE, absent from DocType metadata) is
  always included in INSERT/UPDATE SQL. Must be called once after frappe.init().
"""

import logging
from typing import Dict, List

import frappe

from frappe_microservice.hooks import DocumentHooks
from frappe_microservice.policy import resolve_bool

try:
    from opentelemetry import trace

    _otel_tracer = trace.get_tracer(__name__)
except ImportError:
    _otel_tracer = None


_logger = logging.getLogger(__name__)


# Masters this library renames on insert: the docname becomes ``<tenant8>-<slug>``
# so ``tab<Doctype>.name`` can stay globally unique, while the mapped field keeps
# the label the caller actually works in. Anything renamed here must be presented
# back under its label on the way out — see ``frappe_microservice.presentation``.
FIELD_AUTONAMED_MASTER_DOCTYPES = {
    "Item": ("item_code", "item"),
    "Item Group": ("item_group_name", "category"),
    "Supplier": ("supplier_name", "supplier"),
    "Customer": ("customer_name", "customer"),
    "Customer Group": ("customer_group_name", "group"),
    "Supplier Group": ("supplier_group_name", "group"),
    "Warehouse": ("warehouse_name", "warehouse"),
}


class TenantAccessError(frappe.PermissionError):
    """
    Raised when a document belongs to a different tenant.

    Subclasses ``frappe.PermissionError`` so existing ``except
    frappe.PermissionError`` handlers keep catching it (no behavior change when
    masking is off). ``secure_route`` masks *only* this subclass to 404 when
    ``MASK_PERMISSION_ERRORS`` is enabled, so role-permission denials (plain
    ``PermissionError``) still surface as 403.
    """

    pass


# Global schema/config DocTypes that must never be tenant-partitioned.
#
# tenancy_app's ``add_tenant_id_to_all_tables`` patch ALTERs every non-single
# table, so these tables *do* have a tenant_id column — stamping them would not
# fail, it would split one shared schema across tenants. They are written by
# ``bench migrate`` and fixture sync, not by tenant traffic, so this set is
# belt-and-braces for the case where a tenant request triggers a schema write.
#
# Audit/activity doctypes (Version, Comment, Activity Log, Route History, ...)
# are deliberately NOT here: they are the history of a *tenant's* documents, and
# leaving them at the SYSTEM default makes them unreadable through
# TenantAwareDB, which filters ``tenant_id = <tenant>`` and rejects SYSTEM.
GLOBAL_DOCTYPES = frozenset(
    {
        # Core schema & metadata
        "DocType",
        "DocField",
        "DocPerm",
        "DocType Action",
        "DocType Link",
        "DocType State",
        "DocType Layout",
        "Module Def",
        "Custom Field",
        "Property Setter",
        # Platform configuration shared by every tenant
        "Role",
        "Scheduled Job Type",
        "Website Route Meta",
        "Installed Applications",
        "Installed Application",
    }
)


def _should_stamp_tenant_id(doc):
    """
    Return True when ``tenant_id`` belongs in this document's INSERT/UPDATE SQL.

    In multi-tenant mode every table is assumed to have the column: a tenancy
    provider's ``add_tenant_id_to_all_tables`` patch ALTERs every DocType with
    ``issingle: 0``, child tables included. Measured on an ERPNext site with that
    patch applied: 753 of 756 tables carry it. The three that do not — ``tabSeries``,
    ``tabSessions``, ``tabSingles`` — are framework stores written by raw SQL or by
    ``update_single``, never through a DocType ``db_insert``. So no per-write schema
    probe is needed; two structural exclusions cover everything:

    - ``GLOBAL_DOCTYPES``: shared schema/config. These *do* have the column, so
      this is about not partitioning one shared schema across tenants, not about
      avoiding an error.
    - **Single DocTypes**: excluded by the patch's ``issingle: 0`` filter, and
      ``tabSingles`` is one of the three tables without the column.
      ``Document.update_single`` DELETEs every ``tabSingles`` row for the doctype
      and re-INSERTs one per key in the valid dict, so a stamped single writes a
      real ``(doctype, 'tenant_id', <tenant>)`` row that the next tenant to save
      that single silently overwrites. ``meta.issingle`` is what catches this; a
      ``"Singles"`` entry in a doctype set cannot, because ``self.doctype`` is the
      single's own name.

    A site where the patch has not run is a deployment error, not a case to degrade
    into: it surfaces as OperationalError 1054 on the first child-row insert rather
    than as rows silently landing at the SYSTEM default where no tenant can read
    them. See ``docker-compose.integration.yml`` — the suite runs against the
    central-site image so tests exercise the migrated schema.
    """
    doctype = getattr(doc, "doctype", None)
    if not isinstance(doctype, str) or not doctype:
        return True
    if doctype in GLOBAL_DOCTYPES:
        return False
    try:
        # cached_property on BaseDocument, already resolved by get_valid_dict via
        # get_valid_columns() — this costs nothing.
        return not getattr(doc.meta, "issingle", 0)
    except Exception:
        return True


def _parent_tenant_id(child, parenttype, parent):
    """
    Resolve the parent document's tenant_id without a query.

    Frappe gives every child row a weak reference back to its parent
    (``BaseDocument.parent_doc``, set in ``append()``), and the parent is alive
    on the stack for the whole save — children are persisted only after it. Its
    ``tenant_id`` is the same value the DB holds: the parent is inserted first,
    and a parent loaded for an update comes from ``SELECT *``, which includes
    columns outside DocType metadata.

    Measured on a single Purchase Invoice insert: 38 child rows, ``parent_doc``
    present on all 38 — so the read is free where it used to be 38 SELECTs.

    The session (``frappe.session.tenant_id``) is *not* used as the source. It is
    empty in Administrator contexts — bootstrap and platform-level writes, where
    the parent still correctly reads ``SYSTEM`` — and it says nothing about the
    parent, so it cannot support the cross-tenant check the caller performs on
    this value.

    Falls back to one query when there is no live parent reference (a child
    constructed standalone, or the weakref collected).
    """
    parent_doc = getattr(child, "parent_doc", None)
    if parent_doc is not None:
        cached = getattr(parent_doc, "tenant_id", None)
        if isinstance(cached, str) and cached.strip():
            return cached
    try:
        return frappe.db.get_value(parenttype, parent, "tenant_id")
    except Exception:
        return None


def patch_valid_dict_for_tenant_id(get_tenant_id_func=None, multi_tenant=None):
    """
    Monkey-patch ``BaseDocument.get_valid_dict`` so that ``tenant_id`` is
    included in the dict that Frappe uses to build INSERT and UPDATE SQL.

    **Why this is needed:**
    ``tenant_id`` is added to DocType tables via ``ALTER TABLE``, not via
    DocType field metadata. Frappe's ``get_valid_dict`` only returns fields
    from ``get_valid_columns`` (metadata-driven), so the INSERT/UPDATE SQL
    never contains ``tenant_id`` — the column keeps its DB default (usually
    ``SYSTEM`` or ``NULL``).

    This patch appends ``tenant_id`` to the returned dict whenever it is set
    on the document object. When a secure request tenant provider is supplied,
    it also stamps new standalone documents created internally by ERPNext (for
    example immutable-ledger reversal rows) and rejects real cross-tenant
    writes. It covers:
    - ``insert_doc`` (single inserts via TenantAwareDB)
    - ``new_doc`` followed by manual ``doc.insert()``
    - Child-table row inserts (Frappe calls ``db_insert`` per child)
    - Direct ``frappe.get_doc({...}).insert()`` anywhere in service code

    Must be called after ``frappe.init()`` — patching is idempotent and the
    tenant provider is refreshed on every call.

    Gated on the multi-tenant flag (kwarg, then ``MULTI_TENANT`` env, default
    on). When multi-tenancy is off the patch is never installed, so stamping
    costs nothing on the write path rather than being branched away per row.
    Single-tenant deployments must also stop routing reads through
    ``TenantAwareDB``: it filters ``tenant_id = <tenant>`` and would return
    nothing once rows land at the ``SYSTEM`` column default.
    """
    if not resolve_bool(multi_tenant, "MULTI_TENANT", default=True):
        _logger.info("MULTI_TENANT is off — tenant_id stamping not installed")
        return

    from frappe.model.base_document import BaseDocument

    BaseDocument._tenant_id_provider = get_tenant_id_func

    if getattr(BaseDocument, "_tenant_valid_dict_patched", False):
        return

    original_get_valid_dict = BaseDocument.get_valid_dict

    def _patched_get_valid_dict(self, *args, **kwargs):
        d = original_get_valid_dict(self, *args, **kwargs)
        if not _should_stamp_tenant_id(self):
            return d
        tenant_id = getattr(self, "tenant_id", None)

        provider = getattr(BaseDocument, "_tenant_id_provider", None)
        try:
            request_tenant_id = provider() if callable(provider) else None
        except Exception:
            request_tenant_id = None
        if not (
            isinstance(request_tenant_id, str)
            and request_tenant_id.strip()
            and request_tenant_id != "SYSTEM"
        ):
            request_tenant_id = None

        # Child rows are persisted after their parent, but microservice images
        # intentionally do not install the Central Site's tenancy_app hooks.
        # Resolve the parent's persisted tenant at the final SQL boundary so a
        # child supplied as SYSTEM (or with another tenant) cannot keep that
        # value. This also covers children added by ERPNext validation after
        # TenantAwareDB constructed the document tree.
        parent = getattr(self, "parent", None)
        parenttype = getattr(self, "parenttype", None)
        if (
            isinstance(parent, str)
            and parent.strip()
            and isinstance(parenttype, str)
            and parenttype.strip()
        ):
            parent_tenant_id = _parent_tenant_id(self, parenttype, parent)
            if isinstance(parent_tenant_id, str) and parent_tenant_id.strip():
                if request_tenant_id and parent_tenant_id != request_tenant_id:
                    raise TenantAccessError(
                        "Access denied: Parent document does not belong to current tenant"
                    )
                tenant_id = parent_tenant_id
                self.tenant_id = parent_tenant_id

        # ERPNext creates some accounting side effects directly with
        # frappe.new_doc(...).submit(), bypassing TenantAwareDB.insert_doc().
        # The authenticated request tenant is authoritative for an unowned or
        # SYSTEM-owned standalone row. A concrete different tenant must fail
        # closed instead of being silently reassigned.
        elif request_tenant_id:
            if tenant_id not in (None, "", "SYSTEM", request_tenant_id):
                raise TenantAccessError(
                    "Access denied: Document does not belong to current tenant"
                )
            tenant_id = request_tenant_id
            self.tenant_id = request_tenant_id

        if tenant_id:
            d["tenant_id"] = tenant_id
        return d

    BaseDocument.get_valid_dict = _patched_get_valid_dict
    BaseDocument._tenant_valid_dict_patched = True
    _logger.info("Patched BaseDocument.get_valid_dict to include tenant_id in SQL")


class NullContext:
    """
    No-op context manager. Use when optional OpenTelemetry spans are not available:
    'with (tracer.start_as_current_span(...) if tracer else NullContext())'.
    set_attribute is a no-op for compatibility with span.set_attribute.
    """

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, *args):
        pass


def get_user_tenant_id(user_email=None):
    """
    Resolve tenant_id for a user from the User doctype. Used by secure_route to
    set g.tenant_id after authentication. Tries direct SQL first, then
    frappe.db.get_value. Returns None for Guest, missing user, disabled user,
    or SYSTEM tenant (security: SYSTEM must not be used for data access).
    """
    logger = logging.getLogger("frappe_microservice.get_user_tenant_id")

    if not user_email:
        user_email = (
            getattr(getattr(frappe, "session", None), "user", "Guest") or "Guest"
        )
        logger.debug(f"No user_email provided, resolved to: {user_email}")

    # Reject Guest users
    if not user_email or user_email == "Guest":
        logger.warning(
            f"Rejected user '{user_email}' - Guest users not allowed in tenant resolution"
        )
        return None

    logger.info(f"Resolving tenant_id for user: {user_email}")

    try:
        result = frappe.db.sql(
            """
            SELECT tenant_id FROM `tabUser`
            WHERE name = %s AND enabled = 1
            LIMIT 1
        """,
            (user_email,),
            as_dict=True,
        )

        logger.debug(f"SQL query result for user '{user_email}': {result}")

        if result and result[0].get("tenant_id"):
            tenant_id = result[0]["tenant_id"]
            logger.info(f"Found tenant_id '{tenant_id}' for user '{user_email}'")

            if tenant_id == "SYSTEM":
                logger.error(
                    f"CRITICAL: User '{user_email}' has SYSTEM tenant_id - rejecting to prevent data leakage"
                )
                return None

            logger.info(
                f"Successfully resolved tenant_id: {tenant_id} for user: {user_email}"
            )
            return tenant_id

        if user_email == "Administrator":
            # TODO(tenant-admin): this guidance contradicts the platform model
            # and should be replaced with "authenticate as a tenant user".
            # Administrator is platform-level and cannot belong to a tenant —
            # tenancy_app.utils.tenant.setup_user_tenant() pins it to SYSTEM,
            # and set_tenant_id() stamps SYSTEM on anything it writes. Linking
            # it to tenant-0 would therefore make every write fail the
            # post-insert tenant check in insert_doc(). The named helper also
            # delegates to a provisioning function that no longer exists, so
            # following this advice raises AttributeError rather than doing
            # anything.
            logger.warning(
                "Administrator user has no tenant_id. Please run "
                "setup_default_tenant_for_administrator() to create tenant-0 "
                "and link Administrator to it."
            )
        else:
            logger.warning(
                f"No tenant_id found for user '{user_email}' or user is disabled"
            )
        return None
    except Exception as e:
        logger.error(f"Error in SQL query for user '{user_email}': {str(e)}")
        try:
            tenant_id = frappe.db.get_value("User", user_email, "tenant_id")
            logger.info(
                f"Fallback method resolved tenant_id: {tenant_id} for user: {user_email}"
            )

            if tenant_id == "SYSTEM":
                logger.error(
                    "CRITICAL: User '%s' has SYSTEM tenant_id (fallback) - "
                    "rejecting to prevent data leakage" % user_email
                )
                return None

            if tenant_id:
                logger.info(
                    f"Fallback method successfully resolved tenant_id: {tenant_id} for user: {user_email}"
                )
                return tenant_id
            return None
        except Exception as fallback_error:
            logger.error(
                f"Fallback method also failed for user '{user_email}': {str(fallback_error)}"
            )
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

    def __init__(
        self,
        get_tenant_id_func,
        logger=None,
        verify_tenant_on_insert=True,
        permission_mode="warn",
        tenant_strict="warn",
        multi_tenant=None,
    ):
        """
        Initialize tenant-aware DB wrapper

        Args:
            get_tenant_id_func: Function that returns current tenant_id
            logger: Optional logger instance
            verify_tenant_on_insert: If True, verify tenant_id after insert (default: True)
                                    Set to False for performance in high-trust environments
            permission_mode: off|warn|enforce for read/set_value authorization. Default "warn" = log-only, zero
                             behavior change. Writes (insert/update/delete) are
                             always enforced by Frappe's document model.
            multi_tenant: Whether tenant isolation is active. Falls back to the
                          ``MULTI_TENANT`` env var, default True. When False the
                          wrapper never resolves or writes a ``tenant_id``, so the
                          library runs against a stock Frappe/ERPNext site whose
                          tables have no ``tenant_id`` column.
        """
        self.get_tenant_id = get_tenant_id_func
        self.logger = logger or logging.getLogger(__name__)
        self.hooks = DocumentHooks(logger=self.logger)
        self.verify_tenant_on_insert = verify_tenant_on_insert
        self.permission_mode = permission_mode
        # off|warn|enforce for the "table has no tenant_id column" fail-open gap.
        # Default "warn".
        self.tenant_strict = tenant_strict
        self.multi_tenant = resolve_bool(multi_tenant, "MULTI_TENANT", default=True)
        if not self.multi_tenant:
            self.logger.info(
                "MULTI_TENANT is off — queries and writes are not tenant-scoped"
            )
            if tenant_strict == "enforce":
                self.logger.warning(
                    "TENANT_STRICT=enforce has no effect while MULTI_TENANT is off"
                )

    def current_tenant_id(self):
        """
        Tenant to scope by, or None in single-tenant mode.

        Prefer this over calling ``get_tenant_id`` directly: the raw provider is
        unaware of the mode flag and would hand back a tenant that must not be
        used for scoping.
        """
        return self.get_tenant_id() if self.multi_tenant else None

    def _check_permission(self, doc_or_doctype, ptype):
        """
        Enforce Frappe role permissions for read/write according to
        ``permission_mode``.

        The current user is already set by ``secure_route`` via
        ``frappe.set_user()`` before any tenant_db call, so no user argument is
        needed. ``off`` = no check; ``warn`` = log ``[would-deny]`` and proceed;
        ``enforce`` = raise ``frappe.PermissionError``.
        """
        if self.permission_mode == "off":
            return

        if isinstance(doc_or_doctype, str):
            doctype = doc_or_doctype
            try:
                allowed = frappe.has_permission(doctype, ptype)
            except Exception as e:
                # e.g. doctype has no meta — treat as not-allowed.
                self.logger.debug(
                    "has_permission(%s, %s) raised %s; treating as denied",
                    doctype,
                    ptype,
                    e,
                )
                allowed = False
        else:
            doctype = getattr(doc_or_doctype, "doctype", "?")
            try:
                allowed = doc_or_doctype.has_permission(ptype)
            except Exception as e:
                self.logger.debug(
                    "doc.has_permission(%s) raised %s; treating as denied", ptype, e
                )
                allowed = False

        if allowed:
            return

        user = getattr(getattr(frappe, "session", None), "user", None)
        if self.permission_mode == "warn":
            self.logger.warning(
                "[would-deny] permission %s on %s for user %s (mode=warn)",
                ptype,
                doctype,
                user,
            )
            return

        raise frappe.PermissionError(f"No {ptype} permission for {doctype}")

    def _handle_missing_tenant_column(self, doctype):
        """
        Handle a fetched document whose table lacks a ``tenant_id`` column
        (tenant ownership cannot be verified). Honors ``tenant_strict``:
        off = silent pass (legacy); warn = log ``[would-deny]`` and proceed;
        enforce = raise ``frappe.PermissionError``.
        """
        if self.tenant_strict == "off":
            return
        if self.tenant_strict == "warn":
            self.logger.warning(
                "[would-deny] tenant isolation unavailable for %s: table lacks a "
                "tenant_id column (mode=warn)",
                doctype,
            )
            return
        raise frappe.PermissionError(
            f"tenant isolation not available for this doctype: {doctype}"
        )

    def _verify_doc_tenant(self, doc, doctype):
        """
        Verify a fetched document belongs to the current tenant.

        No-op in single-tenant mode: there is no tenant to compare against, and
        ``tenant_strict`` is deliberately short-circuited rather than consulted.
        Its whole purpose is to flag tables *missing* a ``tenant_id`` column,
        which on a stock site is every table — under the default ``warn`` that
        would log on every read, and under ``enforce`` it would make
        single-tenant mode impossible.
        """
        if not self.multi_tenant:
            return

        if hasattr(doc, "tenant_id"):
            tenant_id = self.get_tenant_id()
            if doc.tenant_id != tenant_id:
                raise TenantAccessError(
                    "Access denied: Document does not belong to current tenant"
                )
        else:
            # Gap A: the table has no tenant_id column, so ownership
            # cannot be verified. Fail closed under enforce instead of
            # silently passing.
            self._handle_missing_tenant_column(doctype)

    def _require_query_tenant_id(self):
        """
        Resolve the tenant to scope a query by, or raise.

        Only reached in multi-tenant mode; a missing or SYSTEM tenant there is a
        deployment error, not a case to degrade into.
        """
        tenant_id = self.get_tenant_id()

        if not tenant_id:
            raise ValueError(
                "No tenant_id found in context. Cannot query without tenant isolation."
            )

        if tenant_id == "SYSTEM":
            raise ValueError("SYSTEM tenant_id is not allowed")

        return tenant_id

    def _add_tenant_filter(self, filters):
        """
        Inject the current tenant_id into the filters so queries are always
        tenant-scoped. Accepts None, dict, list, or str (document name).
        Raises ValueError if tenant_id is missing, SYSTEM, or filters type
        is unsupported.

        In single-tenant mode (``MULTI_TENANT=0``) filters are returned as given
        — a bare document name is still normalized to ``{"name": ...}`` because
        callers pass the result straight to Frappe, which cannot take a bare
        name. Unsupported filter types are not rejected here in that mode;
        Frappe raises on them downstream.
        """
        if not self.multi_tenant:
            return {"name": filters} if isinstance(filters, str) else filters

        tenant_id = self._require_query_tenant_id()

        if filters is None:
            return {"tenant_id": tenant_id}

        if isinstance(filters, str):
            return {"name": filters, "tenant_id": tenant_id}

        if isinstance(filters, dict):
            filters = filters.copy()
            filters["tenant_id"] = tenant_id
            return filters

        if isinstance(filters, list):
            filters = filters.copy()
            filters.append(["tenant_id", "=", tenant_id])
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
        with (
            _otel_tracer.start_as_current_span(f"db.get_all.{doctype}")
            if _otel_tracer
            else NullContext()
        ) as span:
            if _otel_tracer and span:
                span.set_attribute("db.system", "mariadb")
                span.set_attribute("db.operation", "select")
                span.set_attribute("db.target", doctype)

            self._check_permission(doctype, "read")
            filters = self._add_tenant_filter(filters)
            return frappe.get_all(doctype, filters=filters, **kwargs)

    def get_list(self, doctype, filters=None, **kwargs):
        """Alias for get_all; same behaviour with tenant filter applied."""
        return self.get_all(doctype, filters=filters, **kwargs)

    def get_doc(
        self, doctype, name=None, verify_tenant=True, check_permission=True, **kwargs
    ):
        """
        Get a document and verify tenant ownership

        Args:
            doctype: DocType name
            name: Document name
            verify_tenant: If True, verifies tenant_id matches current tenant
            check_permission: If True, apply the PERMISSION_MODE read check.
                Internal write paths (set_value/update_doc/delete_doc) pass
                False because the document model enforces write permission and
                a duplicate read check would double-log in warn mode.

        Usage:
            user = db.get_doc('User', 'USER-001')
        """
        with (
            _otel_tracer.start_as_current_span(f"db.get_doc.{doctype}")
            if _otel_tracer
            else NullContext()
        ) as span:
            if _otel_tracer and span:
                span.set_attribute("db.system", "mariadb")
                span.set_attribute("db.operation", "select")
                span.set_attribute("db.target", doctype)
                if name:
                    span.set_attribute("db.document.name", name)

            doc = (
                frappe.get_doc(doctype, name, **kwargs)
                if name
                else frappe.get_doc(doctype, **kwargs)
            )

            if verify_tenant:
                self._verify_doc_tenant(doc, doctype)

            # Role-permission read check (after tenant-ownership so cross-tenant
            # probes still see the tenant error first).
            if check_permission:
                self._check_permission(doc, "read")

            return doc

    def count(self, doctype, filters=None):
        """Count documents matching filters, always scoped to current tenant."""
        self._check_permission(doctype, "read")
        filters = self._add_tenant_filter(filters)
        return frappe.db.count(doctype, filters=filters)

    def exists(self, doctype, filters):
        """Return True if a document exists matching filters in the current tenant."""
        self._check_permission(doctype, "read")
        filters = self._add_tenant_filter(filters)
        return frappe.db.exists(doctype, filters)

    def find_by_label(self, doctype, label, label_fields):
        """Docname of THIS tenant's row whose label matches, else None.

        Master doctypes are tenant-scoped by docname (``<tenant8>-<slug>``), so a
        caller holding a user-facing label cannot look the row up by name. It
        also may not know which field carries the label — an Item can be found by
        ``item_code`` or ``item_name`` — hence the ordered ``label_fields``.

        Exists so services stop reaching into ``frappe_microservice.naming``'s
        private ``_claimable_row`` for this. Tenant scoping is the library's job;
        a controller importing an underscore-prefixed internal to get it is the
        coupling this method removes.

        Never returns another tenant's row or an unowned one: every lookup goes
        through get_all, and _add_tenant_filter raises when the tenant is missing
        or SYSTEM.

        Args:
            doctype: e.g. "Item"
            label: the user-facing value, e.g. "Travel"
            label_fields: fields to try in order, e.g. ["item_code", "item_name"]

        Returns:
            The docname, or None when this tenant has no matching row.
        """
        if not label:
            return None
        if isinstance(label_fields, str):
            label_fields = [label_fields]

        for field in label_fields:
            try:
                rows = self.get_all(
                    doctype, filters={field: label}, fields=["name"], limit=1
                )
            except Exception as exc:
                self.logger.debug(
                    "find_by_label %s.%s=%r: %s", doctype, field, label, exc
                )
                continue
            if not rows:
                continue
            row = rows[0]
            name = (
                row.get("name") if isinstance(row, dict) else getattr(row, "name", None)
            )
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def get_value(self, doctype, filters, fieldname, **kwargs):
        """Get a single field value with tenant filter. filters can be dict or name string."""
        self._check_permission(doctype, "read")
        if isinstance(filters, str):
            filters = {"name": filters}
        filters = self._add_tenant_filter(filters)
        return frappe.db.get_value(doctype, filters, fieldname, **kwargs)

    def set_value(self, doctype, name, fieldname, value=None, **kwargs):
        """Update a single field; verifies tenant ownership + write permission first."""
        doc = self.get_doc(doctype, name, verify_tenant=True, check_permission=False)
        if not doc:
            raise frappe.PermissionError(
                f"Document {doctype} {name} not found or belongs to another tenant"
            )
        # set_value bypasses the document model, so enforce write permission
        # explicitly (after the tenant check inside get_doc).
        self._check_permission(doc, "write")
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
        # Single-tenant mode has no tenant to validate against; the caller-writes-
        # its-own-filter contract documented above is unchanged.
        if self.multi_tenant:
            tenant_id = self.get_tenant_id()

            if not tenant_id:
                raise ValueError(
                    "No tenant_id found in context. Cannot execute SQL query without tenant isolation.\n"
                    "Ensure get_tenant_id_func is properly configured and user is authenticated."
                )

        # Raw SQL bypasses role-permission checks by design; surface it for
        # audits when enforcing.
        if self.permission_mode == "enforce":
            self.logger.debug(
                "raw SQL call bypassed permission checks (permission_mode=enforce)"
            )

        return frappe.db.sql(query, values=values, **kwargs)

    def commit(self):
        """Commit the current Frappe DB transaction."""
        if not getattr(frappe, "db", None):
            self.logger.warning("commit() called but frappe.db is not available")
            return
        return frappe.db.commit()

    def rollback(self):
        """Roll back the current Frappe DB transaction."""
        if not getattr(frappe, "db", None):
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
        tenant_id = self._resolve_write_tenant_id()

        doc_dict = {"doctype": doctype, **kwargs}
        if tenant_id:
            doc_dict["tenant_id"] = tenant_id

        doc = frappe.get_doc(doc_dict)
        self.hooks.run_hooks(doc, "before_validate", raise_on_error=True)
        return doc

    def _resolve_write_tenant_id(self):
        """
        Tenant to stamp on a new document, or None in single-tenant mode.

        Returning None rather than raising is what lets writes work outside a
        request context at all — notably background jobs, where Flask's ``g``
        is unavailable.
        """
        if not self.multi_tenant:
            return None

        tenant_id = self.get_tenant_id()
        self.logger.debug(f"Retrieved tenant_id: {tenant_id}")

        if not tenant_id:
            error_msg = (
                "No tenant_id in context. Cannot create document without tenant isolation."
            )
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        return tenant_id

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
            to Frappe's doc.insert() method. However, use with extreme caution:

            APPROPRIATE USAGE:
            - During signup/initialization: When no user context exists yet (e.g., creating the first admin).
            - System-level operations: Migrations, scheduled tasks, or automated background workers.
            - Internal state management: Updating records where the user has no direct control.

            INAPPROPRIATE USAGE:
            - Regular business operations: Creating orders, customers, or items initiated by a user.
            - Public microservice endpoints: API calls from external systems
              should generally be authenticated and authorized.
            - User-initiated actions: Bypassing permissions often indicates a missing role assignment.

            BEST PRACTICE:
            Instead of using ignore_permissions, ensure users are assigned proper roles during
            signup (System Manager, Sales User, Stock User, etc.) and create explicit
            User Permission records for the Company and Customer they own. This allows
            Frappe's permission system to work correctly if the user ever logs into the Desk.

        Usage:
            doc = db.insert_doc('Sales Order', {'customer': 'CUST-001'})
            doc = db.insert_doc('Sales Order', customer='CUST-001', items=[...])
            doc = db.insert_doc(
                'User Permission', {'user': 'admin@example.com', 'allow': 'Company'},
                ignore_permissions=True)
        """
        self.logger.debug(f"TenantAwareDB.insert_doc() called for doctype: {doctype}")
        tenant_id = self._resolve_write_tenant_id()

        insert_params = {}
        doc_fields = data.copy() if data else {}

        insert_param_names = {
            "ignore_permissions",
            "ignore_links",
            "ignore_if_duplicate",
            "ignore_mandatory",
            "set_name",
            "set_child_names",
        }

        for key in list(kwargs.keys()):
            if key in insert_param_names:
                insert_params[key] = kwargs.pop(key)

        doc_fields.update(kwargs)
        self.logger.debug(f"Document fields to be set: {list(doc_fields.keys())}")

        if (
            tenant_id
            and "tenant_id" in doc_fields
            and doc_fields["tenant_id"] != tenant_id
        ):
            raise TenantAccessError(
                "Cannot create document for different tenant. "
                f"Current: {tenant_id}, Requested: {doc_fields['tenant_id']}"
            )

        # Tenant-scoped docnames exist to keep two tenants' masters from
        # colliding on one name. With no tenancy there is nothing to scope, so
        # leave naming to Frappe rather than feeding it a raw display label.
        label_val = None
        if (
            tenant_id
            and doctype in FIELD_AUTONAMED_MASTER_DOCTYPES
            and "set_name" not in insert_params
        ):
            label_field, fallback_slug = FIELD_AUTONAMED_MASTER_DOCTYPES[doctype]
            label_val = doc_fields.get(label_field) or doc_fields.get("name")
            if label_val:
                from frappe_microservice.naming import (
                    _claimable_row,
                    tenant_scoped_docname,
                    _next_free_docname,
                )

                existing_name = _claimable_row(
                    doctype, label_field, str(label_val), self
                )
                if existing_name:
                    return self.get_doc(doctype, existing_name)

                base_name = tenant_scoped_docname(
                    str(label_val),
                    get_tenant_id=self.get_tenant_id,
                    fallback=fallback_slug,
                )
                docname = _next_free_docname(doctype, base_name)
                insert_params["set_name"] = docname

        doc_dict = {"doctype": doctype, **doc_fields}
        if tenant_id:
            doc_dict["tenant_id"] = tenant_id

        self.logger.debug(f"Creating document with tenant_id: {tenant_id}")
        doc = frappe.get_doc(doc_dict)

        # CRITICAL: Set tenant_id directly on doc object
        # This is required when tenant_id column was added via ALTER TABLE
        # (not in DocType metadata). Frappe's valid_columns only includes
        # fields from metadata, so fields added via ALTER TABLE won't be saved
        # during insert() unless set directly on the doc object.
        #
        # Skipped in single-tenant mode: a stock site has no such column, and
        # setting it would put tenant_id into the INSERT and fail with 1054.
        if tenant_id:
            doc.tenant_id = tenant_id

        if run_hooks:
            self.hooks.run_hooks(doc, "before_validate")
            self.hooks.run_hooks(doc, "before_insert")

        with (
            _otel_tracer.start_as_current_span(f"db.insert.{doctype}")
            if _otel_tracer
            else NullContext()
        ) as span:
            if _otel_tracer and span:
                span.set_attribute("db.system", "mariadb")
                span.set_attribute("db.operation", "insert")
                span.set_attribute("db.target", doctype)

            self.logger.debug(
                f"Calling doc.insert() for {doctype} with params: {insert_params}"
            )
            doc.insert(**insert_params)
            self.logger.debug(
                f"doc.insert() completed. Document name: {getattr(doc, 'name', 'N/A')}"
            )

            if doctype in FIELD_AUTONAMED_MASTER_DOCTYPES and label_val:
                from frappe_microservice.naming import _restore_display_label

                label_field = FIELD_AUTONAMED_MASTER_DOCTYPES[doctype][0]
                docname = getattr(doc, "name", insert_params.get("set_name"))
                setattr(doc, label_field, str(label_val))
                _restore_display_label(
                    doctype, docname, label_field, str(label_val), self
                )
                if docname:
                    frappe.clear_document_cache(doctype, docname)

            if _otel_tracer and span and hasattr(doc, "name"):
                span.set_attribute("db.document.name", doc.name)

        if (
            tenant_id
            and self.verify_tenant_on_insert
            and hasattr(doc, "name")
            and doc.name
        ):
            saved_tenant = frappe.db.get_value(doctype, doc.name, "tenant_id")

            if saved_tenant != tenant_id:
                error_msg = (
                    f"CRITICAL: tenant_id mismatch after insert! "
                    f"Expected: {tenant_id}, Got: {saved_tenant}"
                )
                self.logger.error(error_msg)
                raise ValueError(error_msg)

            self.logger.debug(f"Tenant isolation verified for {doctype}/{doc.name}")

        if run_hooks:
            self.hooks.run_hooks(doc, "after_insert")

        return doc

    def update_doc(self, doctype, name, data, run_hooks=True, **kwargs):
        """
        Update document with hooks and tenant verification

        Hook execution order:
        1. [GET DOC - verify tenant]
        2. before_update (custom)
        3. doc.update(data)
        4. before_validate (custom) — same as insert_doc so e.g. Purchase Invoice can resolve/create Supplier
        5. validate / before_save / on_update (Frappe + custom via doc.save())
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
        doc = self.get_doc(doctype, name, verify_tenant=True, check_permission=False)

        if not data or not isinstance(data, dict):
            raise ValueError("update_doc requires a non-empty dict of fields to update")

        if "tenant_id" in data:
            data = data.copy()
            del data["tenant_id"]

        if run_hooks:
            self.hooks.run_hooks(doc, "before_update")

        doc.update(data)

        if run_hooks:
            self.hooks.run_hooks(doc, "before_validate")

        doc.save(**kwargs)

        if run_hooks:
            self.hooks.run_hooks(doc, "after_update")

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
            doc = self.get_doc(
                doctype, name, verify_tenant=verify_tenant, check_permission=False
            )

            if run_hooks:
                self.hooks.run_hooks(doc, "before_delete")

        if doc is not None:
            doc.delete(**kwargs)
        else:
            frappe.delete_doc(doctype, name, **kwargs)

        if run_hooks and doc:
            self.hooks.run_hooks(doc, "after_delete")

    # ============================================
    # HOOK REGISTRATION METHODS
    # ============================================

    def on(self, doctype: str = "*", event: str = "before_insert"):
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

    def before_validate(self, doctype: str = "*"):
        """Decorator to register a before_validate hook for the given doctype."""
        return self.hooks.decorator(doctype, "before_validate")

    def before_insert(self, doctype: str = "*"):
        """Decorator to register a before_insert hook for the given doctype."""
        return self.hooks.decorator(doctype, "before_insert")

    def after_insert(self, doctype: str = "*"):
        """Decorator to register an after_insert hook for the given doctype."""
        return self.hooks.decorator(doctype, "after_insert")

    def before_update(self, doctype: str = "*"):
        """Decorator to register a before_update hook for the given doctype."""
        return self.hooks.decorator(doctype, "before_update")

    def after_update(self, doctype: str = "*"):
        """Decorator to register an after_update hook for the given doctype."""
        return self.hooks.decorator(doctype, "after_update")

    def before_delete(self, doctype: str = "*"):
        """Decorator to register a before_delete hook for the given doctype."""
        return self.hooks.decorator(doctype, "before_delete")

    def after_delete(self, doctype: str = "*"):
        """Decorator to register an after_delete hook for the given doctype."""
        return self.hooks.decorator(doctype, "after_delete")

    def list_hooks(self) -> Dict[str, Dict[str, List[str]]]:
        """
        List all registered hooks (for debugging)

        Returns:
            Dict with structure: {doctype: {event: [handler_names]}}
        """
        return self.hooks.list_hooks()
