"""
Standalone helpers for one-shot DB presync and migration.

Runs once from the container entrypoint before Gunicorn starts.
Enforces multi-replica safety using database advisory locks.
"""

import hashlib
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import frappe

from frappe_microservice.policy import resolve_bool

# DocType schema sync modes.
DOCTYPE_SYNC_MIGRATE = "migrate"
DOCTYPE_SYNC_CREATE = "create"
_DOCTYPE_SYNC_MODES = frozenset({DOCTYPE_SYNC_MIGRATE, DOCTYPE_SYNC_CREATE})

# Fixture sync failure handling. STRICT is the default on purpose: a fixture is
# service-owned schema, so a failed import means the service runs against a
# schema it does not have. That used to be a swallowed warning, which is how a
# broken fixture stayed invisible long enough for services to grow request-time
# ALTER TABLE workarounds. SOFT exists for recovery situations (booting a pod to
# inspect a half-migrated site) and still logs at ERROR — never downgrade it to
# a warning, or this becomes invisible again.
FIXTURE_SYNC_STRICT = "strict"
FIXTURE_SYNC_SOFT = "soft"
_FIXTURE_SYNC_MODES = frozenset({FIXTURE_SYNC_STRICT, FIXTURE_SYNC_SOFT})

# tenant_id is added to tables via ALTER TABLE (absent from DocType metadata).
_TENANT_ID_COLUMN_TYPE = "varchar(140)"
_TENANT_ID_DEFAULT = "SYSTEM"

# Advisory lock defaults for concurrent-replica presync.
_PRESYNC_LOCK_TIMEOUT = 120
# Poll interval for the Postgres non-blocking try-lock loop (seconds).
_PRESYNC_LOCK_RETRY_INTERVAL = 2


def resolve_fixture_sync_mode(logger=None):
    """Resolve FIXTURE_SYNC_MODE from the environment.

    Returns ``"strict"`` (default — a failed fixture aborts startup) or
    ``"soft"`` (log at ERROR and continue). Unknown values log a warning and
    fall back to strict, so a typo fails safe rather than silently degrading.
    """
    raw = (os.getenv("FIXTURE_SYNC_MODE") or FIXTURE_SYNC_STRICT).strip().lower()
    if raw in _FIXTURE_SYNC_MODES:
        return raw
    if logger:
        logger.warning(
            "Unknown FIXTURE_SYNC_MODE=%r; falling back to %r",
            raw,
            FIXTURE_SYNC_STRICT,
        )
    return FIXTURE_SYNC_STRICT


def resolve_doctype_sync_mode(logger=None):
    """Resolve DOCTYPE_SYNC_MODE from the environment.

    Returns ``"migrate"`` (default) or ``"create"``. Unknown values log a
    warning and fall back to ``"migrate"``.
    """
    raw = (os.getenv("DOCTYPE_SYNC_MODE") or DOCTYPE_SYNC_MIGRATE).strip().lower()
    if raw in _DOCTYPE_SYNC_MODES:
        return raw
    if logger:
        logger.warning(
            "Unknown DOCTYPE_SYNC_MODE=%r; falling back to %r",
            raw,
            DOCTYPE_SYNC_MIGRATE,
        )
    return DOCTYPE_SYNC_MIGRATE


@contextmanager
def suppress_metadata_version_reset():
    """Suppress ``frappe.cache_manager.reset_metadata_version`` for the duration of
    a DocType import.

    A metadata version bump in shared Redis makes the central site reload all
    hooks on its next request; if it holds a hook pointing at a removed/renamed
    function that reload raises. DocType-specific cache keys are still cleared,
    so the service stays consistent. Must wrap every DocType import path.
    """
    import frappe.cache_manager as _cm

    original = _cm.reset_metadata_version
    _cm.reset_metadata_version = lambda: None
    try:
        yield
    finally:
        _cm.reset_metadata_version = original


_suppress_metadata_version_reset = suppress_metadata_version_reset


def _is_postgres_db():
    """Detect the DB backend via frappe.db.db_type / frappe.conf.db_type."""
    db_type = (
        getattr(getattr(frappe, "db", None), "db_type", None)
        or getattr(getattr(frappe, "conf", None), "db_type", None)
        or "mariadb"
    )
    return str(db_type).lower() in ("postgres", "postgresql")


