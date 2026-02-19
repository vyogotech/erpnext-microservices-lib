import os
import json
import pytest
from unittest.mock import MagicMock, patch

import frappe
from flask import g

from frappe_microservice.core import get_user_tenant_id, TenantAwareDB, MicroserviceApp
from frappe_microservice.entrypoint import create_site_config, run_app
from frappe_microservice.controller import DocumentController, ControllerRegistry


def test_get_user_tenant_id_rejects_guest():
    assert get_user_tenant_id('Guest') is None


def test_get_user_tenant_id_sql_success():
    frappe.db.sql.return_value = [{"tenant_id": "tenant-1"}]
    assert get_user_tenant_id('user@example.com') == "tenant-1"


def test_get_user_tenant_id_sql_system_blocked():
    frappe.db.sql.return_value = [{"tenant_id": "SYSTEM"}]
    assert get_user_tenant_id('user@example.com') is None


def test_get_user_tenant_id_fallback_path():
    frappe.db.sql.side_effect = Exception("sql failed")
    frappe.db.get_value.return_value = "tenant-2"
    assert get_user_tenant_id('user@example.com') == "tenant-2"


def test_get_user_tenant_id_admin_no_tenant():
    frappe.db.sql.return_value = []
    assert get_user_tenant_id('Administrator') is None


def test_tenant_db_set_value_requires_exists():
    db = TenantAwareDB(lambda: "tenant-1")
    frappe.db.exists.return_value = False
    with pytest.raises(frappe.PermissionError):
        db.set_value("Sales Order", "SO-001", "status", "Draft")


def test_tenant_db_set_value_success():
    db = TenantAwareDB(lambda: "tenant-1")
    frappe.db.exists.return_value = True
    frappe.db.set_value.return_value = True
    assert db.set_value("Sales Order", "SO-001", "status", "Draft") is True


def test_tenant_db_sql_requires_tenant():
    db = TenantAwareDB(lambda: None)
    with pytest.raises(ValueError):
        db.sql("SELECT 1")


def test_tenant_db_sql_commit_rollback_and_getters():
    db = TenantAwareDB(lambda: "tenant-1")
    frappe.db.sql.return_value = [{"name": "DOC"}]
    frappe.db.commit.return_value = True
    frappe.db.rollback.return_value = True
    frappe.db.count.return_value = 3
    frappe.db.exists.return_value = True

    assert db.sql("SELECT 1") == [{"name": "DOC"}]
    assert db.commit() is True
    assert db.rollback() is True
    assert db.count("Sales Order") == 3
    assert db.exists("Sales Order", {"name": "SO-001"}) is True


def test_tenant_db_get_value_with_string_filter():
    db = TenantAwareDB(lambda: "tenant-1")
    frappe.db.get_value.return_value = "CUST-001"
    result = db.get_value("Sales Order", "SO-001", "customer")
    assert result == "CUST-001"


def test_tenant_db_new_doc_runs_hooks():
    db = TenantAwareDB(lambda: "tenant-1")
    doc = MagicMock()
    doc.doctype = "Sales Order"
    frappe.get_doc.return_value = doc

    @db.before_validate("Sales Order")
    def set_flag(d):
        d.flag = "ok"

    result = db.new_doc("Sales Order", customer="CUST-001")
    assert result.flag == "ok"


def test_tenant_db_insert_doc_with_insert_params():
    db = TenantAwareDB(lambda: "tenant-1")
    doc = MagicMock()
    doc.name = "DOC-001"
    frappe.get_doc.return_value = doc
    frappe.db.get_value.return_value = "tenant-1"

    db.insert_doc("Sales Order", {"customer": "CUST"}, ignore_permissions=True)
    doc.insert.assert_called_once_with(ignore_permissions=True)


def test_tenant_db_delete_doc_runs_hooks():
    db = TenantAwareDB(lambda: "tenant-1")
    doc = MagicMock()
    doc.doctype = "Sales Order"
    doc.tenant_id = "tenant-1"
    db.get_doc = MagicMock(return_value=doc)
    events = []

    @db.before_delete("Sales Order")
    def before(d):
        events.append("before")

    @db.after_delete("Sales Order")
    def after(d):
        events.append("after")

    db.delete_doc("Sales Order", "SO-001", run_hooks=True)
    doc.delete.assert_called_once()
    assert events == ["before", "after"]


