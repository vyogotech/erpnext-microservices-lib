"""
Single-tenant mode (MULTI_TENANT=0).

The library was originally written against a Central Site carrying a tenancy
provider app, whose patch ALTERs every table to add a ``tenant_id`` column.
Single-tenant mode drops that requirement so the library runs against a stock
Frappe/ERPNext site, where no such column exists.

These tests pin the two halves of that contract:

1. Nothing resolves, filters on, writes or verifies a ``tenant_id``.
2. Everything *else* — role permissions above all — still behaves exactly as it
   does in multi-tenant mode.

Multi-tenant remains the default; the tests alongside these in
tests/test_tenant_aware_db.py are the regression guard for it.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

import frappe  # noqa: F401  (conftest installs the mock)

from frappe_microservice.core import TenantAwareDB


class _Doc:
    """Plain object, deliberately not a MagicMock.

    MagicMock auto-creates any attribute touched, so an assertion that
    ``tenant_id`` was never set would pass vacuously against one.
    """

    def __init__(self, **kwargs):
        self.name = kwargs.pop("name", "DOC-001")
        self.doctype = kwargs.pop("doctype", "Item")
        for key, value in kwargs.items():
            setattr(self, key, value)

    def insert(self, **kwargs):
        self.__dict__.setdefault("_insert_kwargs", kwargs)
        return self


@pytest.fixture
def st_db():
    """A single-tenant wrapper whose tenant provider must never be consulted."""
    provider = MagicMock(side_effect=AssertionError("tenant provider was consulted"))
    return TenantAwareDB(provider, multi_tenant=False)


class TestFlagResolution:
    """MULTI_TENANT resolution, including the empty-env fail-safe."""

    def test_defaults_to_multi_tenant(self, monkeypatch):
        monkeypatch.delenv("MULTI_TENANT", raising=False)
        assert TenantAwareDB(MagicMock()).multi_tenant is True

    def test_kwarg_beats_env(self, monkeypatch):
        monkeypatch.setenv("MULTI_TENANT", "1")
        assert TenantAwareDB(MagicMock(), multi_tenant=False).multi_tenant is False

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_env_disables(self, monkeypatch, raw):
        monkeypatch.setenv("MULTI_TENANT", raw)
        assert TenantAwareDB(MagicMock()).multi_tenant is False

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_env_stays_multi_tenant(self, monkeypatch, raw):
        """A declared-but-valueless env var must not silently drop isolation."""
        monkeypatch.setenv("MULTI_TENANT", raw)
        assert TenantAwareDB(MagicMock()).multi_tenant is True

    def test_current_tenant_id_is_none_when_off(self, st_db):
        assert st_db.current_tenant_id() is None

    def test_current_tenant_id_resolves_when_on(self):
        db = TenantAwareDB(MagicMock(return_value="t1"), multi_tenant=True)
        assert db.current_tenant_id() == "t1"

    def test_warns_when_tenant_strict_is_moot(self, caplog):
        with caplog.at_level(logging.WARNING):
            TenantAwareDB(MagicMock(), multi_tenant=False, tenant_strict="enforce")
        assert any("TENANT_STRICT=enforce has no effect" in r.message for r in caplog.records)


class TestReadPath:
    """_add_tenant_filter is the single choke point for every read."""

    def test_dict_filters_pass_through(self, st_db):
        assert st_db._add_tenant_filter({"status": "Active"}) == {"status": "Active"}

    def test_list_filters_pass_through(self, st_db):
        filters = [["status", "=", "Active"]]
        assert st_db._add_tenant_filter(filters) == [["status", "=", "Active"]]

    def test_none_filters_pass_through(self, st_db):
        assert st_db._add_tenant_filter(None) is None

    def test_str_filters_normalized_to_name(self, st_db):
        """Callers hand the result to Frappe, which cannot take a bare name."""
        assert st_db._add_tenant_filter("DOC-001") == {"name": "DOC-001"}

    def test_tenant_provider_never_consulted(self):
        provider = MagicMock(return_value="t1")
        db = TenantAwareDB(provider, multi_tenant=False)
        db._add_tenant_filter({"a": 1})
        provider.assert_not_called()

    def test_no_raise_without_tenant(self):
        db = TenantAwareDB(MagicMock(return_value=None), multi_tenant=False)
        assert db._add_tenant_filter({"a": 1}) == {"a": 1}

    def test_no_raise_on_system_tenant(self):
        db = TenantAwareDB(MagicMock(return_value="SYSTEM"), multi_tenant=False)
        assert db._add_tenant_filter({"a": 1}) == {"a": 1}

    @patch("frappe.get_all", create=True)
    def test_get_all_passes_original_filters(self, mock_get_all, st_db):
        mock_get_all.return_value = []
        st_db.get_all("Item", filters={"item_group": "Products"})
        assert mock_get_all.call_args[1]["filters"] == {"item_group": "Products"}


class TestDocumentVerification:
    """A foreign tenant_id on a fetched doc is not an error when there is no tenancy."""

    @patch("frappe.get_doc", create=True)
    def test_foreign_tenant_doc_returned(self, mock_get_doc, st_db):
        mock_get_doc.return_value = _Doc(tenant_id="some_other_tenant")
        assert st_db.get_doc("Item", "ITEM-001").tenant_id == "some_other_tenant"

    @patch("frappe.get_doc", create=True)
    def test_missing_column_does_not_warn(self, mock_get_doc, caplog):
        """tenant_strict is short-circuited, not consulted.

        Left live, its default `warn` would log on every single read.
        """
        db = TenantAwareDB(MagicMock(), multi_tenant=False, tenant_strict="warn")
        mock_get_doc.return_value = _Doc()  # no tenant_id attribute
        with caplog.at_level(logging.WARNING):
            db.get_doc("Item", "ITEM-001")
        # Specifically the tenant-isolation warning. The permission-mode
        # [would-deny] line is a different check and must still be free to fire.
        assert not [
            r for r in caplog.records if "tenant isolation unavailable" in r.message
        ]

    @patch("frappe.get_doc", create=True)
    def test_enforce_does_not_block(self, mock_get_doc):
        """TENANT_STRICT=enforce must not make single-tenant mode impossible."""
        db = TenantAwareDB(MagicMock(), multi_tenant=False, tenant_strict="enforce")
        mock_get_doc.return_value = _Doc()
        assert db.get_doc("Item", "ITEM-001") is not None


class TestWritePath:
    @patch("frappe.get_doc", create=True)
    @patch("frappe.db.get_value", create=True)
    def test_no_tenant_id_in_doc_dict(self, mock_get_value, mock_get_doc, st_db):
        mock_get_doc.return_value = _Doc()
        st_db.insert_doc("Item", {"item_code": "WIDGET"})
        assert "tenant_id" not in mock_get_doc.call_args[0][0]

    @patch("frappe.get_doc", create=True)
    @patch("frappe.db.get_value", create=True)
    def test_tenant_id_not_set_on_doc(self, mock_get_value, mock_get_doc, st_db):
        doc = _Doc()
        mock_get_doc.return_value = doc
        st_db.insert_doc("Item", {"item_code": "WIDGET"})
        assert not hasattr(doc, "tenant_id")

    @patch("frappe.get_doc", create=True)
    @patch("frappe.db.get_value", create=True)
    def test_no_post_insert_verification_query(self, mock_get_value, mock_get_doc):
        """The verification query reads a column that does not exist."""
        db = TenantAwareDB(
            MagicMock(), multi_tenant=False, verify_tenant_on_insert=True
        )
        mock_get_doc.return_value = _Doc()
        db.insert_doc("Item", {"item_code": "WIDGET"})
        mock_get_value.assert_not_called()

    @patch("frappe.get_doc", create=True)
    @patch("frappe.db.get_value", create=True)
    def test_insert_succeeds_without_tenant(self, mock_get_value, mock_get_doc):
        db = TenantAwareDB(MagicMock(return_value=None), multi_tenant=False)
        mock_get_doc.return_value = _Doc()
        assert db.insert_doc("Item", {"item_code": "WIDGET"}) is not None

    @patch("frappe.get_doc", create=True)
    @patch("frappe.db.get_value", create=True)
    def test_explicit_tenant_id_passes_through(self, mock_get_value, mock_get_doc, st_db):
        """No cross-tenant rejection when there is no current tenant to compare.

        Passed through rather than stripped: on a stock site Frappe rejects the
        unknown fieldname itself, which surfaces the misconfiguration instead of
        hiding it.
        """
        mock_get_doc.return_value = _Doc()
        st_db.insert_doc("Item", {"item_code": "W", "tenant_id": "other"})
        assert mock_get_doc.call_args[0][0]["tenant_id"] == "other"

    @patch("frappe.get_doc", create=True)
    def test_new_doc_without_tenant(self, mock_get_doc, st_db):
        mock_get_doc.return_value = _Doc()
        st_db.new_doc("Item", item_code="WIDGET")
        assert "tenant_id" not in mock_get_doc.call_args[0][0]

    @patch("frappe.db.sql", create=True)
    def test_sql_without_tenant(self, mock_sql, st_db):
        mock_sql.return_value = []
        st_db.sql("SELECT 1")
        mock_sql.assert_called_once()


class TestMasterAutonaming:
    """Tenant-scoped docnames are skipped; Frappe autonames instead."""

    @patch("frappe.get_doc", create=True)
    @patch("frappe.db.get_value", create=True)
    def test_no_set_name_injected(self, mock_get_value, mock_get_doc, st_db, monkeypatch):
        import frappe_microservice.naming as naming

        def _boom(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("tenant-scoped naming ran in single-tenant mode")

        monkeypatch.setattr(naming, "_claimable_row", _boom)
        monkeypatch.setattr(naming, "tenant_scoped_docname", _boom)
        monkeypatch.setattr(naming, "_next_free_docname", _boom)

        doc = _Doc()
        mock_get_doc.return_value = doc
        st_db.insert_doc("Item", {"item_name": "Widget"})
        assert "set_name" not in doc.__dict__.get("_insert_kwargs", {})


class TestPermissionsStillEnforced:
    """permission_mode is orthogonal to tenancy and must survive untouched.

    Guards against a later "single-tenant means no checks" simplification.
    """

    @patch("frappe.has_permission", create=True)
    @patch("frappe.get_all", create=True)
    def test_get_all_still_permission_checked(self, mock_get_all, mock_has_perm):
        mock_has_perm.return_value = False
        db = TenantAwareDB(MagicMock(), multi_tenant=False, permission_mode="enforce")
        with pytest.raises(frappe.PermissionError):
            db.get_all("Sales Invoice")

    @patch("frappe.has_permission", create=True)
    @patch("frappe.get_doc", create=True)
    def test_get_doc_still_permission_checked(self, mock_get_doc, mock_has_perm):
        mock_has_perm.return_value = False
        mock_get_doc.return_value = _Doc()
        db = TenantAwareDB(MagicMock(), multi_tenant=False, permission_mode="enforce")
        with pytest.raises(frappe.PermissionError):
            db.get_doc("Sales Invoice", "SI-001")


class TestPresyncDoesNotRecreateColumn:
    """The trap that would silently undo the whole point of this mode."""

    def test_no_ddl_when_off(self, monkeypatch):
        from frappe_microservice import presync

        monkeypatch.setenv("MULTI_TENANT", "0")
        table_exists = MagicMock(return_value=True)
        monkeypatch.setattr(frappe.db, "table_exists", table_exists, raising=False)

        presync._ensure_tenant_id_column("Widget")

        table_exists.assert_not_called()

    def test_ddl_still_runs_when_on(self, monkeypatch):
        from frappe_microservice import presync

        monkeypatch.setenv("MULTI_TENANT", "1")
        table_exists = MagicMock(return_value=False)
        monkeypatch.setattr(frappe.db, "table_exists", table_exists, raising=False)

        presync._ensure_tenant_id_column("Widget")

        table_exists.assert_called_once()


class TestPresentationIsInert:
    """Link values must not be rewritten to labels when nothing was renamed."""

    def test_documents_returned_unmutated(self, st_db):
        from frappe_microservice import presentation

        docs = [{"customer": "CUST-00001"}]
        assert presentation.present_documents("Sales Order", docs, st_db) is docs
        assert docs == [{"customer": "CUST-00001"}]

