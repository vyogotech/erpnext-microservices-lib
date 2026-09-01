"""
Unit tests for frappe_microservice.naming module.
Verifies tenant-scoped docname generation and resolve_or_create helper.
"""

from unittest.mock import MagicMock, patch
import pytest
import frappe

from frappe_microservice.naming import (
    tenant_scoped_docname,
    resolve_or_create,
    _next_free_docname,
)


class TestTenantScopedDocname:
    """Tests for tenant_scoped_docname formula & idempotency"""

    def test_tenant_scoped_docname_normal(self):
        def tenant_id_fn():
            return "e373311e-84b2-4d1a-9f5e-123456789abc"

        res = tenant_scoped_docname("Travel", tenant_id_fn)
        assert res == "e373311e-travel"

    def test_tenant_scoped_docname_idempotent(self):
        def tenant_id_fn():
            return "e373311e-84b2-4d1a-9f5e-123456789abc"

        res = tenant_scoped_docname("e373311e-travel", tenant_id_fn)
        assert res == "e373311e-travel"

    def test_tenant_scoped_docname_empty_fallback(self):
        def tenant_id_fn():
            return "e373311e-84b2-4d1a-9f5e-123456789abc"

        res = tenant_scoped_docname("", tenant_id_fn, fallback="category")
        assert res == "e373311e-category"

    def test_tenant_scoped_docname_no_tenant(self):
        def tenant_id_fn():
            return ""

        res = tenant_scoped_docname("Travel", tenant_id_fn)
        assert res == "Travel"

    def test_tenant_scoped_docname_special_chars(self):
        def tenant_id_fn():
            return "e373311e-84b2-4d1a-9f5e-123456789abc"

        res = tenant_scoped_docname("Travel & Expense (2026)", tenant_id_fn)
        assert res == "e373311e-travel-expense-2026"


class TestResolveOrCreate:
    """Tests for resolve_or_create logic"""

    @pytest.fixture
    def tenant_db(self):
        db = MagicMock()
        db.get_tenant_id.return_value = "e373311e-84b2-4d1a-9f5e-123456789abc"
        return db

    def test_resolve_existing_by_docname(self, tenant_db):
        """Step 0: the label is itself a docname this tenant owns.

        Asserts the lookup is tenant-filtered — a bare {"name": ...} probe with
        no tenant_id would be the cross-tenant leak this module exists to close.
        """
        seen_filters = []

        def get_all(doctype, filters=None, **kwargs):
            seen_filters.append(filters)
            if filters and filters.get("name") == "Travel":
                return [{"name": "e373311e-travel"}]
            return []

        tenant_db.get_all.side_effect = get_all
        res = resolve_or_create("Item", "item_code", "Travel", tenant_db)
        assert res == "e373311e-travel"
        tenant_db.insert_doc.assert_not_called()
        assert seen_filters[0]["tenant_id"] == "e373311e-84b2-4d1a-9f5e-123456789abc"

    def test_resolve_existing_by_label_field(self, tenant_db):
        tenant_db.get_value.return_value = None
        tenant_db.get_all.return_value = [{"name": "e373311e-travel"}]
        res = resolve_or_create("Item", "item_code", "Travel", tenant_db)
        assert res == "e373311e-travel"
        tenant_db.insert_doc.assert_not_called()

    @patch("frappe.db.exists", return_value=False)
    def test_create_new_row(self, mock_exists, tenant_db):
        tenant_db.get_value.return_value = None
        tenant_db.get_all.return_value = []
        created_doc = MagicMock()
        created_doc.name = "e373311e-travel"
        tenant_db.insert_doc.return_value = created_doc

        # mock _row_exists check
        with patch("frappe_microservice.naming._row_exists", return_value=True):
            res = resolve_or_create(
                "Item",
                "item_code",
                "Travel",
                tenant_db,
                extra_fields={"item_name": "Travel"},
            )
            assert res == "e373311e-travel"
            tenant_db.insert_doc.assert_called_once()
            _, kwargs = tenant_db.insert_doc.call_args
            assert kwargs.get("set_name") == "e373311e-travel"

    def test_concurrent_insert_handling(self, tenant_db):
        # 1st lookup returns None, 2nd lookup (after insert failure) returns existing doc
        tenant_db.get_value.side_effect = [None, "e373311e-travel"]
        tenant_db.get_all.side_effect = [[], [{"name": "e373311e-travel"}]]
        tenant_db.insert_doc.side_effect = Exception("DuplicateEntryError")

        with patch("frappe.db.exists", return_value=False):
            res = resolve_or_create("Item", "item_code", "Travel", tenant_db)
            assert res == "e373311e-travel"

    def test_creation_fails_no_fallback_raises(self, tenant_db):
        tenant_db.get_value.return_value = None
        tenant_db.get_all.return_value = []
        tenant_db.insert_doc.side_effect = Exception("DB Error")

        with patch("frappe.db.exists", return_value=False):
            with pytest.raises(frappe.ValidationError):
                resolve_or_create("Item", "item_code", "Travel", tenant_db)

    def test_creation_fails_with_fallback(self, tenant_db):
        tenant_db.get_value.return_value = None
        tenant_db.get_all.return_value = []
        tenant_db.insert_doc.side_effect = Exception("DB Error")

        with patch("frappe.db.exists", return_value=False):
            res = resolve_or_create(
                "Item Group",
                "item_group_name",
                "Travel",
                tenant_db,
                fallback_docname="All Item Groups",
            )
            assert res == "All Item Groups"