def test_document_controller_attr_sync():
    doc = MagicMock()
    controller = DocumentController(doc)
    controller.status = "Draft"
    assert doc.status == "Draft"
    doc.customer = "CUST-001"
    assert controller.customer == "CUST-001"


def test_document_controller_helpers():
    doc = MagicMock()
    doc._doc_before_save = {"status": "Draft"}
    doc.get.side_effect = lambda key: {"status": "Submitted"}.get(key)
    controller = DocumentController(doc)
    assert controller.has_value_changed("status") is True
    assert controller.get_value_before_save("status") == "Draft"


def test_controller_registry_filename_helpers():
    registry = ControllerRegistry()
    assert registry._filename_to_doctype("sales_order") == "Sales Order"
    assert registry._filename_to_classname("sales_order") == "SalesOrder"


def test_controller_registry_discover_and_setup(tmp_path, capfd):
    # Create a fake controller module
    module_path = tmp_path / "sales_order.py"
    module_path.write_text(
        "from frappe_microservice.controller import DocumentController\n"
        "class SalesOrder(DocumentController):\n"
        "    pass\n"
    )

    registry = ControllerRegistry()
    registry.add_controller_path(str(tmp_path))
    registry.discover_controllers(str(tmp_path))

    controller_cls = registry.get_controller("Sales Order")
    assert controller_cls is not None
    assert controller_cls.__name__ == "SalesOrder"

    # setup_controllers should be a no-op but should not print
    registry.setup_controllers(MagicMock())
    captured = capfd.readouterr()
    assert "✅" not in captured.out


def test_controller_registry_alias_methods():
    registry = ControllerRegistry()

    class TestController(DocumentController):
        pass

    registry.register_controller("Test Doc", TestController)
    assert registry.has_controller("Test Doc") is True
    assert registry.get_controller("Test Doc") == TestController


def test_create_site_config_writes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAPPE_SITES_PATH", str(tmp_path))
    monkeypatch.setenv("FRAPPE_SITE", "test.local")
    monkeypatch.setenv("DB_HOST", "db")
    monkeypatch.setenv("DB_NAME", "testdb")

    config = create_site_config()

    assert config["db_host"] == "db"
    assert config["db_name"] == "testdb"

    config_path = tmp_path / "test.local" / "site_config.json"
    assert config_path.exists()
    loaded = json.loads(config_path.read_text())
    assert loaded["db_name"] == "testdb"


def test_create_site_config_permission_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("FRAPPE_SITES_PATH", "/app/sites")
    monkeypatch.setenv("FRAPPE_SITE", "dev.localhost")

    with patch("pathlib.Path.mkdir", side_effect=PermissionError):
        config = create_site_config(db_host="db", db_name="name")

    assert config["db_host"] == "db"
    assert config["db_name"] == "name"


def test_run_app_invokes_app_run(monkeypatch):
    mock_app = MagicMock()
    monkeypatch.setenv("PORT", "9999")
    monkeypatch.setenv("HOST", "127.0.0.1")

    with patch("frappe_microservice.entrypoint.create_site_config") as mock_create:
        run_app(mock_app)
        mock_create.assert_called_once()
        mock_app.run.assert_called_once()


def test_microservice_app_health_route():
    app = MicroserviceApp("test-service", central_site_url="http://central")
    app.flask_app.testing = True

    client = app.flask_app.test_client()
    response = client.get("/health")

    assert response.status_code == 200
    data = response.json
    assert data["status"] == "healthy"
    assert data["service"] == "test-service"