def _ensure_tenant_id_column(doctype, logger=None, multi_tenant=None):
    """Ensure ``tab{doctype}`` has a ``tenant_id`` column (idempotent).

    tenant_id lives outside DocType metadata (see patch_valid_dict_for_tenant_id),
    so a freshly created/updated table needs it added via ALTER TABLE plus a
    non-unique index. DDL is dialect-aware (MariaDB backticks / Postgres quoted
    identifiers). Failures are logged and never abort startup.

    No-op in single-tenant mode (``MULTI_TENANT=0``). Without this guard the
    service would quietly re-create the very column that single-tenant mode
    exists to do without, reintroducing the dependency on a tenancy provider.
    """
    if not resolve_bool(multi_tenant, "MULTI_TENANT", default=True):
        return

    try:
        if not frappe.db.table_exists(doctype):
            return
        if frappe.db.has_column(doctype, "tenant_id"):
            return
        table = f"tab{doctype}"
        is_postgres = _is_postgres_db()
        # DDL implicitly commits; flush any pending transaction writes first so
        # Frappe's implicit-commit guard doesn't reject the ALTER, and prefer
        # sql_ddl when available (bypasses the DML transaction bookkeeping).
        try:
            frappe.db.commit()
        except Exception:
            pass
        run_ddl = getattr(frappe.db, "sql_ddl", None) or frappe.db.sql
        if is_postgres:
            run_ddl(
                f'ALTER TABLE "{table}" '
                f"ADD COLUMN IF NOT EXISTS tenant_id {_TENANT_ID_COLUMN_TYPE} "
                f"DEFAULT '{_TENANT_ID_DEFAULT}'"
            )
            # Postgres truncates identifiers to 63 chars; IF NOT EXISTS checks
            # the truncated name so long doctype names stay idempotent.
            index_sql = (
                f'CREATE INDEX IF NOT EXISTS "{table}_tenant_id_idx" '
                f'ON "{table}" (tenant_id)'
            )
        else:
            run_ddl(
                f"ALTER TABLE `{table}` "
                f"ADD COLUMN `tenant_id` {_TENANT_ID_COLUMN_TYPE} "
                f"DEFAULT '{_TENANT_ID_DEFAULT}'"
            )
            index_sql = f"ALTER TABLE `{table}` ADD INDEX `tenant_id` (`tenant_id`)"
        try:
            run_ddl(index_sql)
        except Exception as idx_e:
            if logger:
                logger.debug("tenant_id index on %s not added: %s", doctype, idx_e)
        try:
            frappe.db.commit()
        except Exception:
            pass
        if logger:
            logger.info("Added tenant_id column to %s", doctype)
    except Exception as e:
        if logger:
            logger.warning("Could not ensure tenant_id column on %s: %s", doctype, e)


def _presync_lock_name(site, service_app):
    """Build a sanitized advisory lock name (MySQL lock names cap at 64 chars)."""
    raw = f"fms_presync_{site or 'site'}_{service_app or 'svc'}"
    safe = "".join(c if c.isalnum() or c in ("_", "-", ".") else "_" for c in raw)
    return safe[:64]


@contextmanager
def _presync_advisory_lock(lock_name, timeout=_PRESYNC_LOCK_TIMEOUT, logger=None):
    """DB advisory lock so concurrent replicas don't run DDL simultaneously.

    Yields True if the lock was acquired (caller should do the sync), False if
    it could not be acquired within ``timeout`` (another replica is syncing —
    caller should skip, not crash). Backend detected via ``frappe.db.db_type``
    (MariaDB ``GET_LOCK(name, timeout)`` / Postgres ``pg_try_advisory_lock`` in
    a bounded retry loop — the blocking ``pg_advisory_lock`` has no timeout and
    would hang every starting pod if a replica wedged mid-sync). Tolerates
    frappe.db being unavailable by yielding True (best-effort, matches prior
    no-lock behavior).
    """
    db = getattr(frappe, "db", None)
    if not db:
        yield True
        return

    is_postgres = _is_postgres_db()
    pg_key = None
    acquired = False
    try:
        if is_postgres:
            # Derive a stable signed 64-bit key from the lock name.
            digest = hashlib.sha1(lock_name.encode()).hexdigest()
            pg_key = int(digest[:15], 16)  # fits in bigint
            # Non-blocking try-lock with a bounded retry loop (timeout parity
            # with the MariaDB GET_LOCK branch).
            deadline = time.monotonic() + timeout
            while True:
                res = frappe.db.sql("SELECT pg_try_advisory_lock(%s)", (pg_key,))
                try:
                    val = res[0][0] if (res and res[0]) else None
                except (TypeError, IndexError, KeyError):
                    val = None
                # psycopg returns a bool; be tolerant of 1/'t' style drivers.
                if val in (True, 1, "t", "true"):
                    acquired = True
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(_PRESYNC_LOCK_RETRY_INTERVAL)
        else:
            res = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, timeout))
            # MariaDB GET_LOCK returns 1 (acquired), 0 (timeout), or NULL
            # (error). Treat only an explicit 0/NULL as "not acquired"; any
            # other value counts as acquired (fail-open, matching the prior
            # no-lock behavior when the result cannot be interpreted).
            try:
                val = res[0][0] if (res and res[0]) else None
            except (TypeError, IndexError, KeyError):
                val = None
            acquired = val not in (0, None)
        if not acquired:
            if logger:
                logger.warning(
                    "presync: could not acquire advisory lock %r within %ss; "
                    "another replica is likely syncing — skipping",
                    lock_name,
                    timeout,
                )
        yield acquired
    finally:
        if acquired:
            try:
                if is_postgres:
                    frappe.db.sql("SELECT pg_advisory_unlock(%s)", (pg_key,))
                else:
                    frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
            except Exception as e:
                if logger:
                    logger.debug(
                        "presync: failed releasing advisory lock %r: %s", lock_name, e
                    )


