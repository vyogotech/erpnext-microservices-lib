"""Coverage for frappe_microservice.resources JSON helpers."""

import json
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID

from frappe_microservice.resources import (
    _doc_as_json_str,
    _format_timedelta_safe,
    _make_json_safe,
)


def test_make_json_safe_primitives_and_containers():
    d = {
        "n": 1,
        "f": 1.5,
        "b": True,
        "x": None,
        "td": timedelta(hours=1),
        "dt": date(2026, 1, 1),
        "dtt": datetime(2026, 1, 1, 12, 0, 0),
        "t": time(12, 30),
        "dec": Decimal("1.25"),
        "u": UUID("550e8400-e29b-41d4-a716-446655440000"),
        "by": b"ab",
        "st": {1, 2},
        "m": {"k": 1},
        "seq": (2, 3),
    }
    safe = _make_json_safe(d)
    json.dumps(safe, default=str)
    assert safe["n"] == 1
    assert isinstance(safe["td"], str)
    assert safe["dec"] == 1.25
    assert safe["by"] == "ab"
    assert set(safe["st"]) == {1, 2}


def test_make_json_safe_iterable_generator():
    def gen():
        yield 1
        yield 2

    safe = _make_json_safe({"g": gen()})
    assert safe["g"] == [1, 2]


def test_make_json_safe_unknown_type():
    class Weird:
        def __str__(self):
            return "weird"

    safe = _make_json_safe({"w": Weird()})
    assert safe["w"] == "weird"


def test_format_timedelta_safe_when_format_timedelta_raises(monkeypatch):
    def bad_td(_o):
        raise RuntimeError("simulated")

    monkeypatch.setattr(sys.modules["frappe.utils"], "format_timedelta", bad_td)
    assert _format_timedelta_safe(timedelta(hours=1)) == str(timedelta(hours=1))


def test_format_timedelta_safe_coerces_non_str_return(monkeypatch):
    monkeypatch.setattr(
        sys.modules["frappe.utils"],
        "format_timedelta",
        MagicMock(return_value=42),
    )
    assert _format_timedelta_safe(timedelta(seconds=1)) == "42"


def test_doc_as_json_str_round_trip():
    body = _doc_as_json_str({"name": "X"})
    assert json.loads(body)["name"] == "X"


def test_doc_as_json_str_timedelta_becomes_string():
    body = _doc_as_json_str({"name": "PI-1", "schedule": timedelta(hours=2)})
    data = json.loads(body)
    assert data["name"] == "PI-1"
    assert isinstance(data["schedule"], str)