def test_microservice_app_register_resource_crud(monkeypatch):
    app = MicroserviceApp("test-service", central_site_url="http://central")
    app.flask_app.testing = True

    # Mock authentication
    monkeypatch.setattr(app, "_validate_session", lambda: ("user@example.com", None))

    # Mock tenant db
    app.tenant_db.get_all = MagicMock(return_value=[{"name": "SO-001"}])
    app.tenant_db.get_doc = MagicMock(return_value=MagicMock(as_dict=lambda: {"name": "SO-001"}))
    created_doc = MagicMock()
    created_doc.name = "SO-002"
    updated_doc = MagicMock()
    updated_doc.name = "SO-001"
    app.tenant_db.insert_doc = MagicMock(return_value=created_doc)
    app.tenant_db.update_doc = MagicMock(return_value=updated_doc)
    app.tenant_db.delete_doc = MagicMock()

    app.register_resource("Sales Order")

    client = app.flask_app.test_client()

    # List
    res = client.get("/api/resource/sales-order")
    assert res.status_code == 200
    assert res.json["doctype"] == "Sales Order"

    # Get
    res = client.get("/api/resource/sales-order/SO-001")
    assert res.status_code == 200
    assert res.json["name"] == "SO-001"

    # Create
    res = client.post("/api/resource/sales-order", json={"customer": "CUST"})
    assert res.status_code == 201
    assert res.json["name"] == "SO-002"

    # Update
    res = client.put("/api/resource/sales-order/SO-001", json={"status": "Draft"})
    assert res.status_code == 200

    # Delete
    res = client.delete("/api/resource/sales-order/SO-001")
    assert res.status_code == 200


def test_microservice_app_register_resource_errors(monkeypatch):
    app = MicroserviceApp("test-service", central_site_url="http://central")
    app.flask_app.testing = True

    monkeypatch.setattr(app, "_validate_session", lambda: ("user@example.com", None))

    app.tenant_db.get_doc = MagicMock(side_effect=frappe.PermissionError("Access denied"))
    app.tenant_db.insert_doc = MagicMock()
    app.register_resource("Sales Order")

    client = app.flask_app.test_client()

    # Missing body
    res = client.post("/api/resource/sales-order", json={})
    assert res.status_code == 400

    # Permission error in get
    res = client.get("/api/resource/sales-order/SO-001")
    assert res.status_code == 403


def test_microservice_app_set_tenant_id():
    app = MicroserviceApp("test-service", central_site_url="http://central")
    with app.flask_app.test_request_context():
        app.set_tenant_id("tenant-1")
        assert g.tenant_id == "tenant-1"
        assert app._get_current_tenant_id() == "tenant-1"


def test_microservice_app_validate_session_bearer():
    app = MicroserviceApp("test-service", central_site_url="http://central")
    with app.flask_app.test_request_context(headers={"Authorization": "Bearer token"}):
        with patch.object(app, "_validate_oauth_token", return_value=("user@example.com", None)):
            user, error = app._validate_session()
            assert user == "user@example.com"
            assert error is None


def test_microservice_app_validate_session_cookie_invalid():
    app = MicroserviceApp("test-service", central_site_url="http://central")
    with app.flask_app.test_request_context(headers={"Cookie": "sid=invalid"}):
        with patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 401
            user, error = app._validate_session()
            assert user is None
            assert error[1] == 401


def test_microservice_app_isolate_apps_filters_hooks():
    app = MicroserviceApp("test-service", central_site_url="http://central")

    def fake_get_installed_apps(*, _ensure_on_bench=False):
        return ["frappe", "erpnext", "test_service", "otherapp"]

    def fake_get_doc_hooks():
        return {
            "Sales Order": {
                "before_insert": [
                    "frappe.core.do", "otherapp.handler.do"
                ]
            }
        }

    frappe.get_installed_apps = fake_get_installed_apps
    frappe.get_doc_hooks = fake_get_doc_hooks

    app._isolate_microservice_apps()

    installed = frappe.get_installed_apps()
    assert "frappe" in installed
    assert "test_service" in installed

    filtered_hooks = frappe.get_doc_hooks()
    handlers = filtered_hooks["Sales Order"]["before_insert"]
    assert "otherapp.handler.do" not in handlers


def test_microservice_app_run_calls_flask_run():
    app = MicroserviceApp("test-service", central_site_url="http://central")
    with patch("frappe_microservice.core.create_site_config") as mock_create:
        with patch.object(app.flask_app, "run") as mock_run:
            app.run(port=5050)
            mock_create.assert_called_once()
            mock_run.assert_called_once()


def test_microservice_app_get_current_tenant_id_custom():
    app = MicroserviceApp("test-service", get_tenant_id_func=lambda: "tenant-x")
    assert app._get_current_tenant_id() == "tenant-x"