def register_module_for_service(module_str, service_app):
    """Register a module -> service_app in frappe.local (in-memory only)."""
    if not module_str:
        return
    if not hasattr(frappe.local, "module_app"):
        frappe.local.module_app = {}
    if not hasattr(frappe.local, "app_modules"):
        frappe.local.app_modules = {}
    if service_app not in frappe.local.app_modules:
        frappe.local.app_modules[service_app] = []

    scrubbed = module_str.lower().replace(" ", "_")
    for key in (module_str, scrubbed):
        if key not in frappe.local.module_app:
            frappe.local.module_app[key] = service_app
        if key not in frappe.local.app_modules[service_app]:
            frappe.local.app_modules[service_app].append(key)


def register_service_doctypes(doctypes_path, service_name, logger=None):
    """Scan doctypes_path for DocType JSONs and register doctype names and
    module mappings in memory only (no DB).
    """
    if not doctypes_path:
        return set()

    if not os.path.isdir(doctypes_path):
        if logger:
            logger.warning(f"DocTypes directory not found: {doctypes_path}")
        return set()

    service_app = service_name.replace("-", "_")
    doctypes_dir = Path(doctypes_path)
    found_names = set()

    for json_path in doctypes_dir.glob("*/*.json"):
        try:
            with open(json_path, "r") as f:
                doc = json.load(f)
            doc_name = doc.get("name")
            if doc_name:
                found_names.add(doc_name)
            module = doc.get("module")
            register_module_for_service(module, service_app)
        except Exception as e:
            if logger:
                logger.error(f"Error reading DocType JSON at {json_path}: {e}")

    # Import globally to register service doctypes dynamically
    from frappe_microservice.isolation import _SERVICE_DOCTYPES, apply_controller_patch

    _SERVICE_DOCTYPES.update(found_names)
    apply_controller_patch()
    return found_names


def _ensure_module_def_record(module_name: str, app_name: str, logger=None):
    """Insert a Module Def record if it doesn't already exist. Idempotent."""
    try:
        if not frappe.db.table_exists("Module Def"):
            return
        if frappe.db.exists("Module Def", module_name):
            return
        m = frappe.get_doc(
            {
                "doctype": "Module Def",
                "module_name": module_name,
                "app_name": app_name,
            }
        )
        m.flags.ignore_permissions = True
        m.flags.ignore_mandatory = True
        m.flags.ignore_links = True
        m.insert()
        frappe.db.commit()
        if logger:
            logger.info("Created Module Def '%s' -> app '%s'", module_name, app_name)
    except Exception as e:
        if logger:
            logger.debug("Module Def '%s' skipped: %s", module_name, e)


def _import_doc_suppressing_cache_reset(doc: dict):
    """Import a DocType document without triggering reset_metadata_version."""
    import importlib

    # Frappe >= 16.53: submodule is not imported implicitly.
    importlib.import_module("frappe.modules.import_file")
    with suppress_metadata_version_reset():
        frappe.modules.import_file.import_doc(
            doc,
            ignore_version=True,
            reset_permissions=False,
        )


