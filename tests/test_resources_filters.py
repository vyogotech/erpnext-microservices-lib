"""Coverage for the ``filters`` / ``or_filters`` handling in resources.py.

Regression guard for BUG-GW: the generic list handler used to build ``filters``
only from flat ``?field=value`` query params and skipped the Frappe-standard JSON
``filters`` param entirely (it is a meta key), so every predicate the client sent
was dropped and the query returned every row the tenant owned.
"""

import json

from unittest.mock import MagicMock

from flask import g

from frappe_microservice.core import MicroserviceApp
from frappe_microservice.resources import (
    _merge_resource_filters,
    _parse_structured_filters_query_param,
)


# ---------------------------------------------------------------------------
# _parse_structured_filters_query_param
# ---------------------------------------------------------------------------

def test_parse_structured_filters_list_of_conditions():
    raw = '[["customer","=","Muthu"],["docstatus","=",1]]'
    assert _parse_structured_filters_query_param(raw) == [
        ["customer", "=", "Muthu"],
        ["docstatus", "=", 1],
    ]


def test_parse_structured_filters_dict():
    assert _parse_structured_filters_query_param('{"customer":"Muthu"}') == {
        "customer": "Muthu"
    }


def test_parse_structured_filters_percent_encoded():
    # Werkzeug decodes once; a double-encoded client still parses via the fallback.
    enc = "%5B%5B%22customer%22%2C%22%3D%22%2C%22Muthu%22%5D%5D"
    assert _parse_structured_filters_query_param(enc) == [["customer", "=", "Muthu"]]


def test_parse_structured_filters_none_and_empty():
    assert _parse_structured_filters_query_param(None) is None
    assert _parse_structured_filters_query_param("") is None
    assert _parse_structured_filters_query_param("   ") is None


def test_parse_structured_filters_invalid_json_returns_none():
    assert _parse_structured_filters_query_param("not-json") is None
    assert _parse_structured_filters_query_param("[unclosed") is None


def test_parse_structured_filters_scalar_json_rejected():
    # Only dict/list are valid filter structures; a bare scalar is not.
    assert _parse_structured_filters_query_param("5") is None
    assert _parse_structured_filters_query_param('"customer"') is None


# ---------------------------------------------------------------------------
# _merge_resource_filters
# ---------------------------------------------------------------------------

def test_merge_flat_into_structured_list():
    merged = _merge_resource_filters({"parent": "P"}, [["customer", "=", "X"]])
    assert merged == [["parent", "=", "P"], ["customer", "=", "X"]]


def test_merge_empty_flat_into_list_is_just_the_list():
    conditions = [["customer", "=", "X"], ["docstatus", "=", 1]]
    assert _merge_resource_filters({}, conditions) == conditions


def test_merge_flat_into_structured_dict_structured_wins():
    merged = _merge_resource_filters(
        {"parent": "P", "customer": "flat"}, {"customer": "structured"}
    )
    assert merged == {"parent": "P", "customer": "structured"}


def test_merge_unsupported_structured_returns_flat():
    flat = {"customer": "X"}
    assert _merge_resource_filters(flat, "not-a-structure") == flat


# ---------------------------------------------------------------------------
# End-to-end: the list route actually forwards the parsed filters to get_all.
# Mirrors test_microservice_app_register_resource_list_json_fields.
# ---------------------------------------------------------------------------

def _list_app(monkeypatch, get_all):
    app = MicroserviceApp("test-service", central_site_url="http://central")
    app.flask_app.testing = True
    monkeypatch.setattr(app, "_validate_session", lambda: ("user@example.com", None))
    app.tenant_db.get_all = get_all
    app.tenant_db.get_doc = MagicMock()
    app.register_resource("Sales Invoice")
    return app


def test_list_forwards_json_filters_to_get_all(monkeypatch):
    captured = {}

    def get_all(dt, filters=None, **kwargs):
        captured["filters"] = filters
        captured["or_filters"] = kwargs.get("or_filters", "ABSENT")
        return [{"name": "INV-1"}]

    app = _list_app(monkeypatch, get_all)
    client = app.flask_app.test_client()
    res = client.get(
        "/api/resource/sales-invoice",
        query_string={
            "filters": json.dumps([["customer", "=", "Muthu"], ["docstatus", "=", 1]])
        },
    )

    assert res.status_code == 200
    assert captured["filters"] == [["customer", "=", "Muthu"], ["docstatus", "=", 1]]
    # No or_filters param sent -> the kwarg must not be passed at all.
    assert captured["or_filters"] == "ABSENT"


def test_list_forwards_or_filters_to_get_all(monkeypatch):
    captured = {}

    def get_all(dt, filters=None, **kwargs):
        captured["or_filters"] = kwargs.get("or_filters")
        return []

    app = _list_app(monkeypatch, get_all)
    client = app.flask_app.test_client()
    res = client.get(
        "/api/resource/sales-invoice",
        query_string={"or_filters": json.dumps([["status", "=", "Overdue"]])},
    )

    assert res.status_code == 200
    assert captured["or_filters"] == [["status", "=", "Overdue"]]


def test_list_merges_flat_param_with_json_filters(monkeypatch):
    captured = {}

    def get_all(dt, filters=None, **kwargs):
        captured["filters"] = filters
        return []

    app = _list_app(monkeypatch, get_all)
    client = app.flask_app.test_client()
    # ``territory`` is an ordinary column (not a Frappe meta key), so it survives
    # the flat loop and must merge with the structured JSON filter.
    res = client.get(
        "/api/resource/sales-invoice",
        query_string={
            "territory": "Australia",
            "filters": json.dumps([["customer", "=", "Muthu"]]),
        },
    )

    assert res.status_code == 200
    assert captured["filters"] == [
        ["territory", "=", "Australia"],
        ["customer", "=", "Muthu"],
    ]


def test_list_without_filters_param_preserves_flat_dict_behaviour(monkeypatch):
    captured = {}

    def get_all(dt, filters=None, **kwargs):
        captured["filters"] = filters
        captured["or_filters"] = kwargs.get("or_filters", "ABSENT")
        return []

    app = _list_app(monkeypatch, get_all)
    client = app.flask_app.test_client()
    res = client.get("/api/resource/sales-invoice")

    assert res.status_code == 200
    assert captured["filters"] == {}
    assert captured["or_filters"] == "ABSENT"


def test_list_malformed_filters_json_falls_back_safely(monkeypatch):
    captured = {}

    def get_all(dt, filters=None, **kwargs):
        captured["filters"] = filters
        return []

    app = _list_app(monkeypatch, get_all)
    client = app.flask_app.test_client()
    res = client.get(
        "/api/resource/sales-invoice",
        query_string={"filters": "not-json"},
    )

    # A garbage filter must not crash the route; it degrades to the flat dict
    # (empty here) rather than 500ing.
    assert res.status_code == 200
    assert captured["filters"] == {}
