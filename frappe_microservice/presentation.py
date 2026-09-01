"""Hide tenant-scoped docnames behind the labels callers work in.

``TenantAwareDB.insert_doc`` renames the masters in
``FIELD_AUTONAMED_MASTER_DOCTYPES`` to ``<tenant8>-<slug>`` so that
``tab<Doctype>.name`` can stay globally unique across tenants. That prefix is a
storage detail: this API is already tenant-scoped by session, so the docname
carries nothing a caller can act on. Leaking it is not cosmetic — clients
reconcile a saved document against what they submitted, so an echoed
``item_code`` that no longer matches the one they sent turns a committed write
into a client-side error.

Presentation is therefore the exact inverse of the rename, and lives next to it:
whatever this library renames on the way in, it presents on the way out.

Two rules keep this safe:

* Only **Link values** are translated — a reference *to* a renamed master. A
  document's own ``name`` is its address (``GET /api/resource/Item/<name>``) and
  is never touched.
* The label used is the master's mapped field (``Item.item_code``, not
  ``item_name``), because that is what ``_claimable_row`` resolves against on the
  way back in. That is what makes the round-trip closed: whatever a caller is
  handed can be sent back unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

import frappe

from frappe_microservice.tenant import FIELD_AUTONAMED_MASTER_DOCTYPES

logger = logging.getLogger(__name__)


def _get(row: Any, field: str):
    if isinstance(row, dict):
        return row.get(field)
    return getattr(row, field, None)


def _set(row: Any, field: str, value) -> None:
    if isinstance(row, dict):
        row[field] = value
    else:
        setattr(row, field, value)


def _linked_master_fields(doctype: str):
    """(fieldname, target_doctype) for Link fields pointing at a renamed master."""
    out = []
    try:
        meta = frappe.get_meta(doctype)
    except Exception:
        return out
    for df in getattr(meta, "fields", None) or []:
        if _get(df, "fieldtype") != "Link":
            continue
        target = str(_get(df, "options") or "").strip()
        if target in FIELD_AUTONAMED_MASTER_DOCTYPES:
            out.append((_get(df, "fieldname"), target))
    return out


def _child_table_fields(doctype: str):
    """(fieldname, child_doctype) for this doctype's child tables."""
    out = []
    try:
        meta = frappe.get_meta(doctype)
    except Exception:
        return out
    for df in getattr(meta, "fields", None) or []:
        if _get(df, "fieldtype") in ("Table", "Table MultiSelect"):
            child = str(_get(df, "options") or "").strip()
            if child:
                out.append((_get(df, "fieldname"), child))
    return out


def _rows_of(doc: Any, fieldname: str):
    value = _get(doc, fieldname)
    if not value:
        return []
    return value if isinstance(value, (list, tuple)) else []


def _collect(
    doc: Any, doctype: str, wanted: dict, targets: list, depth: int = 0
) -> None:
    """Gather every docname referenced through a renamed-master Link."""
    if doc is None or depth > 2:
        return
    for fieldname, target in _linked_master_fields(doctype):
        value = _get(doc, fieldname)
        if isinstance(value, str) and value.strip():
            wanted.setdefault(target, set()).add(value.strip())
            targets.append((doc, fieldname, target))
    for fieldname, child_doctype in _child_table_fields(doctype):
        for row in _rows_of(doc, fieldname):
            _collect(row, child_doctype, wanted, targets, depth + 1)


def _labels_for(target_doctype: str, docnames, tenant_db) -> dict:
    label_field = FIELD_AUTONAMED_MASTER_DOCTYPES[target_doctype][0]
    names = sorted(docnames)
    db = tenant_db if tenant_db is not None else frappe
    try:
        rows = (
            db.get_all(
                target_doctype,
                filters={"name": ("in", names)},
                fields=["name", label_field],
            )
            or []
        )
    except Exception as exc:
        logger.warning("label lookup failed for %s %s: %s", target_doctype, names, exc)
        return {}
    labels = {}
    for row in rows:
        docname = str(_get(row, "name") or "").strip()
        label = str(_get(row, label_field) or "").strip()
        if docname and label:
            labels[docname] = label
    return labels


def present_documents(doctype: str, docs, tenant_db=None):
    """Present a whole page of documents with one label lookup per target doctype.

    A list route resolves the same handful of masters over and over, so collecting
    across the page first keeps the query count flat in page size instead of doing
    a ``get_all`` per row per target.

    Never raises: a presentation failure must not cost the caller the documents.

    Inert in single-tenant mode. This is the inverse of the tenant-scoped rename
    insert_doc performs; with no rename happening there is nothing to undo, and
    rewriting Link values to labels would break the round-trip — a caller would
    read back "Acme Corp" where the stored value is "CUST-00001".
    """
    if not doctype or not docs:
        return docs
    if tenant_db is not None and not getattr(tenant_db, "multi_tenant", True):
        return docs
    try:
        wanted: dict = {}
        targets: list = []
        for doc in docs:
            _collect(doc, doctype, wanted, targets)
        if not targets:
            return docs
        labels = {t: _labels_for(t, names, tenant_db) for t, names in wanted.items()}
        for row, fieldname, target in targets:
            label = labels.get(target, {}).get(str(_get(row, fieldname) or "").strip())
            if label:
                _set(row, fieldname, label)
    except Exception:
        logger.warning("link presentation failed for %s", doctype, exc_info=True)
    return docs


def present_document(doctype: str, doc: Any, tenant_db=None):
    """Replace renamed-master Link values with their labels, in place.

    Never raises: a presentation failure must not cost the caller the document.
    """
    if doc is None or not doctype:
        return doc
    present_documents(doctype, [doc], tenant_db)
    return doc
