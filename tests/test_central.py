import pytest
import os
from unittest.mock import MagicMock, patch
from frappe_microservice.central import CentralSiteClient
from frappe_microservice import MicroserviceApp

def test_central_client_config_from_env(monkeypatch):
    """Test that CentralSiteClient reads config from environment variables."""
    monkeypatch.setenv("CENTRAL_SITE_URL", "http://env-site:8000")
    monkeypatch.setenv("CENTRAL_SITE_API_KEY", "env_key")
    monkeypatch.setenv("CENTRAL_SITE_API_SECRET", "env_secret")
    
    client = CentralSiteClient()
    assert client._url == "http://env-site:8000"
    assert client._api_key == "env_key"
    assert client._api_secret == "env_secret"

def test_central_client_explicit_config():
    """Test that explicit config overrides environment variables."""
    client = CentralSiteClient(
        url="http://explicit-site:8000",
        api_key="explicit_key",
        api_secret="explicit_secret"
    )
    assert client._url == "http://explicit-site:8000"
    assert client._api_key == "explicit_key"
    assert client._api_secret == "explicit_secret"

def test_central_client_lazy_init(monkeypatch):
    """Test that FrappeClient is only initialized when a method is called."""
    monkeypatch.setenv("CENTRAL_SITE_URL", "http://test-site:8000")
    
    with patch("frappe_microservice.central.FrappeClient") as mock_frappe_client:
        client = CentralSiteClient()
        assert client._client is None
        
        # Accessing .client should trigger init
        _ = client.client
        mock_frappe_client.assert_called_once_with(
            url="http://test-site:8000",
            api_key=None,
            api_secret=None,
            username=None,
            password=None
        )
        assert client._client is not None

def test_central_client_missing_url():
    """Test that it raises ValueError if no URL is provided."""
    client = CentralSiteClient(url=None)
    # Clear env just in case
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="CENTRAL_SITE_URL is not set"):
            _ = client.client

def test_central_client_methods(monkeypatch):
    """Test that CentralSiteClient wraps FrappeClient methods correctly."""
    monkeypatch.setenv("CENTRAL_SITE_URL", "http://test-site:8000")
    
    with patch("frappe_microservice.central.FrappeClient") as mock_frappe_client:
        instance = mock_frappe_client.return_value
        client = CentralSiteClient()
        
        # get_doc
        client.get_doc("User", "Administrator")
        instance.get_doc.assert_called_with("User", name="Administrator", filters=None, fields=None)
        
        # insert
        doc = {"doctype": "Task", "title": "Buy milk"}
        client.insert(doc)
        instance.insert.assert_called_with(doc)
        
        # update
        doc = {"doctype": "Task", "name": "TASK001", "title": "Buy cookies"}
        client.update(doc)
        instance.update.assert_called_with(doc)
        
        # get_value
        client.get_value("User", "full_name", {"name": "Administrator"})
        instance.get_value.assert_called_with("User", fieldname="full_name", filters={"name": "Administrator"})
        
        # get_list
        client.get_list("User", filters={"enabled": 1}, fields=["email"])
        instance.get_list.assert_called_with(
            "User",
            filters={"enabled": 1},
            fields=["email"],
            limit_start=0,
            limit_page_length=20,
            order_by=None
        )
        
        # call
        client.call("ping", {"foo": "bar"})
        instance.post_api.assert_called_with("ping", {"foo": "bar"})

def test_microservice_app_central_property(monkeypatch):
    """Test that MicroserviceApp exposes the central client."""
    monkeypatch.setenv("FRAPPE_SITE", "site1.local")
    monkeypatch.setenv("CENTRAL_SITE_URL", "http://central:8000")
    
    app = MicroserviceApp("test-service")
    assert app._central_client is None
    
    with patch("frappe_microservice.central.FrappeClient") as mock_frappe_client:
        central = app.central
        assert isinstance(central, CentralSiteClient)
        assert central._url == "http://central:8000"
        
        # Verify it uses the central site url from the app
        _ = app.central.client
        mock_frappe_client.assert_called_once()
