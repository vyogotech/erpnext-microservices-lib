"""
Resource registration mixin -- auto-generates RESTful CRUD endpoints.

register_resource(doctype, ...) creates Frappe-style /api/resource/{doctype} routes:
- GET list: query params -> filters, tenant_db.get_all, returns {data, doctype}.
- GET one: path name -> tenant_db.get_doc, returns doc.as_dict() or 403/404.
- POST: request.json -> tenant_db.insert_doc, returns 201 with name.
- PUT: path name + request.json -> tenant_db.update_doc.
- DELETE: path name -> tenant_db.delete_doc.

All routes are registered via secure_route so they require authentication and
tenant context. Optional custom_handlers can override list/get/post/put/delete.
Swagger docstrings are attached for /apidocs.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import unquote_plus
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import frappe
from flask import request

from frappe_microservice.tenant import TenantAccessError

# GET /api/resource/{doctype} — query keys that are Frappe list/pagination/meta, not DocType columns.
# If these leak into ``filters``, SQL fails (e.g. Unknown column 'limit_page_length' in 'WHERE').
_RESOURCE_LIST_META_KEYS = frozenset(
    {
        "fields",
        "limit",
        "offset",
        "order_by",
        "limit_page_length",
        "limit_start",
        "filters",
        "or_filters",
        "distinct",
        "ignore_permissions",
        "save_user_settings",
        "with_childnames",
        "parent",
        "parenttype",
        "parentfield",
        "group_by",
        "as_list",
        "debug",
    }
)


def _format_timedelta_safe(obj: timedelta) -> str:
    """Match Frappe desk/API duration strings (see frappe.utils.response.json_handler)."""
    try:
        from frappe.utils import format_timedelta

        out = format_timedelta(obj)
        return out if isinstance(out, str) else str(out)
    except Exception:
        return str(obj)


def _parse_fields_query_param(raw: str) -> list[str]:
    """Parse Frappe-style ``fields`` query: JSON array or comma-separated column names."""
    raw = unquote_plus((raw or "").strip())
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
        return [p.strip() for p in raw.split(",") if p.strip()]
    if "," in raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [raw]


def _parse_structured_filters_query_param(raw: str):
    """Parse a Frappe-style ``filters`` / ``or_filters`` query param.

    Accepts the JSON shapes Frappe's list API sends: a dict (``{field: value}`` or
    ``{field: [op, value]}``) or a list of conditions (``[[field, op, value], ...]``
    or ``[[doctype, field, op, value], ...]``). Returns the parsed dict/list, or
    ``None`` when the param is absent or not valid JSON.

    Werkzeug already URL-decodes query values once; a second ``unquote_plus`` pass is
    tried only if the first parse fails, so double-encoded clients still work without
    mangling ``+`` inside ordinary values.
    """
    if not raw:
        return None
    candidates = [raw]
    unquoted = unquote_plus(raw)
    if unquoted != raw:
        candidates.append(unquoted)
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _merge_resource_filters(flat: dict, structured):
    """Combine flat ``?field=value`` params with a structured ``filters`` param.

    Both are AND conditions. A structured dict merges key-wise (structured wins on
    conflict); a structured list is extended with the flat params rendered as
    ``[field, "=", value]`` conditions so the two encodings compose.
    """
    if isinstance(structured, dict):
        merged = dict(flat)
        merged.update(structured)
        return merged
    if isinstance(structured, list):
        conditions = [[key, "=", value] for key, value in flat.items()]
        conditions.extend(structured)
        return conditions
    return flat


def _make_json_safe(obj):
    """Recursively normalize values for JSON (GET list/one: timedelta, Decimal, Mapping, etc.)."""
    # bool before int: bool is a subclass of int in Python
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float, str)):
        return obj
    if isinstance(obj, timedelta):
        return _format_timedelta_safe(obj)
    if isinstance(obj, (date, datetime, time)):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    if isinstance(obj, set):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, Mapping):
        return {str(k): _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, Sequence):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, Iterable) and not isinstance(obj, (str, bytes, bytearray)):
        return [_make_json_safe(v) for v in obj]
    return str(obj)


def _doc_as_json_str(doc_dict: dict) -> str:
    """Serialize a document dict for GET /api/resource/... responses.

    Do not use frappe.as_json here: some Frappe stacks still raise on nested values
    (e.g. timedelta) despite json_handler; stdlib json with default=str is reliable.
    """
    safe = _make_json_safe(doc_dict)
    return json.dumps(safe, default=str, indent=1, sort_keys=True, ensure_ascii=True)


class ResourceMixin:
    """
    Mixin for MicroserviceApp that provides register_resource().

    Expects the host class to have:
        self.tenant_db: TenantAwareDB
        self.secure_route(rule, **options): method
        self.flask_app: Flask
        self.logger: logging.Logger
    """

    def register_resource(
        self, doctype, base_path=None, methods=None, custom_handlers=None
    ):
        """
        Register RESTful CRUD routes for a DocType. Each enabled method (GET/POST/PUT/DELETE)
        is implemented using tenant_db so all data is tenant-scoped. List GET
        parses request.args for fields, limit / limit_page_length,
        offset / limit_start, order_by and passes the rest as filters (Frappe
        meta keys are never treated as columns).
        Create/update/delete use request.json. All routes are secured and get (user) as
        first argument.         custom_handlers: {'list'|'get'|'post'|'put'|'delete': callable}.
        """
        base_path = base_path or "/api/resource"
        methods = methods or ["GET", "POST", "PUT", "DELETE"]
        custom_handlers = custom_handlers or {}

        # Record for the startup tenant_id-column diagnostic.
        if hasattr(self, "_registered_resources"):
            self._registered_resources.add(doctype)

        doctype_url = doctype.lower().replace(" ", "-")

        if "GET" in methods:
            if "list" in custom_handlers:
                list_handler = custom_handlers["list"]
            else:

                def make_list_handler(dt):
                    def handler(user):
                        from frappe_microservice.presentation import present_documents

                        filters = {}
                        for key, value in request.args.items():
                            if key not in _RESOURCE_LIST_META_KEYS:
                                filters[key] = value

                        # Frappe's list API sends predicates in the JSON ``filters``
                        # param, e.g. filters=[["customer","=","X"],["docstatus","=",1]].
                        # It is a meta key (excluded from the flat loop above), so
                        # without parsing it here every predicate was dropped and the
                        # query returned every row the tenant owns (BUG-GW).
                        structured_filters = _parse_structured_filters_query_param(
                            request.args.get("filters")
                        )
                        if structured_filters is not None:
                            filters = _merge_resource_filters(
                                filters, structured_filters
                            )
                        or_filters = _parse_structured_filters_query_param(
                            request.args.get("or_filters")
                        )

                        fields_raw = unquote_plus(
                            (request.args.get("fields") or "").strip()
                        )
                        fields = (
                            _parse_fields_query_param(fields_raw)
                            if fields_raw
                            else None
                        )
                        try:
                            if request.args.get("limit_page_length") is not None:
                                limit = int(request.args.get("limit_page_length"))
                            else:
                                limit = int(request.args.get("limit", 20))
                        except (ValueError, TypeError):
                            limit = 20
                        try:
                            if request.args.get("limit_start") is not None:
                                offset = int(request.args.get("limit_start"))
                            else:
                                offset = int(request.args.get("offset", 0))
                        except (ValueError, TypeError):
                            offset = 0
                        order_by = request.args.get("order_by", "modified desc")

                        get_all_kwargs = dict(
                            filters=filters,
                            fields=fields,
                            limit_start=offset,
                            limit_page_length=limit,
                            order_by=order_by,
                        )
                        if or_filters is not None:
                            get_all_kwargs["or_filters"] = or_filters

                        documents = self.tenant_db.get_all(dt, **get_all_kwargs)
                        # Same contract as the GET handler below, batched over the page.
                        present_documents(dt, documents, self.tenant_db)

                        return _make_json_safe({"data": documents, "doctype": dt})

                    return handler

                list_handler = make_list_handler(doctype)

            endpoint_name = f'list_{doctype_url.replace("-", "_")}'

            list_handler.__doc__ = f"""
            List {doctype} documents
            ---
            tags:
              - {doctype}
            parameters:
              - name: fields
                in: query
                type: string
                description: 'JSON array (Frappe) e.g. ["name","customer"] or comma-separated names'
              - name: limit
                in: query
                type: integer
                default: 20
                description: Number of records to return (alias; Frappe uses limit_page_length)
              - name: limit_page_length
                in: query
                type: integer
                description: Frappe-style page size (if set, overrides limit)
              - name: offset
                in: query
                type: integer
                default: 0
                description: Row offset (alias; Frappe uses limit_start)
              - name: limit_start
                in: query
                type: integer
                description: Frappe-style offset for pagination
              - name: order_by
                in: query
                type: string
                default: modified desc
                description: Sort order
            responses:
              200:
                description: List of {doctype} documents
            """

            # Standard Frappe path (with spaces or %20)
            self.secure_route(
                f"{base_path}/{doctype}", methods=["GET"], endpoint=endpoint_name
            )(list_handler)

            # Hyphenated path for backward compatibility
            if doctype_url != doctype:
                endpoint_name_kebab = f'list_{doctype_url.replace("-", "_")}_kebab'
                self.secure_route(
                    f"{base_path}/{doctype_url}",
                    methods=["GET"],
                    endpoint=endpoint_name_kebab,
                )(list_handler)

            if "get" in custom_handlers:
                get_handler = custom_handlers["get"]
            else:

                def make_get_handler(dt):
                    def handler(user, name):
                        from flask import Response

                        from frappe_microservice.presentation import present_document

                        try:
                            doc = self.tenant_db.get_doc(dt, name)
                            # Internal ``<tenant8>-<slug>`` docnames must not leave
                            # the service; callers round-trip what we hand them.
                            payload = present_document(
                                dt, doc.as_dict(), self.tenant_db
                            )
                            body = _doc_as_json_str(payload)
                            return Response(
                                body, mimetype="application/json", status=200
                            )
                        except TenantAccessError:
                            # Mask cross-tenant access as 404 (identical to a
                            # missing document) when enabled.
                            if getattr(self, "mask_permission_errors", False):
                                return {"error": f"{dt} not found"}, 404
                            return {"error": "Access denied"}, 403
                        except frappe.PermissionError:
                            return {"error": "Access denied"}, 403
                        except frappe.DoesNotExistError:
                            return {"error": f"{dt} not found"}, 404

                    return handler

                get_handler = make_get_handler(doctype)

            endpoint_name = f'get_{doctype_url.replace("-", "_")}'

            get_handler.__doc__ = f"""
            Get a single {doctype} document
            ---
            tags:
              - {doctype}
            parameters:
              - name: name
                in: path
                type: string
                required: true
                description: Document name
            responses:
              200:
                description: {doctype} document details
              404:
                description: Document not found
            """

            # Standard Frappe path (with spaces or %20)
            self.secure_route(
                f"{base_path}/{doctype}/<name>", methods=["GET"], endpoint=endpoint_name
            )(get_handler)

            # Hyphenated path for backward compatibility
            if doctype_url != doctype:
                endpoint_name_kebab = f'get_{doctype_url.replace("-", "_")}_kebab'
                self.secure_route(
                    f"{base_path}/{doctype_url}/<name>",
                    methods=["GET"],
                    endpoint=endpoint_name_kebab,
                )(get_handler)

        if "POST" in methods:
            if "post" in custom_handlers:
                create_handler = custom_handlers["post"]
            else:

                def make_create_handler(dt):
                    def handler(user):
                        data = request.json

                        if not data:
                            return {"error": "Request body required"}, 400

                        doc = self.tenant_db.insert_doc(dt, data)

                        return {"success": True, "doctype": dt, "name": doc.name}, 201

                    return handler

                create_handler = make_create_handler(doctype)

            endpoint_name = f'create_{doctype_url.replace("-", "_")}'

            create_handler.__doc__ = f"""
            Create a new {doctype} document
            ---
            tags:
              - {doctype}
            parameters:
              - name: body
                in: body
                required: true
                schema:
                  type: object
            responses:
              201:
                description: Document created successfully
              400:
                description: Invalid request data
            """

            # Standard Frappe path (with spaces or %20)
            self.secure_route(
                f"{base_path}/{doctype}", methods=["POST"], endpoint=endpoint_name
            )(create_handler)

            # Hyphenated path for backward compatibility
            if doctype_url != doctype:
                endpoint_name_kebab = f'create_{doctype_url.replace("-", "_")}_kebab'
                self.secure_route(
                    f"{base_path}/{doctype_url}",
                    methods=["POST"],
                    endpoint=endpoint_name_kebab,
                )(create_handler)

        if "PUT" in methods:
            if "put" in custom_handlers:
                update_handler = custom_handlers["put"]
            else:

                def make_update_handler(dt):
                    def handler(user, name):
                        try:
                            data = request.json

                            if not data:
                                return {"error": "Request body required"}, 400

                            doc = self.tenant_db.update_doc(dt, name, data)

                            return {"success": True, "doctype": dt, "name": doc.name}
                        except TenantAccessError:
                            if getattr(self, "mask_permission_errors", False):
                                return {"error": f"{dt} not found"}, 404
                            return {"error": "Access denied"}, 403
                        except frappe.PermissionError:
                            return {"error": "Access denied"}, 403
                        except frappe.DoesNotExistError:
                            return {"error": f"{dt} not found"}, 404

                    return handler

                update_handler = make_update_handler(doctype)

            endpoint_name = f'update_{doctype_url.replace("-", "_")}'

            update_handler.__doc__ = f"""
            Update an existing {doctype} document
            ---
            tags:
              - {doctype}
            parameters:
              - name: name
                in: path
                type: string
                required: true
                description: Document name
              - name: body
                in: body
                required: true
                schema:
                  type: object
            responses:
              200:
                description: Document updated successfully
              404:
                description: Document not found
            """

            # Standard Frappe path (with spaces or %20)
            self.secure_route(
                f"{base_path}/{doctype}/<name>", methods=["PUT"], endpoint=endpoint_name
            )(update_handler)

            # Hyphenated path for backward compatibility
            if doctype_url != doctype:
                endpoint_name_kebab = f'update_{doctype_url.replace("-", "_")}_kebab'
                self.secure_route(
                    f"{base_path}/{doctype_url}/<name>",
                    methods=["PUT"],
                    endpoint=endpoint_name_kebab,
                )(update_handler)

        if "DELETE" in methods:
            if "delete" in custom_handlers:
                delete_handler = custom_handlers["delete"]
            else:

                def make_delete_handler(dt):
                    def handler(user, name):
                        try:
                            self.tenant_db.delete_doc(dt, name)
                            return {
                                "success": True,
                                "doctype": dt,
                                "message": f"{dt} deleted",
                            }
                        except TenantAccessError:
                            if getattr(self, "mask_permission_errors", False):
                                return {"error": f"{dt} not found"}, 404
                            return {"error": "Access denied"}, 403
                        except frappe.PermissionError:
                            return {"error": "Access denied"}, 403
                        except frappe.DoesNotExistError:
                            return {"error": f"{dt} not found"}, 404

                    return handler

                delete_handler = make_delete_handler(doctype)

            endpoint_name = f'delete_{doctype_url.replace("-", "_")}'

            delete_handler.__doc__ = f"""
            Delete a {doctype} document
            ---
            tags:
              - {doctype}
            parameters:
              - name: name
                in: path
                type: string
                required: true
                description: Document name
            responses:
              200:
                description: Document deleted successfully
              404:
                description: Document not found
            """

            # Standard Frappe path (with spaces or %20)
            self.secure_route(
                f"{base_path}/{doctype}/<name>",
                methods=["DELETE"],
                endpoint=endpoint_name,
            )(delete_handler)

            # Hyphenated path for backward compatibility
            if doctype_url != doctype:
                endpoint_name_kebab = f'delete_{doctype_url.replace("-", "_")}_kebab'
                self.secure_route(
                    f"{base_path}/{doctype_url}/<name>",
                    methods=["DELETE"],
                    endpoint=endpoint_name_kebab,
                )(delete_handler)
