import os
import logging
from typing import Any, Dict, List, Optional, Union
import frappe
from frappe.frappeclient import FrappeClient

logger = logging.getLogger("frappe_microservice.central")

class CentralSiteClient:
    """
    A Frappe-like API client for the Central Site.
    Configured via environment variables:
    - CENTRAL_SITE_URL
    - CENTRAL_SITE_API_KEY
    - CENTRAL_SITE_API_SECRET
    - CENTRAL_SITE_USER
    - CENTRAL_SITE_PASSWORD
    - CENTRAL_SITE_TIMEOUT (default: 10)
    """

    def __init__(
        self,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: Optional[int] = None
    ):
        self._url = url or os.getenv("CENTRAL_SITE_URL")
        self._api_key = api_key or os.getenv("CENTRAL_SITE_API_KEY")
        self._api_secret = api_secret or os.getenv("CENTRAL_SITE_API_SECRET")
        self._username = username or os.getenv("CENTRAL_SITE_USER")
        self._password = password or os.getenv("CENTRAL_SITE_PASSWORD")
        self._timeout = timeout or int(os.getenv("CENTRAL_SITE_TIMEOUT", 10))
        
        self._client: Optional[FrappeClient] = None

    @property
    def client(self) -> FrappeClient:
        """Lazy-initialize the FrappeClient."""
        if not self._client:
            if not self._url:
                raise ValueError("CENTRAL_SITE_URL is not set and no URL provided.")
            
            logger.debug(f"Initializing CentralSiteClient for {self._url}")
            try:
                self._client = FrappeClient(
                    url=self._url,
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    username=self._username,
                    password=self._password
                )
                # Note: FrappeClient doesn't have an explicit timeout param in __init__
                # in some versions, but we could patch the session if needed.
            except Exception as e:
                logger.error(f"Failed to initialize FrappeClient for {self._url}: {e}")
                raise

        return self._client

    def get_doc(self, doctype: str, name: Optional[str] = None, filters: Optional[Dict] = None, fields: Optional[List[str]] = None) -> Any:
        """Return a single remote document."""
        return self.client.get_doc(doctype, name=name, filters=filters, fields=fields)

    def insert(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a document to the remote server."""
        return self.client.insert(doc)

    def update(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """Update a remote document. 'name' is mandatory."""
        return self.client.update(doc)

    def get_value(self, doctype: str, fieldname: Optional[str] = None, filters: Optional[Union[Dict, str]] = None) -> Any:
        """Return a value from a document."""
        return self.client.get_value(doctype, fieldname=fieldname, filters=filters)

    def get_list(
        self,
        doctype: str,
        filters: Optional[Dict] = None,
        fields: Optional[Union[str, List[str]]] = None,
        limit_start: int = 0,
        limit_page_length: int = 20,
        order_by: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return list of records of a particular type."""
        return self.client.get_list(
            doctype,
            filters=filters,
            fields=fields or ["name"],
            limit_start=limit_start,
            limit_page_length=limit_page_length,
            order_by=order_by
        )

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Call a remote whitelisted method."""
        return self.client.post_api(method, params or {})
