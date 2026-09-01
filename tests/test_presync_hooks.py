"""Tests for SERVICE_POST_PRESYNC hook resolution."""

import sys

import pytest

from frappe_microservice.isolation import _resolve_post_presync_callable


def _clear_bootstrap_module():
    sys.modules.pop("bootstrap", None)


def test_resolve_post_presync_callable_imports_bootstrap(tmp_path, monkeypatch):
    service = tmp_path / "service"
    service.mkdir()
    (service / "bootstrap.py").write_text(
        "def run_post_presync():\n"
        "    return 'ok'\n"
    )

    monkeypatch.setenv("SERVICE_PATH", str(service))
    monkeypatch.delitem(sys.modules, "bootstrap", raising=False)
    monkeypatch.syspath_prepend(str(service))

    fn = _resolve_post_presync_callable("bootstrap.run_post_presync")
    assert fn() == "ok"


def test_resolve_post_presync_callable_rejects_invalid_hook():
    with pytest.raises(ValueError, match="Invalid SERVICE_POST_PRESYNC"):
        _resolve_post_presync_callable("notcallable")


def test_resolve_post_presync_callable_rejects_non_callable(tmp_path, monkeypatch):
    service = tmp_path / "service"
    service.mkdir()
    (service / "bootstrap.py").write_text("run_post_presync = 'not-a-function'\n")

    monkeypatch.setenv("SERVICE_PATH", str(service))
    monkeypatch.delitem(sys.modules, "bootstrap", raising=False)
    monkeypatch.syspath_prepend(str(service))

    with pytest.raises(TypeError, match="not callable"):
        _resolve_post_presync_callable("bootstrap.run_post_presync")


def test_resolve_post_presync_callable_appends_service_path_once(tmp_path, monkeypatch):
    service = tmp_path / "service"
    service.mkdir()
    (service / "bootstrap.py").write_text(
        "def run_post_presync():\n"
        "    return 'ok'\n"
    )

    monkeypatch.setenv("SERVICE_PATH", str(service))
    monkeypatch.delitem(sys.modules, "bootstrap", raising=False)
    monkeypatch.syspath_prepend(str(service))

    fn = _resolve_post_presync_callable("bootstrap.run_post_presync")
    assert fn() == "ok"
    assert sys.path.count(str(service)) == 1


def test_resolve_post_presync_callable_missing_module(tmp_path, monkeypatch):
    service = tmp_path / "service"
    service.mkdir()
    monkeypatch.setenv("SERVICE_PATH", str(service))
    monkeypatch.delitem(sys.modules, "bootstrap", raising=False)
    monkeypatch.syspath_prepend(str(service))

    with pytest.raises(ModuleNotFoundError):
        _resolve_post_presync_callable("missing_module.run_post_presync")