def _sync_doctype_json_file(
    json_path, service_app, sync_mode, logger, ensure_module_def=None
):
    """Sync one DocType JSON file into the DB, honoring the sync mode.

    - ``create`` mode: import only when the DocType does not yet exist
      (byte-for-byte the legacy behavior).
    - ``migrate`` mode: use Frappe's ``import_file_by_path(force=False)`` which
      skips unchanged JSONs and updates changed ones (running ``updatedb`` to
      ALTER the table additively — it never drops unknown columns like
      tenant_id). After a create/update the ``tenant_id`` column is ensured.

    All imports are wrapped in ``_suppress_metadata_version_reset``. Returns
    True if an import (create or update) was attempted. Raises on error so the
    caller can log + rollback per file.
    """
    if ensure_module_def is None:
        ensure_module_def = _ensure_module_def_record

    with open(json_path, "r") as f:
        doc = json.load(f)

    module = doc.get("module")
    if module:
        ensure_module_def(module, service_app, logger)
        # Error faced: presync logged "Module X not found" after CREATE TABLE DDL.
        # Module Def in DB is not enough — frappe.modules.utils.get_module_app()
        # reads frappe.local.module_app (in-memory). Without registration here,
        # import_file_by_path runs DDL (MySQL auto-commits) then fails on
        # controller resolution, leaving an orphan SQL table and no DocType row.
        register_module_for_service(module, service_app)

    doc_name = doc.get("name")
    if not doc_name:
        return False

    exists = frappe.db.exists("DocType", doc_name)

    if sync_mode == DOCTYPE_SYNC_CREATE:
        if not exists:
            if logger:
                logger.info("presync: creating DocType %s", doc_name)
            _import_doc_suppressing_cache_reset(doc)
            _ensure_tenant_id_column(doc_name, logger)
            return True
        return False

    # migrate mode: create-if-missing + update-if-changed via Frappe's
    # content-hash aware importer. import_file_by_path returns True only when it
    # actually (re)imported; it returns False when the JSON is unchanged (hash
    # match). Use that so logs and the commit reflect real work.
    import_file_by_path = _get_import_file_by_path()
    with suppress_metadata_version_reset():
        imported = import_file_by_path(
            str(json_path),
            force=False,
            reset_permissions=False,
        )
    if imported:
        if logger:
            logger.info(
                "presync: %s DocType %s (migrate)",
                "created" if not exists else "updated",
                doc_name,
            )
        # Ensure tenant_id only after a real create/update (docs/tasks §4.4).
        _ensure_tenant_id_column(doc_name, logger)
    elif logger:
        logger.debug("presync: DocType %s unchanged, skipped", doc_name)
    return bool(imported)


