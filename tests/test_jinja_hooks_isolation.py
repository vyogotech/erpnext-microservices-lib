"""Jinja hook isolation — central-site hook leak regression (company_tracker)."""

import sys
import types
from unittest.mock import patch

import frappe
from frappe_microservice.core import MicroserviceApp
from frappe_microservice.isolation import (
    _filter_jinja_hook_paths,
    _filter_jinja_hooks_config,
)


def _reset_microservice_guards():
    for flag in (
        "_microservice_isolation_applied",
        "_microservice_load_app_hooks_patched",
        "_microservice_hooks_resolution_patched",
        "_microservice_get_hooks_jinja_patched",
        "_microservice_controller_patched",
    ):
        if hasattr(frappe, flag):
            delattr(frappe, flag)
    sys.modules.pop("frappe.utils.jinja", None)


def test_filter_jinja_hook_paths_drops_company_tracker():
    allowed = {"frappe", "erpnext", "quotation_tracker"}
    paths = [
        "company_tracker.branding.print_logo.get_company_logo_data_uri",
        "frappe.utils.data.sanitize_html",
    ]
    assert _filter_jinja_hook_paths(paths, allowed) == [
        "frappe.utils.data.sanitize_html"
    ]


def test_filter_jinja_hooks_config():
    allowed = {"frappe", "erpnext", "quotation_tracker"}
    raw = {
        "methods": [
            "company_tracker.branding.print_logo.get_company_logo_data_uri",
            "erpnext.accounts.utils.get_currency_symbol",
        ],
        "filters": [
            "company_tracker.branding.print_logo.file_url_to_data_uri",
        ],
    }
    filtered = _filter_jinja_hooks_config(raw, allowed)
    assert filtered["methods"] == ["erpnext.accounts.utils.get_currency_symbol"]
    assert filtered["filters"] == []


def test_get_hooks_jinja_filter_strips_central_site_hooks():
    """Regression: shared Redis app_hooks must not expose company_tracker to microservices."""
    _reset_microservice_guards()

    cached_hooks = {
        "jinja": {
            "methods": [
                "company_tracker.branding.print_logo.get_company_logo_data_uri",
            ],
            "filters": [],
        }
    }

    def original_get_hooks(hook=None, default="_KEEP_DEFAULT_LIST", app_name=None):
        if hook == "jinja":
            return cached_hooks["jinja"]
        return frappe._dict(cached_hooks)

    frappe.get_hooks = original_get_hooks
    with patch("frappe.connect"):
        MicroserviceApp(
            "quotation-service",
            load_framework_hooks=["frappe", "erpnext"],
        )

    jinja = frappe.get_hooks("jinja")
    assert jinja["methods"] == []
