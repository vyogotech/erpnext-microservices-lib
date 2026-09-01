"""
Shared policy-mode helpers for security workstreams.

Several security features (PERMISSION_MODE, CSRF_MODE, TENANT_STRICT) ship in
warn-only mode in 1.5.0 and flip to enforce in 2.0. They all share the same
tri-state resolution: a constructor kwarg wins over an env var, which wins over
a default. Unknown values log a warning and fall back to the default.
"""

import logging
import os

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_ENFORCE = "enforce"

_VALID_MODES = frozenset({MODE_OFF, MODE_WARN, MODE_ENFORCE})

_logger = logging.getLogger(__name__)


def resolve_mode(kwarg_value, env_name, default=MODE_WARN):
    """
    Resolve an ``off|warn|enforce`` mode from a kwarg, then an env var, then a
    default. Case-insensitive. Unknown values log a warning and return the
    default.

    Args:
        kwarg_value: value passed to a constructor (or None to defer to env).
        env_name: environment variable name to consult when kwarg is None.
        default: fallback mode (defaults to ``"warn"``).

    Returns:
        One of ``"off"``, ``"warn"``, ``"enforce"``.
    """
    raw = kwarg_value
    if raw is None:
        raw = os.getenv(env_name)
    if raw is None:
        return default

    normalized = str(raw).strip().lower()
    if normalized in _VALID_MODES:
        return normalized

    _logger.warning(
        "Unknown mode %r for %s; falling back to %r",
        raw,
        env_name,
        default,
    )
    return default


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def resolve_bool(kwarg_value, env_name, default=False):
    """
    Resolve a boolean-ish flag from kwarg, then env (``1/true/yes/on``), then
    default. Used for non-tri-state flags such as ``MASK_PERMISSION_ERRORS``.

    An env var that is unset **or empty** falls through to ``default``. A
    declared-but-valueless env var (``env: [{name: MULTI_TENANT}]`` in a
    Kubernetes manifest, or a missing ConfigMap key) arrives as ``""``, and
    reading that as "false" would silently flip a security flag that defaults
    on. ``resolve_mode`` already behaves this way for the tri-state flags.
    """
    if kwarg_value is not None:
        if isinstance(kwarg_value, bool):
            return kwarg_value
        return str(kwarg_value).strip().lower() in _TRUTHY
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY
