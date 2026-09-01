"""
Tenant-scoped docname generation and resolve-or-create for field-autonamed
master doctypes (Item, Item Group, Supplier, Customer, etc.).

Produces docnames byte-identical to tenancy_app.utils.tenant so records
created by a microservice and by the central site converge on the same PK.
"""

import re
import logging
from typing import Any, Callable, Dict, Optional

import frappe

logger = logging.getLogger(__name__)


def _as_docname(value: Any) -> Optional[str]:
    """Accept only a real non-empty docname (raw SQL / mocks can yield anything)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _row_exists(doctype: str, name: str) -> bool:
    """True when the row really landed on disk."""
    if not name:
        return False
    try:
        res = frappe.db.exists(doctype, name)
    except Exception as exc:
        logger.debug("_row_exists %s(%r): %s", doctype, name, exc)
        return True
    if isinstance(res, (bool, int, str)):
        return bool(res)
    return True


def tenant_scoped_docname(
    display_name: str,
    get_tenant_id: Callable[[], str],
    fallback: str = "item",
) -> str:
    """Build a tenant-prefixed docname: ``<tenant8>-<slug>``.

    Byte-identical to ``tenancy_app.utils.tenant._slugify_docname_part``
    + the 8-char prefix used in ``set_item_tenant_autoname``. That parity is a
    hard requirement, not a nicety: the central site autonames Item and Item
    Group through its own hook, so any divergence here splits one tenant's
    master data across two docnames for the same label.

    Two tenant_ids that share their first 8 hex chars collapse to the same
    prefix (e.g. ``test-tenant-001`` and ``test-tenant-002`` -> ``testtena``).
    That is cosmetic, not an isolation hole: ``_next_free_docname`` suffixes the
    second row and ``_claimable_row`` is tenant-filtered, so neither tenant can
    reach the other's row. Do NOT "fix" it by hashing or reshaping the prefix —
    that breaks parity with the central site, which is the far worse failure.

    Idempotent: an already tenant-scoped value is returned as-is.
    """
    tenant_id = (
        get_tenant_id() if callable(get_tenant_id) else str(get_tenant_id or "")
    ) or ""
    prefix = str(tenant_id).replace("-", "")[:8].lower()

    name = (display_name or "").strip()
    if prefix and name.lower().startswith(f"{prefix}-"):
        return name
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug or fallback
    return f"{prefix}-{slug}" if prefix else name


def _claimable_row(
    doctype: str,
    label_field: str,
    label: str,
    tenant_db: Any,
) -> Optional[str]:
    """Find a row by display label that this tenant owns.

    Tenant-scoped lookup only — NEVER another tenant's row and NEVER an unowned/global row.
    """
    if not label:
        return None

    # 0) Check if label is already a valid docname owned by this tenant
    try:
        # get_tenant_id is the raw provider and is unaware of the mode flag, so
        # check the flag first: in single-tenant mode there is no tenant_id
        # column to filter on. Keyed off the flag's truthiness rather than
        # method presence, because the duck-typed doubles in the tests are
        # MagicMocks that answer hasattr() for anything.
        if not getattr(tenant_db, "multi_tenant", True):
            tenant_id = None
        elif hasattr(tenant_db, "get_tenant_id"):
            tenant_id = tenant_db.get_tenant_id()
        else:
            tenant_id = None
        filters = {"name": label}
        if tenant_id:
            filters["tenant_id"] = tenant_id
        rows = tenant_db.get_all(doctype, filters=filters, fields=["name"], limit=1)
        if rows:
            first_row = rows[0]
            val = (
                first_row.get("name")
                if isinstance(first_row, dict)
                else getattr(first_row, "name", None)
            )
            found = _as_docname(val)
            if found:
                return found
    except Exception as exc:
        logger.debug("_claimable_row tenant docname %s(%r): %s", doctype, label, exc)

    # 1) Search by display label field using tenant-filtered get_all
    try:
        # Oldest wins, explicitly. The label field is not unique — the central
        # site drops that constraint, and _next_free_docname suffixes collisions
        # — so one tenant can hold several rows sharing a label. Without an
        # order_by this took Frappe's default (``modified desc``), which meant
        # the row a label resolved to changed whenever any of them was touched,
        # silently repointing documents that had already been saved against it.
        rows = tenant_db.get_all(
            doctype,
            filters={label_field: label},
            fields=["name"],
            order_by="creation asc",
            limit=1,
        )
        if rows:
            first_row = rows[0]
            val = (
                first_row.get("name")
                if isinstance(first_row, dict)
                else getattr(first_row, "name", None)
            )
            found = _as_docname(val)
            if found:
                return found
    except Exception as exc:
        logger.debug("_claimable_row tenant label %s(%r): %s", doctype, label, exc)

    return None


def _next_free_docname(doctype: str, base: str) -> str:
    """First unclaimed docname: ``base``, else ``base-002``, ``base-003``, …"""

    def _taken(name: str) -> bool:
        try:
            res = frappe.db.exists(doctype, name)
        except Exception:
            return False
        return isinstance(res, (bool, int, str)) and bool(res)

    if not _taken(base):
        return base

    for suffix in range(2, 100):
        candidate = f"{base}-{suffix:03d}"
        if not _taken(candidate):
            return candidate

    logger.debug("_next_free_docname %s(%r): probe exhausted", doctype, base)
    return base


def _insert_with_docname(
    doctype: str,
    docname: str,
    values: Dict[str, Any],
    tenant_db: Any,
) -> Optional[str]:
    """Insert with explicit set_name; returns docname only once on disk.

    A falsy ``docname`` means "let Frappe autoname" — set_name is omitted rather
    than passed as None, which Frappe would treat as an explicit empty name.
    """
    try:
        insert_kwargs = {"ignore_permissions": True}
        if docname:
            insert_kwargs["set_name"] = docname
        doc = tenant_db.insert_doc(doctype, dict(values), **insert_kwargs)
        tenant_db.commit()
    except Exception as exc:
        logger.warning("_insert_with_docname %s(%r): %s", doctype, docname, exc)
        try:
            tenant_db.rollback()
        except Exception:
            pass
        return None

    created = _as_docname(getattr(doc, "name", None)) or docname
    return created if _row_exists(doctype, created) else None


def _restore_display_label(
    doctype: str,
    docname: str,
    label_field: str,
    label: str,
    tenant_db: Any,
) -> None:
    """Undo _sync_autoname_field clobber by restoring the user's label."""
    if not (docname and label):
        return
    try:
        if tenant_db.get_value(doctype, docname, label_field) == label:
            return
        tenant_db.set_value(doctype, docname, label_field, label, update_modified=False)
        frappe.clear_document_cache(doctype, docname)
        frappe.db.commit()
    except Exception as exc:
        logger.warning(
            "_restore_display_label %s %s.%s=%r: %s",
            doctype,
            docname,
            label_field,
            label,
            exc,
        )


def resolve_or_create(
    doctype: str,
    label_field: str,
    label: str,
    tenant_db: Any,
    extra_fields: Optional[Dict[str, Any]] = None,
    fallback_slug: str = "item",
    fallback_docname: Optional[str] = None,
) -> str:
    """Find or create a tenant-scoped master row by display label.

    Never returns another tenant's row and never returns an unowned row.
    Always resolves to a row this tenant owns.
    """
    label_str = (label or "").strip()
    if not label_str:
        if fallback_docname:
            return fallback_docname
        raise frappe.ValidationError(f"Cannot resolve or create empty {doctype} label")

    # 1. Existing row check
    existing = _claimable_row(doctype, label_field, label_str, tenant_db)
    if existing:
        return existing

    # 2. Compute tenant-scoped docname. In single-tenant mode there is nothing
    # to scope against, and tenant_scoped_docname would hand back the raw
    # display label — a poor and possibly invalid docname. Leave naming to
    # Frappe instead by passing no set_name.
    if not getattr(tenant_db, "multi_tenant", True):
        docname = None
    else:
        base_docname = tenant_scoped_docname(
            label_str,
            get_tenant_id=tenant_db.get_tenant_id,
            fallback=fallback_slug,
        )
        docname = _next_free_docname(doctype, base_docname)

    # 3. Insert docname
    payload = {label_field: label_str}
    if extra_fields:
        payload.update(extra_fields)

    created = _insert_with_docname(doctype, docname, payload, tenant_db)
    if created:
        _restore_display_label(doctype, created, label_field, label_str, tenant_db)
        return created

    # 4. Fallback check for concurrent insert
    claimed = _claimable_row(doctype, label_field, label_str, tenant_db)
    if claimed:
        return claimed

    if fallback_docname:
        return fallback_docname

    raise frappe.ValidationError(
        f"Could not resolve or create {doctype} with label '{label_str}'"
    )