def patch_logger_fallback():
    """Defensive patch for ``frappe.utils.logger.create_handler``.

    ``FRAPPE_STREAM_LOGGING=1`` (set in ``frappe_microservice/__init__.py``) is
    the primary fix: it makes every ``frappe.logger()`` use a StreamHandler.
    But some code may call ``get_logger(..., stream_only=False)`` explicitly,
    bypassing that default and hitting the CWD-relative ``RotatingFileHandler``
    path. This wrapper catches ``OSError`` from that constructor and falls back
    to a ``StreamHandler`` instead of crashing the process.

    Idempotent (guard attribute), and logs a single warning when the fallback
    engages so operators can see which log file could not be created.
    """
    import logging as _logging

    from frappe.utils import logger as _flogger

    if getattr(_flogger, "_microservice_create_handler_patched", False):
        return

    original_create_handler = _flogger.create_handler

    def _create_handler_with_stream_fallback(*args, **kwargs):
        try:
            return original_create_handler(*args, **kwargs)
        except OSError as e:
            module = args[0] if args else kwargs.get("module", "unknown")
            _logging.getLogger(__name__).warning(
                "Frappe file log handler creation failed for module %r (%s: %s); "
                "falling back to StreamHandler. Set FRAPPE_STREAM_LOGGING=1 to "
                "avoid file handlers entirely.",
                module,
                type(e).__name__,
                e,
            )
            handler = _logging.StreamHandler()
            handler.setFormatter(
                _logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            return [handler]

    _flogger.create_handler = _create_handler_with_stream_fallback
    _flogger._microservice_create_handler_patched = True


def _get_import_file_by_path():
    """Lazy import of frappe.modules.import_file.import_file_by_path."""
    from frappe.modules.import_file import import_file_by_path

    return import_file_by_path


def _resolve_post_presync_callable(hook: str):
    """Resolve SERVICE_POST_PRESYNC (e.g. ``bootstrap.run_post_presync``) via
    importlib on SERVICE_PATH — NOT via ``frappe.get_attr()``.

    Error faced (reports-service boot):
        presync: post-presync hook bootstrap.run_post_presync failed:
        App bootstrap is not installed

    Why: ``frappe.get_attr()`` splits on the first ``.`` and treats the left
    segment as a *Frappe app name*. ``bootstrap.py`` lives at
    ``$SERVICE_PATH/bootstrap.py`` (service root), not as an installed Frappe
    app. The hook never ran, so ``run_sync_print_formats()`` never executed and
    all 15 custom Print Formats stayed missing from the DB despite HTML/JSON
    files being present in the image.

    Fix: import the module from SERVICE_PATH (same path Gunicorn uses for
    ``server:app``) and return the callable attribute.
    """
    import importlib

    service_path = os.getenv("SERVICE_PATH", "/app/service")
    if service_path and service_path not in sys.path:
        sys.path.append(service_path)

    module_name, sep, attr = hook.rpartition(".")
    if not sep or not module_name or not attr:
        raise ValueError(f"Invalid SERVICE_POST_PRESYNC hook: {hook!r}")

    module = importlib.import_module(module_name)
    fn = getattr(module, attr, None)
    if not callable(fn):
        raise TypeError(
            f"SERVICE_POST_PRESYNC {hook!r} is not callable (got {type(fn)!r})"
        )
    return fn


def _prepare_presync_runtime(service_path, service_name, doctypes_path, logger):
    """Register in-memory module maps and controllers before DocType import.

    Error faced (signup/auth presync):
        Module Signup Service not found / No module named 'X.X.doctype'
        → SQL table created (DDL auto-commit) but DocType metadata missing.

    Why: presync calls ``frappe.init()`` without MicroserviceApp's runtime
    patches. ``get_module_app()`` reads ``frappe.local.module_app`` which is
    empty until we register modules from JSON. Controllers under
    ``$SERVICE_PATH/controllers/`` are also undiscovered until scanned here.

    Also appends SERVICE_PATH to sys.path so post-presync hooks like
    ``bootstrap.run_post_presync`` can import (see ``_resolve_post_presync_callable``).
    """
    if service_path and service_path not in sys.path:
        sys.path.append(service_path)

    if doctypes_path and service_name:
        register_service_doctypes(doctypes_path, service_name, logger)

    controllers_dir = os.path.join(service_path, "controllers")
    if os.path.isdir(controllers_dir):
        from frappe_microservice.controller import get_controller_registry

        registry = get_controller_registry()
        registry.auto_discover_controllers(controllers_dir)
        if logger:
            discovered = list(registry.list_controllers().keys())
            if discovered:
                logger.info("presync: registered controllers: %s", discovered)


def _looks_like_custom_field_fixture(payload) -> bool:
    """True for a list of Custom Field rows (the common service fixture shape)."""
    if not isinstance(payload, list) or not payload:
        return False
    return all(
        isinstance(row, dict)
        and row.get("dt")
        and row.get("fieldname")
        and row.get("doctype", "Custom Field") == "Custom Field"
        for row in payload
    )


# Rough capacity of the SQL types Frappe maps fieldtypes onto. Used only to
# decide "is the new type smaller than what is already there".
_FIELDTYPE_CAPACITY = {
    "check": 1,
    "int": 11,
    "float": 21,
    "currency": 21,
    "date": 10,
    "datetime": 26,
    "select": 140,
    "link": 140,
    "data": 140,
    "small text": 65535,
    "text": 65535,
    "long text": 4294967295,
    "text editor": 4294967295,
    "code": 4294967295,
    "json": 4294967295,
}


def _assert_no_narrowing(by_doctype: dict, logger: logging.Logger) -> None:
    """Refuse a fixture that would shrink an existing column.

    Frappe issues MODIFY COLUMN when a fieldtype changes, and narrowing (Text ->
    Data, Datetime -> Date) truncates every stored value. Unlike a dropped
    column this raises nothing and logs no DROP — the data is simply shorter,
    which is far harder to notice and impossible to undo.

    Widening is left alone; only shrinking is refused.
    """
    problems = []
    for doctype, fields in by_doctype.items():
        try:
            if not frappe.db.table_exists(doctype):
                continue
            columns = {
                c["Field"]: c["Type"]
                for c in frappe.db.sql(
                    f"SHOW COLUMNS FROM `tab{doctype}`", as_dict=True
                )
            }
        except Exception as exc:
            logger.debug("narrowing check skipped for %s: %s", doctype, exc)
            continue

        for field in fields:
            fieldname = field.get("fieldname")
            current = columns.get(fieldname)
            if not current:
                continue  # new column, nothing to shrink
            new_cap = _FIELDTYPE_CAPACITY.get(str(field.get("fieldtype", "")).lower())
            if new_cap is None:
                continue
            cur = str(current).lower()
            if "longtext" in cur or "mediumtext" in cur:
                cur_cap = 4294967295
            elif "text" in cur:
                cur_cap = 65535
            elif "varchar" in cur:
                try:
                    cur_cap = int(cur.split("(")[1].split(")")[0])
                except Exception:
                    cur_cap = 140
            else:
                continue  # numeric/date columns: leave to Frappe
            if new_cap < cur_cap:
                problems.append(
                    f"{doctype}.{fieldname}: {current} -> {field.get('fieldtype')} "
                    f"(capacity {cur_cap} -> {new_cap}) would TRUNCATE existing data"
                )

    if problems:
        for p in problems:
            logger.error("presync: refusing narrowing change — %s", p)
        raise RuntimeError(
            "fixture would narrow existing column(s) and truncate data: "
            + "; ".join(problems)
        )


def _import_custom_field_fixture(json_path, logger: logging.Logger) -> bool:
    """Install Custom Field rows without Frappe's cross-app module resolution.

    ``import_file_by_path`` resolves a fixture's module by walking modules.txt
    for EVERY installed app. A microservice container only has its own service
    plus this library on sys.path, so any app present on the site but absent
    from the container (tenancy_app) makes that walk raise
    ``ModuleNotFoundError`` before a single field is touched — which is what
    silently blocked every service fixture.

    Custom Fields need none of that: create_custom_fields writes them directly
    and is idempotent, so it sidesteps the resolution entirely.

    Returns True when handled here; False to fall through to the generic import.
    """
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        raise RuntimeError(f"unreadable fixture {json_path}: {exc}") from exc

    if not _looks_like_custom_field_fixture(payload):
        return False

    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    by_doctype: dict = {}
    for row in payload:
        row = {k: v for k, v in row.items() if k not in ("name", "doctype", "modified")}
        by_doctype.setdefault(row.pop("dt"), []).append(row)

    _assert_no_narrowing(by_doctype, logger)
    create_custom_fields(by_doctype, ignore_validate=True)
    frappe.db.commit()
    logger.info(
        "presync: installed %d custom field(s) across %d doctype(s) from %s",
        sum(len(v) for v in by_doctype.values()),
        len(by_doctype),
        os.path.basename(str(json_path)),
    )
    return True


def _sync_fixtures_from_path(fixtures_path: str | None, logger: logging.Logger):
    """Import all .json fixture files from fixtures_path into the DB.

    Fixtures are service-owned schema, so a failure here means the service will
    run against a schema it does not have. That used to be logged as a warning
    and swallowed, which is how a broken fixture stayed invisible long enough
    for a service to grow request-time ``ALTER TABLE`` workarounds instead.

    Now it RAISES: the service fails to start and a developer has to fix the
    fixture. Loud at deploy beats mysterious at runtime.
    """
    if not fixtures_path or not os.path.isdir(fixtures_path):
        if fixtures_path:
            logger.warning("Fixtures directory not found: %s", fixtures_path)
        return

    _import = _get_import_file_by_path()

    fix_dir = Path(fixtures_path)
    # Import module_def.json first: DocType JSON references a module name and
    # Frappe expects the Module Def row to exist before doctype/fixture import.
    # Error faced: "Module Signup Service not found" when module_def ran after doctypes.
    module_def = fix_dir / "module_def.json"
    if module_def.is_file():
        try:
            logger.info("presync: importing fixture %s", module_def.name)
            frappe.flags.mute_emails = True
            _import(
                str(module_def),
                data_import=True,
                force=True,
                reset_permissions=True,
            )
            frappe.flags.mute_emails = False
            frappe.db.commit()
        except Exception as e:
            frappe.flags.mute_emails = False
            logger.warning(
                "presync: error importing fixture %s: %s", module_def.name, e
            )
            try:
                frappe.db.rollback()
            except Exception:
                pass

    for json_path in sorted(fix_dir.glob("*.json")):
        if json_path.name == "module_def.json":
            continue
        try:
            logger.info("presync: importing fixture %s", json_path.name)
            frappe.flags.mute_emails = True
            if not _import_custom_field_fixture(json_path, logger):
                _import(
                    str(json_path),
                    data_import=True,
                    force=True,
                    reset_permissions=True,
                )
                frappe.db.commit()
            frappe.flags.mute_emails = False
        except Exception as e:
            frappe.flags.mute_emails = False
            try:
                frappe.db.rollback()
            except Exception:
                pass
            # repr(), not str(): the exception that hid this for so long
            # stringified to an empty message, so the old log line reported that
            # a fixture failed without ever saying why.
            # ERROR in both modes — soft must never be quiet, only non-fatal.
            logger.error(
                "presync: FAILED importing fixture %s: %s", json_path.name, repr(e)
            )
            if resolve_fixture_sync_mode(logger) == FIXTURE_SYNC_STRICT:
                raise RuntimeError(
                    f"fixture import failed for {json_path.name}: {e!r}. "
                    "The service would run against a schema it does not have. "
                    "Fix the fixture, or set FIXTURE_SYNC_MODE=soft to boot "
                    "anyway (recovery only — the schema stays broken)."
                ) from e
            logger.error(
                "presync: FIXTURE_SYNC_MODE=soft — continuing with INCOMPLETE "
                "schema for %s. This service may fail at runtime.",
                json_path.name,
            )


def _invalidate_app_hooks_cache(logger=None):
    """Drop any cached ``app_hooks`` map so ``get_hooks()`` rebuilds it.

    ``patch_app_resolution`` narrows ``get_installed_apps``, but
    ``frappe.get_hooks()`` serves ``app_hooks`` from ``client_cache`` (or the
    site cache in developer_mode). That map is warm whenever presync runs in a
    process that already booted Frappe normally -- the integration suite, or
    anything embedding presync after a request has been served.

    A stale map still carries ``doc_events`` for apps the patch just excluded.
    The first DocType insert then resolves one via ``frappe.get_attr`` and dies
    with ``AppNotInstalledError``, which ``_sync_doctype_json_file`` swallows
    into a warning -- the DocType silently never gets created.

    ``_load_app_hooks`` reads ``get_installed_apps()``, which *is* patched, so
    dropping the cache is enough to make the rebuild correct.
    ``IsolationMixin._patch_hooks_resolution`` does the same for MicroserviceApp.
    """
    client_cache = getattr(frappe, "client_cache", None)
    if client_cache:
        try:
            client_cache.delete_value("app_hooks")
        except Exception as e:
            if logger:
                logger.debug("presync: could not clear app_hooks cache: %s", e)

    for attr in ("_site_cached_load_app_hooks", "_request_cached_load_app_hooks"):
        clear = getattr(getattr(frappe, attr, None), "clear_cache", None)
        if callable(clear):
            try:
                clear()
            except Exception as e:
                if logger:
                    logger.debug("presync: could not clear %s: %s", attr, e)

    # ``get_doc_hooks()`` derives ``local.doc_events_hooks`` once and then
    # short-circuits on it, so dropping app_hooks alone is not enough. This
    # matters because ``frappe.init()`` returns early when ``local.initialised``
    # is already set -- presync's own init is a no-op in any process that booted
    # Frappe first, leaving the pre-patch doc_events map in place.
    local = getattr(frappe, "local", None)
    if local is not None and getattr(local, "doc_events_hooks", None):
        try:
            local.doc_events_hooks = None
        except Exception as e:
            if logger:
                logger.debug("presync: could not clear doc_events_hooks: %s", e)


def presync_service_doctypes(
    doctypes_path: str | None = None,
    service_name: str | None = None,
    site: str | None = None,
    sites_path: str | None = None,
    fixtures_path: str | None = None,
) -> bool:
    """One-shot DB sync of service doctypes and fixtures. Designed to be called
    once from the container entrypoint before Gunicorn starts.

    Safe across multiple pods: all operations are idempotent (check-then-create
    with caught duplicates). Two pods racing will both succeed without conflict.

    Falls back to env vars: SERVICE_PATH, SERVICE_NAME, DOCTYPES_PATH, FRAPPE_SITE,
    FRAPPE_SITES_PATH. If DOCTYPES_PATH is not set, it defaults to
    {SERVICE_PATH}/{service_app}/{service_app}/doctype (e.g. .../signup_service/signup_service/doctype).
    Fixtures auto-discovered at <SERVICE_PATH>/fixtures/.

    Returns:
        True if sync completed (or there was nothing to sync). False if the
        sync was skipped (DB unreachable, advisory lock not acquired) or hit
        an unexpected error — callers such as the entrypoint must not set
        ``_DOCTYPES_PRESYNCED`` in that case so workers can retry on first
        request.
    """
    service_path = os.getenv("SERVICE_PATH", "/app/service")
    service_name = service_name or os.getenv("SERVICE_NAME", "")
    service_app = service_name.replace("-", "_") if service_name else ""

    # Default DOCTYPES_PATH to conventional location: {SERVICE_PATH}/{service_app}/{service_app}/doctype
    if not doctypes_path and service_app:
        doctypes_path = os.path.join(service_path, service_app, service_app, "doctype")
    doctypes_path = doctypes_path or os.getenv("DOCTYPES_PATH")

    if not fixtures_path:
        fixtures_path = os.path.join(service_path, "fixtures")
    has_doctypes = doctypes_path and os.path.isdir(doctypes_path)
    has_fixtures = fixtures_path and os.path.isdir(fixtures_path)
    if not has_doctypes and not has_fixtures:
        return True
    site = site or os.getenv("FRAPPE_SITE", "site1.local")
    sites_path = sites_path or os.getenv("FRAPPE_SITES_PATH", "/app/sites")

    logger = logging.getLogger("presync")

    # Same container-logging hardening as entrypoint / MicroserviceApp
    #: stream logging, CWD under sites, mkdir CWD-relative
    # site logs, and create_handler fallback — before frappe.connect(), which
    # otherwise FileNotFoundError's on <cwd>/<site>/logs/database.log and
    # skips all fixture / SERVICE_POST_PRESYNC work.
    os.environ.setdefault("FRAPPE_STREAM_LOGGING", "1")
    try:
        from frappe_microservice.site_config import ensure_site_log_dirs

        ensure_site_log_dirs(site=site, sites_path=sites_path)
    except Exception as e:
        logger.debug("presync: ensure_site_log_dirs skipped: %s", e)
    if os.path.isdir(sites_path):
        try:
            os.chdir(sites_path)
        except OSError as e:
            logger.warning("presync: could not chdir to %s: %s", sites_path, e)
    try:
        patch_logger_fallback()
    except Exception as e:
        logger.debug("presync: patch_logger_fallback not applied: %s", e)

    # Patch RedisWrapper.make_key to namespace cache keys before frappe.init()
    # to avoid caching polluted central-site config or metadata in Redis.
    # Restrict app resolution BEFORE frappe.init(). Without this, frappe.connect()
    # walks every app installed on the site — including ones this container has no
    # way to import (tenancy_app) — and raises ModuleNotFoundError, so presync
    # logged "cannot connect to DB" and skipped. That is why service fixtures were
    # never applied and services grew request-time schema workarounds instead.
    # patch_app_resolution also applies the Redis key namespacing internally.
    from frappe_microservice.isolation import (
        get_allowed_apps,
        patch_app_resolution,
        patch_load_app_hooks,
    )

    framework_hooks = [
        h.strip()
        for h in (os.getenv("LOAD_FRAMEWORK_HOOKS", "") or "").replace(",", " ").split()
        if h.strip()
    ]
    patch_app_resolution(service_name, framework_hooks, logger)

    try:
        frappe.init(site=site, sites_path=sites_path)
        frappe.connect()
    except Exception as e:
        logger.warning("presync: cannot connect to DB (%s), skipping", e)
        try:
            frappe.destroy()
        except Exception:
            pass
        return False

    # MicroserviceApp installs this for the request path; presync runs earlier,
    # so it must too — the cache drop below forces a rebuild through it.
    patch_load_app_hooks(get_allowed_apps(service_name, framework_hooks), logger)

    _invalidate_app_hooks_cache(logger)

    sync_mode = resolve_doctype_sync_mode(logger)
    lock_name = _presync_lock_name(site, service_app)

    _prepare_presync_runtime(service_path, service_name, doctypes_path, logger)

    succeeded = False
    try:
        # Multi-replica safety: serialize DDL across concurrently starting pods.
        with _presync_advisory_lock(lock_name, logger=logger) as locked:
            if not locked:
                # Another replica may still be syncing — leave workers free to
                # retry rather than claiming presync finished for this process.
                return False

            if has_doctypes:
                doctypes_dir = Path(doctypes_path)
                imported_any = False
                for json_path in doctypes_dir.glob("*/*.json"):
                    try:
                        if _sync_doctype_json_file(
                            json_path, service_app, sync_mode, logger
                        ):
                            imported_any = True
                    except Exception as e:
                        logger.warning(
                            "presync: error syncing %s: %s",
                            json_path,
                            e,
                            exc_info=True,
                        )
                        try:
                            frappe.db.rollback()
                        except Exception:
                            pass

                if imported_any:
                    frappe.db.commit()
                logger.info("presync: service doctype sync complete")

            if has_fixtures:
                _sync_fixtures_from_path(fixtures_path, logger)
                logger.info("presync: fixture sync complete")

            hook = (os.getenv("SERVICE_POST_PRESYNC") or "").strip()
            if hook:
                try:
                    logger.info("presync: running post-presync hook %s", hook)
                    # Do NOT use frappe.get_attr(hook) — see _resolve_post_presync_callable.
                    _resolve_post_presync_callable(hook)()
                    frappe.db.commit()
                    logger.info("presync: post-presync hook complete")
                except Exception as e:
                    logger.warning("presync: post-presync hook %s failed: %s", hook, e)
                    try:
                        frappe.db.rollback()
                    except Exception:
                        pass
            succeeded = True
    except Exception as e:
        logger.warning("presync: unexpected error: %s", e)
        succeeded = False
    finally:
        try:
            frappe.destroy()
        except Exception:
            pass
    return succeeded
