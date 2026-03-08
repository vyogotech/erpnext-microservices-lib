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

import frappe
from flask import request


class ResourceMixin:
    """
    Mixin for MicroserviceApp that provides register_resource().

    Expects the host class to have:
        self.tenant_db: TenantAwareDB
        self.secure_route(rule, **options): method
        self.flask_app: Flask
        self.logger: logging.Logger
    """

    def register_resource(self, doctype, base_path=None, methods=None, custom_handlers=None):
        """
        Register RESTful CRUD routes for a DocType. Each enabled method (GET/POST/PUT/DELETE)
        is implemented using tenant_db so all data is tenant-scoped. List GET parses
        request.args for fields, limit, offset, order_by and passes the rest as filters.
        Create/update/delete use request.json. All routes are secured and get (user) as
        first argument.         custom_handlers: {'list'|'get'|'post'|'put'|'delete': callable}.
        """
        base_path = base_path or '/api/resource'
        methods = methods or ['GET', 'POST', 'PUT', 'DELETE']
        custom_handlers = custom_handlers or {}

        doctype_url = doctype.lower().replace(' ', '-')

        if 'GET' in methods:
            if 'list' in custom_handlers:
                list_handler = custom_handlers['list']
            else:
                def make_list_handler(dt):
                    def handler(user):
                        filters = {}
                        for key, value in request.args.items():
                            if key not in ['fields', 'limit', 'offset', 'order_by']:
                                filters[key] = value

                        fields = request.args.get(
                            'fields', '*').split(',') if request.args.get('fields') else None
                        limit = int(request.args.get('limit', 20))
                        offset = int(request.args.get('offset', 0))
                        order_by = request.args.get(
                            'order_by', 'modified desc')

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
                description: Comma-separated list of fields to return
              - name: limit
                in: query
                type: integer
                default: 20
                description: Number of records to return
              - name: offset
                in: query
                type: integer
                default: 0
                description: Offset for pagination
              - name: order_by
                in: query
                type: string
                default: modified desc
                description: Sort order
            responses:
              200:
                description: List of {doctype} documents
            """

            self.secure_route(f'{base_path}/{doctype_url}',
                               methods=['GET'], endpoint=endpoint_name)(list_handler)

            if 'get' in custom_handlers:
                get_handler = custom_handlers['get']
            else:
                def make_get_handler(dt):
                    def handler(user, name):
                        try:
                            doc = self.tenant_db.get_doc(dt, name)
                            return doc.as_dict()
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

            self.secure_route(f'{base_path}/{doctype_url}/<name>',
                               methods=['GET'], endpoint=endpoint_name)(get_handler)

        if 'POST' in methods:
            if 'post' in custom_handlers:
                create_handler = custom_handlers['post']
            else:
                def make_create_handler(dt):
                    def handler(user):
                        data = request.json

                        if not data:
                            return {"error": "Request body required"}, 400

                        doc = self.tenant_db.insert_doc(dt, data)

                        return {
                            "success": True,
                            "doctype": dt,
                            "name": doc.name
                        }, 201
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

            self.secure_route(f'{base_path}/{doctype_url}',
                               methods=['POST'], endpoint=endpoint_name)(create_handler)

        if 'PUT' in methods:
            if 'put' in custom_handlers:
                update_handler = custom_handlers['put']
            else:
                def make_update_handler(dt):
                    def handler(user, name):
                        try:
                            data = request.json

                            if not data:
                                return {"error": "Request body required"}, 400

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

            self.secure_route(f'{base_path}/{doctype_url}/<name>',
                               methods=['PUT'], endpoint=endpoint_name)(update_handler)

        if 'DELETE' in methods:
            if 'delete' in custom_handlers:
                delete_handler = custom_handlers['delete']
            else:
                def make_delete_handler(dt):
                    def handler(user, name):
                        try:
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

            self.secure_route(f'{base_path}/{doctype_url}/<name>',
                               methods=['DELETE'], endpoint=endpoint_name)(delete_handler)