class TestCrossTenantIsolation:
    """The regressions this module exists to prevent.

    Every test here must FAIL if the unowned-row claim fallback is reintroduced
    or the docname prefix stops depending on tenant_id.
    """

    @staticmethod
    def _db_for(tenant_id, owned_rows):
        """A tenant_db that can only ever see ``owned_rows``.

        Mirrors TenantAwareDB: get_all injects tenant_id, so a row belonging to
        another tenant — or an unowned row — is simply not returned.

        ``owned_rows`` maps docname -> display label, so both _claimable_row
        probes are modelled: step 0 by {"name": ...}, step 1 by {label_field: ...}.
        """
        db = MagicMock()
        db.get_tenant_id.return_value = tenant_id

        def get_all(doctype, filters=None, fields=None, **kwargs):
            f = filters or {}
            if "name" in f:
                return [{"name": f["name"]}] if f["name"] in owned_rows else []
            label = next((v for k, v in f.items() if k != "tenant_id"), None)
            for docname, owned_label in owned_rows.items():
                if owned_label == label:
                    return [{"name": docname}]
            return []

        db.get_all.side_effect = get_all

        def insert_doc(doctype, values, **kwargs):
            docname = kwargs["set_name"]
            # resolve_or_create builds the payload as {label_field: label, **extra}
            label = next(iter(values.values()))
            owned_rows[docname] = label
            doc = MagicMock()
            doc.name = docname
            return doc

        db.insert_doc.side_effect = insert_doc
        return db

    @staticmethod
    def _exists_in(*stores):
        """frappe.db.exists over the union of the given tenant stores.

        Docnames are a global PK, so existence is not tenant-scoped — this is
        what makes _next_free_docname probe correctly before an insert and
        _row_exists confirm afterwards.
        """

        def _exists(doctype, name, *args, **kwargs):
            return any(name in store for store in stores)

        return _exists

    def test_two_tenants_same_label_get_distinct_rows(self):
        """The original leak: tenant B's "Travel" resolved to tenant A's Item."""
        rows_a, rows_b = {}, {}
        db_a = self._db_for("aaaaaaaa-1111-2222-3333-444444444444", rows_a)
        db_b = self._db_for("bbbbbbbb-1111-2222-3333-444444444444", rows_b)

        with patch("frappe.db.exists", side_effect=self._exists_in(rows_a, rows_b)):
            name_a = resolve_or_create("Item", "item_code", "Travel", db_a)
            name_b = resolve_or_create("Item", "item_code", "Travel", db_b)

        assert name_a == "aaaaaaaa-travel"
        assert name_b == "bbbbbbbb-travel"
        assert name_a != name_b
        # Neither tenant can even see the other's docname.
        assert name_b not in rows_a
        assert name_a not in rows_b

    def test_unowned_global_row_is_never_claimed(self):
        """A row with no tenant_id must not be reused — every tenant gets its own.

        tenant_db returns nothing for it (the tenant filter excludes unowned
        rows), so resolve_or_create must create rather than claim.
        """
        rows = {}
        db = self._db_for("aaaaaaaa-1111-2222-3333-444444444444", rows)
        # The unowned row exists globally under its bare label, but tenant_db
        # cannot see it — so it must be created fresh, never claimed.
        unowned = {"Acme Pty Ltd": "Acme Pty Ltd"}

        with patch("frappe.db.exists", side_effect=self._exists_in(rows, unowned)):
            res = resolve_or_create("Supplier", "supplier_name", "Acme Pty Ltd", db)

        assert res == "aaaaaaaa-acme-pty-ltd"
        assert res != "Acme Pty Ltd"
        db.insert_doc.assert_called_once()
        # The docname rides on set_name; a "name" key would be silently dropped
        # by Frappe's set_new_name() for field-autonamed doctypes.
        _, kwargs = db.insert_doc.call_args
        assert kwargs["set_name"] == "aaaaaaaa-acme-pty-ltd"
        assert "name" not in db.insert_doc.call_args.args[1]

    def test_second_resolve_reuses_the_tenants_own_row(self):
        """Idempotent within a tenant: no duplicate, no -002 suffix."""
        rows = {}
        db = self._db_for("aaaaaaaa-1111-2222-3333-444444444444", rows)

        with patch("frappe.db.exists", side_effect=self._exists_in(rows)):
            first = resolve_or_create("Customer", "customer_name", "John Smith", db)
            second = resolve_or_create("Customer", "customer_name", "John Smith", db)

        assert first == second == "aaaaaaaa-john-smith"
        assert db.insert_doc.call_count == 1

    def test_next_free_docname_probes_suffixes_on_prefix_collision(self):
        """Two tenants CAN share the 8-char prefix and must still get distinct rows.

        Observed live: expense-integ-tenant-001 and -002 both reduce to
        'expensei', so the tenant-scoped docname is already taken by the other
        tenant. Creation must probe base-002, base-003, … rather than collide on
        the PK. Moved here from expense-service, which had been reaching into
        this library internal to cover it.
        """
        taken = {"expensei-travel", "expensei-travel-002"}
        with patch("frappe.db.exists", side_effect=lambda dt, name: name in taken):
            assert (
                _next_free_docname("Item", "expensei-travel") == "expensei-travel-003"
            )
        with patch("frappe.db.exists", return_value=False):
            assert _next_free_docname("Item", "expensei-travel") == "expensei-travel"

    def test_docname_matches_central_site_formula(self):
        """Parity guard: the central site autonames Item/Item Group itself.

        If this drifts, one tenant's data splits across two docnames for the
        same label depending on which side created it.
        """
        tenant_id = "e373311e-84b2-4d1a-9f5e-123456789abc"
        # tenancy_app.utils.tenant.set_item_tenant_autoname
        central = tenant_id.replace("-", "")[:8].lower() + "-travel"
        assert tenant_scoped_docname("Travel", lambda: tenant_id) == central


