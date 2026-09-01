"""
Unit tests for container-safe logging.

Covers:
- ``frappe_microservice`` sets ``FRAPPE_STREAM_LOGGING=1`` on import via
  ``os.environ.setdefault`` (and preserves an operator-set ``""`` opt-out).
- ``isolation.patch_logger_fallback`` wraps ``create_handler`` so an ``OSError``
  from the file-handler branch degrades to a ``StreamHandler`` instead of
  crashing; the patch is idempotent.
"""

import importlib
import logging
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from frappe_microservice.isolation import patch_logger_fallback


def test_import_sets_stream_logging_default():
    """Importing the package (done in conftest) leaves the env var truthy."""
    assert os.environ.get("FRAPPE_STREAM_LOGGING") == "1"


def test_setdefault_preserves_opt_out(monkeypatch):
    """With FRAPPE_STREAM_LOGGING pre-set to '', reimport must NOT overwrite it."""
    monkeypatch.setenv("FRAPPE_STREAM_LOGGING", "")
    import frappe_microservice

    importlib.reload(frappe_microservice)
    assert os.environ.get("FRAPPE_STREAM_LOGGING") == ""


def test_setdefault_sets_value_when_unset(monkeypatch):
    """With FRAPPE_STREAM_LOGGING unset, reimport sets it to '1'."""
    monkeypatch.delenv("FRAPPE_STREAM_LOGGING", raising=False)
    import frappe_microservice

    importlib.reload(frappe_microservice)
    assert os.environ.get("FRAPPE_STREAM_LOGGING") == "1"


@pytest.fixture
def fake_logger_module(monkeypatch):
    """Inject a fake frappe.utils.logger module with a real create_handler.

    ``patch_logger_fallback`` does ``from frappe.utils import logger``, which
    resolves the attribute off ``sys.modules['frappe.utils']`` — so the fake
    must be attached there (not on the ``frappe.utils`` MagicMock attribute).
    """
    fake = types.ModuleType("frappe.utils.logger")

    def create_handler(module, site=None, *args, **kwargs):
        # Simulate the file-handler branch failing (read-only / missing dir).
        raise OSError(2, "No such file or directory", f"/logs/{module}.log")

    fake.create_handler = create_handler
    monkeypatch.setitem(sys.modules, "frappe.utils.logger", fake)
    monkeypatch.setattr(sys.modules["frappe.utils"], "logger", fake, raising=False)
    return fake


def test_patch_logger_fallback_degrades_to_stream(fake_logger_module, caplog):
    patch_logger_fallback()
    assert fake_logger_module._microservice_create_handler_patched is True

    with caplog.at_level(logging.WARNING):
        handlers = fake_logger_module.create_handler("database")

    assert isinstance(handlers, list)
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert any(
        "StreamHandler" in r.message or "fall" in r.message.lower()
        for r in caplog.records
    )


def test_patch_logger_fallback_idempotent(fake_logger_module):
    patch_logger_fallback()
    first = fake_logger_module.create_handler
    patch_logger_fallback()
    second = fake_logger_module.create_handler
    assert first is second, "double-apply must not re-wrap create_handler"


def test_ensure_site_log_dirs_covers_cwd_and_sites_path(tmp_path, monkeypatch):
    """Presync only used FRAPPE_SITES_PATH; Frappe opens <cwd>/<site>/logs when
    CWD is WORKDIR /app — that gap caused database.log ENOENT skips."""
    from frappe_microservice.site_config import ensure_site_log_dirs

    sites = tmp_path / "sites"
    sites.mkdir()
    cwd = tmp_path / "workdir"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("FRAPPE_SITE", "central.example")
    monkeypatch.setenv("FRAPPE_SITES_PATH", str(sites))

    ensure_site_log_dirs()

    assert (cwd / "central.example" / "logs").is_dir()
    assert (sites / "central.example" / "logs").is_dir()


def test_create_site_config_default_sites_path_is_app_sites():
    """Regression: default was '/app', which diverged from entrypoint/presync."""
    import inspect
    from frappe_microservice import site_config as sc

    src = inspect.getsource(sc.create_site_config)
    assert "os.getenv('FRAPPE_SITES_PATH', '/app/sites')" in src
    assert "os.getenv('FRAPPE_SITES_PATH', '/app')" not in src


def test_presync_applies_logger_fallback_before_connect(monkeypatch, tmp_path):
    """patch_logger_fallback must run before frappe.connect or missing CWD
    site logs skip fixtures / SERVICE_POST_PRESYNC."""
    import frappe

    from frappe_microservice.presync import presync_service_doctypes

    doctypes = tmp_path / "dt"
    doctypes.mkdir()
    order = []

    monkeypatch.setenv("FRAPPE_SITE", "dev.localhost")
    monkeypatch.setenv("FRAPPE_SITES_PATH", str(tmp_path / "sites"))
    (tmp_path / "sites").mkdir()

    monkeypatch.setattr(
        "frappe_microservice.presync.patch_logger_fallback",
        lambda: order.append("patch"),
    )

    def _init(**_kw):
        order.append("init")

    def _connect():
        order.append("connect")
        raise Exception("Conn fail")

    monkeypatch.setattr(frappe, "init", _init)
    monkeypatch.setattr(frappe, "connect", _connect)
    monkeypatch.setattr(frappe, "destroy", MagicMock())

    with patch("logging.getLogger") as mock_get:
        mock_get.return_value = MagicMock()
        presync_service_doctypes(doctypes_path=str(doctypes), service_name="test")

    assert order == ["patch", "init", "connect"]