class TestLabelResolutionIsDeterministic:
    """One tenant may hold several rows sharing a label, so the tie-break matters.

    The central site drops the uniqueness constraint on the label field and
    _next_free_docname suffixes docname collisions, so duplicates are permitted
    by construction. Frappe's default ordering is ``modified desc``, which made
    label -> docname resolution drift every time any of the duplicates was
    touched — silently repointing already-saved documents at a different row.
    """

    def test_label_probe_asks_for_the_oldest_row(self):
        from frappe_microservice.naming import _claimable_row

        calls = []
        db = MagicMock()
        db.get_tenant_id.return_value = "4af90f31"

        def get_all(doctype, filters=None, fields=None, **kwargs):
            calls.append({"filters": filters or {}, **kwargs})
            if "name" in (filters or {}):
                return []
            return [{"name": "4af90f31-brake-pads"}]

        db.get_all.side_effect = get_all

        assert (
            _claimable_row("Item", "item_code", "Brake Pads", db)
            == "4af90f31-brake-pads"
        )

        label_probe = next(c for c in calls if "name" not in c["filters"])
        assert label_probe["order_by"] == "creation asc"

    def test_duplicate_labels_resolve_to_the_original_row(self):
        from frappe_microservice.naming import _claimable_row

        # Same label, two rows; the second was created later but touched last.
        rows = [
            {"name": "4af90f31-brake-pads-002", "creation": 2, "modified": 9},
            {"name": "4af90f31-brake-pads", "creation": 1, "modified": 1},
        ]
        db = MagicMock()
        db.get_tenant_id.return_value = "4af90f31"

        def get_all(doctype, filters=None, fields=None, **kwargs):
            if "name" in (filters or {}):
                return []
            field, _, direction = (kwargs.get("order_by") or "modified desc").partition(
                " "
            )
            ordered = sorted(
                rows, key=lambda r: r[field], reverse=direction.strip() == "desc"
            )
            return ordered[: kwargs.get("limit") or len(ordered)]

        db.get_all.side_effect = get_all

        # Not the most recently modified row — the one that has always held the label.
        assert (
            _claimable_row("Item", "item_code", "Brake Pads", db)
            == "4af90f31-brake-pads"
        )


class TestLinkPresentation:
    """Whatever the library renames on the way in, it presents on the way out."""

    @staticmethod
    def _meta(fields):
        m = MagicMock()
        m.fields = [MagicMock(**f) for f in fields]
        return m

    def _patch_meta(self, monkeypatch, mapping):
        import frappe_microservice.presentation as pres

        def get_meta(dt):
            if dt not in mapping:
                raise Exception("no meta")
            return self._meta(mapping[dt])

        monkeypatch.setattr(pres.frappe, "get_meta", get_meta, raising=False)

    def test_child_row_link_is_presented_as_its_label(self, monkeypatch):
        from frappe_microservice.presentation import present_document

        self._patch_meta(
            monkeypatch,
            {
                "Quotation": [
                    {
                        "fieldtype": "Table",
                        "options": "Quotation Item",
                        "fieldname": "items",
                    },
                ],
                "Quotation Item": [
                    {"fieldtype": "Link", "options": "Item", "fieldname": "item_code"},
                ],
            },
        )
        db = MagicMock()
        db.get_all.return_value = [
            {"name": "4af90f31-brake-pads", "item_code": "Brake Pads"}
        ]

        doc = {
            "name": "SAL-QTN-1",
            "items": [{"item_code": "4af90f31-brake-pads", "qty": 1}],
        }
        out = present_document("Quotation", doc, db)

        assert out["items"][0]["item_code"] == "Brake Pads"
        # Item.item_code, not item_name: that is what _claimable_row resolves against.
        assert "item_code" in db.get_all.call_args.kwargs["fields"]

    def test_a_page_of_rows_is_presented_with_one_lookup_per_target(self, monkeypatch):
        """List routes resolve the same masters repeatedly; the cost must not scale with rows."""
        from frappe_microservice.presentation import present_documents

        self._patch_meta(
            monkeypatch,
            {
                "Sales Invoice": [
                    {
                        "fieldtype": "Link",
                        "options": "Customer",
                        "fieldname": "customer",
                    },
                ],
            },
        )
        db = MagicMock()
        db.get_all.return_value = [
            {"name": "4af90f31-acme-pty-ltd", "customer_name": "Acme Pty Ltd"},
            {"name": "4af90f31-globex", "customer_name": "Globex"},
        ]

        rows = [
            {"name": "SINV-1", "customer": "4af90f31-acme-pty-ltd"},
            {"name": "SINV-2", "customer": "4af90f31-globex"},
            {"name": "SINV-3", "customer": "4af90f31-acme-pty-ltd"},
        ]
        present_documents("Sales Invoice", rows, db)

        assert [r["customer"] for r in rows] == ["Acme Pty Ltd", "Globex", "Acme Pty Ltd"]
        # One target doctype across the page — one query, not one per row.
        assert db.get_all.call_count == 1
        assert [r["name"] for r in rows] == ["SINV-1", "SINV-2", "SINV-3"]

    def test_document_own_name_is_never_touched(self, monkeypatch):
        """``name`` is the document's address; rewriting it would break GET by name."""
        from frappe_microservice.presentation import present_document

        self._patch_meta(
            monkeypatch,
            {
                "Item": [
                    {"fieldtype": "Data", "options": "", "fieldname": "item_code"},
                ]
            },
        )
        db = MagicMock()
        db.get_all.return_value = []

        doc = {"name": "4af90f31-brake-pads", "item_code": "Brake Pads"}
        out = present_document("Item", doc, db)

        assert out["name"] == "4af90f31-brake-pads"

    def test_unresolvable_link_is_left_alone(self, monkeypatch):
        from frappe_microservice.presentation import present_document

        self._patch_meta(
            monkeypatch,
            {
                "Quotation": [
                    {
                        "fieldtype": "Link",
                        "options": "Customer",
                        "fieldname": "party_name",
                    },
                ]
            },
        )
        db = MagicMock()
        db.get_all.return_value = []

        out = present_document("Quotation", {"party_name": "CUST-1"}, db)
        assert out["party_name"] == "CUST-1"

    def test_lookup_failure_never_costs_the_document(self, monkeypatch):
        from frappe_microservice.presentation import present_document

        self._patch_meta(
            monkeypatch,
            {
                "Quotation": [
                    {
                        "fieldtype": "Link",
                        "options": "Customer",
                        "fieldname": "party_name",
                    },
                ]
            },
        )
        db = MagicMock()
        db.get_all.side_effect = RuntimeError("db down")

        out = present_document("Quotation", {"party_name": "CUST-1"}, db)
        assert out["party_name"] == "CUST-1"
