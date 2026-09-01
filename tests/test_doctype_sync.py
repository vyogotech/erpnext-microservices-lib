"""
Unit tests for Hibernate-style DocType schema sync.

Covers sync-mode resolution, migrate-vs-create import behavior, per-file error
isolation, the multi-replica advisory lock, metadata-version suppression, and
the tenant_id column ensure step. ``frappe`` is mocked (see tests/conftest.py).
"""

import json

import pytest
from unittest.mock import MagicMock, patch
import frappe

import frappe_microservice.presync as iso


# --- sync mode resolution --------------------------------------------------


def test_resolve_sync_mode_default_migrate(monkeypatch):
    monkeypatch.delenv("DOCTYPE_SYNC_MODE", raising=False)
    assert iso.resolve_doctype_sync_mode() == "migrate"


def test_resolve_sync_mode_create(monkeypatch):
    monkeypatch.setenv("DOCTYPE_SYNC_MODE", "create")
    assert iso.resolve_doctype_sync_mode() == "create"


def test_resolve_sync_mode_unknown_warns(monkeypatch):
    monkeypatch.setenv("DOCTYPE_SYNC_MODE", "bogus")
    logger = MagicMock()
    assert iso.resolve_doctype_sync_mode(logger) == "migrate"
    assert logger.warning.called


# --- per-file import behavior ---------------------------------------------


def _write_doctype(tmp_path, name="Widget", module="Widgets"):
    d = tmp_path / name.lower()
    d.mkdir()
    p = d / f"{name.lower()}.json"
    p.write_text(json.dumps({"name": name, "module": module, "doctype": "DocType"}))
    return p


def test_create_mode_skips_existing(tmp_path):
    p = _write_doctype(tmp_path)
    frappe.db.exists = MagicMock(return_value=True)
    with (
        patch.object(iso, "_import_doc_suppressing_cache_reset") as imp,
        patch.object(iso, "_ensure_tenant_id_column"),
    ):
        result = iso._sync_doctype_json_file(
            p, "svc", "create", MagicMock(), ensure_module_def=MagicMock()
        )
    assert result is False
    imp.assert_not_called()


def test_create_mode_imports_missing(tmp_path):
    p = _write_doctype(tmp_path)
    frappe.db.exists = MagicMock(return_value=False)
    with (
        patch.object(iso, "_import_doc_suppressing_cache_reset") as imp,
        patch.object(iso, "_ensure_tenant_id_column") as ensure,
    ):
        result = iso._sync_doctype_json_file(
            p, "svc", "create", MagicMock(), ensure_module_def=MagicMock()
        )
    assert result is True
    imp.assert_called_once()
    ensure.assert_called_once()
    assert ensure.call_args[0][0] == "Widget"


def test_sync_registers_module_in_memory_before_import(tmp_path):
    """Module Def in DB is not enough — Frappe get_module_app reads
    frappe.local.module_app. Without in-memory registration, import creates
    the SQL table (DDL auto-commits) then fails with 'Module X not found',
    leaving an orphan table and no DocType metadata."""
    p = _write_doctype(tmp_path, name="Phone Auth Session", module="Signup Service")
    frappe.db.exists = MagicMock(return_value=False)
    call_order = []

    def track_register(module, service_app):
        call_order.append(("register", module, service_app))

    def track_import(*_a, **_k):
        call_order.append(("import",))
        return True

    with (
        patch.object(
            iso, "register_module_for_service", side_effect=track_register
        ) as reg,
        patch.object(iso, "_get_import_file_by_path", return_value=track_import),
        patch.object(iso, "_ensure_tenant_id_column"),
    ):
        iso._sync_doctype_json_file(
            p, "signup_service", "migrate", MagicMock(), ensure_module_def=MagicMock()
        )

    reg.assert_called_once_with("Signup Service", "signup_service")
    assert call_order[0][0] == "register", call_order
    assert call_order[1][0] == "import", call_order


def test_migrate_mode_imports_existing_doctype(tmp_path):
    """migrate mode must call import_file_by_path even when the DocType exists."""
    p = _write_doctype(tmp_path)
    frappe.db.exists = MagicMock(return_value=True)
    fake_import = MagicMock()
    with (
        patch.object(iso, "_get_import_file_by_path", return_value=fake_import),
        patch.object(iso, "_ensure_tenant_id_column"),
    ):
        result = iso._sync_doctype_json_file(
            p, "svc", "migrate", MagicMock(), ensure_module_def=MagicMock()
        )
    assert result is True
    fake_import.assert_called_once()
    # force=False enables skip-if-unchanged / update-if-changed
    assert fake_import.call_args.kwargs.get("force") is False


def test_migrate_mode_ensures_tenant_id(tmp_path):
    p = _write_doctype(tmp_path)
    frappe.db.exists = MagicMock(return_value=False)
    with (
        patch.object(iso, "_get_import_file_by_path", return_value=MagicMock()),
        patch.object(iso, "_ensure_tenant_id_column") as ensure,
    ):
        iso._sync_doctype_json_file(
            p, "svc", "migrate", MagicMock(), ensure_module_def=MagicMock()
        )
    ensure.assert_called_once()


def test_migrate_mode_skip_is_truthful(tmp_path):
    """When import_file_by_path returns False (unchanged), the file is a no-op:
    returns False (so no commit) and does not touch the tenant_id column."""
    p = _write_doctype(tmp_path)
    frappe.db.exists = MagicMock(return_value=True)
    skipping_import = MagicMock(return_value=False)
    with (
        patch.object(iso, "_get_import_file_by_path", return_value=skipping_import),
        patch.object(iso, "_ensure_tenant_id_column") as ensure,
    ):
        result = iso._sync_doctype_json_file(
            p, "svc", "migrate", MagicMock(), ensure_module_def=MagicMock()
        )
    assert result is False
    ensure.assert_not_called()


def test_presync_migrate_skip_does_not_commit(tmp_path, monkeypatch):
    """A boot where all JSONs are unchanged must not trigger a commit."""
    db, fake_import, ok = _run_presync(
        tmp_path, monkeypatch, import_side_effect=lambda *a, **k: False
    )
    assert ok is True
    assert fake_import.call_count == 2
    db.commit.assert_not_called()


# --- metadata version suppression -----------------------------------------


def test_suppress_metadata_version_reset_restores():
    import frappe.cache_manager as cm

    original = cm.reset_metadata_version
    with iso._suppress_metadata_version_reset():
        assert cm.reset_metadata_version is not original
        # no-op callable
        assert cm.reset_metadata_version() is None
    assert cm.reset_metadata_version is original


# --- stale hooks cache ------------------------------------------------------


def test_invalidate_app_hooks_clears_both_caches():
    """Narrowing the app list must not leave hooks for excluded apps behind.

    frappe.init() returns early when local.initialised is already set, so
    presync's own init is a no-op in a process that booted Frappe first. The
    pre-patch doc_events map then survives and the first DocType insert dies on
    frappe.get_attr -> AppNotInstalledError.
    """
    frappe.client_cache = MagicMock()
    frappe.local.doc_events_hooks = {"*": {"before_insert": ["other_app.stamp"]}}

    iso._invalidate_app_hooks_cache(MagicMock())

    frappe.client_cache.delete_value.assert_called_once_with("app_hooks")
    assert frappe.local.doc_events_hooks is None


def test_invalidate_app_hooks_survives_missing_caches():
    """No client_cache and no derived map must not raise."""
    frappe.client_cache = None
    frappe.local.doc_events_hooks = None

    iso._invalidate_app_hooks_cache(None)

    assert frappe.local.doc_events_hooks is None


# --- tenant_id column ensure ----------------------------------------------


def test_ensure_tenant_id_column_added_when_missing():
    frappe.db.table_exists = MagicMock(return_value=True)
    frappe.db.has_column = MagicMock(return_value=False)
    frappe.db.db_type = "mariadb"
    # code prefers frappe.db.sql_ddl when present.
    frappe.db.sql_ddl = MagicMock()
    frappe.db.commit = MagicMock()
    iso._ensure_tenant_id_column("Widget", MagicMock())
    queries = [c.args[0] for c in frappe.db.sql_ddl.call_args_list]
    assert any("ADD COLUMN" in q for q in queries)
    # MariaDB dialect: backticked identifiers + ADD INDEX
    assert any("`tabWidget`" in q for q in queries)
    assert any("ADD INDEX" in q for q in queries)


def test_ensure_tenant_id_column_postgres_dialect():
    """Postgres must get quoted identifiers + IF NOT EXISTS + CREATE INDEX
    (backticks / ADD INDEX are MySQL-only syntax errors on Postgres)."""
    frappe.db.table_exists = MagicMock(return_value=True)
    frappe.db.has_column = MagicMock(return_value=False)
    frappe.db.db_type = "postgres"
    frappe.db.sql_ddl = MagicMock()
    frappe.db.commit = MagicMock()
    iso._ensure_tenant_id_column("Widget", MagicMock())
    queries = [c.args[0] for c in frappe.db.sql_ddl.call_args_list]
    assert any("ADD COLUMN IF NOT EXISTS tenant_id" in q for q in queries)
    assert any("CREATE INDEX IF NOT EXISTS" in q for q in queries)
    assert not any("`" in q for q in queries)


def test_ensure_tenant_id_column_skipped_when_present():
    frappe.db.table_exists = MagicMock(return_value=True)
    frappe.db.has_column = MagicMock(return_value=True)
    frappe.db.sql = MagicMock()
    frappe.db.sql_ddl = MagicMock()
    iso._ensure_tenant_id_column("Widget", MagicMock())
    frappe.db.sql.assert_not_called()
    frappe.db.sql_ddl.assert_not_called()


def test_ensure_tenant_id_column_swallows_errors():
    frappe.db.table_exists = MagicMock(return_value=True)
    frappe.db.has_column = MagicMock(return_value=False)
    frappe.db.db_type = "mariadb"
    frappe.db.sql_ddl = MagicMock(side_effect=Exception("DDL blew up"))
    logger = MagicMock()
    # must not raise
    iso._ensure_tenant_id_column("Widget", logger)
    assert logger.warning.called


# --- advisory lock ---------------------------------------------------------


def _mariadb_db(get_lock_result):
    db = MagicMock()
    db.db_type = "mariadb"

    def _sql(query, *a, **k):
        if "GET_LOCK" in query:
            return get_lock_result
        return MagicMock()

    db.sql = MagicMock(side_effect=_sql)
    return db


def test_advisory_lock_mariadb_acquire_and_release(monkeypatch):
    db = _mariadb_db([[1]])
    monkeypatch.setattr(frappe, "db", db)
    with iso._presync_advisory_lock("lockname") as locked:
        assert locked is True
    queries = [c.args[0] for c in db.sql.call_args_list]
    assert any("GET_LOCK" in q for q in queries)
    assert any("RELEASE_LOCK" in q for q in queries)


def test_advisory_lock_mariadb_timeout_skips(monkeypatch):
    db = _mariadb_db([[0]])
    monkeypatch.setattr(frappe, "db", db)
    with iso._presync_advisory_lock("lockname") as locked:
        assert locked is False
    queries = [c.args[0] for c in db.sql.call_args_list]
    # not acquired → RELEASE_LOCK must NOT be issued
    assert not any("RELEASE_LOCK" in q for q in queries)


def test_advisory_lock_released_on_exception(monkeypatch):
    db = _mariadb_db([[1]])
    monkeypatch.setattr(frappe, "db", db)
    with pytest.raises(RuntimeError):
        with iso._presync_advisory_lock("lockname") as locked:
            assert locked is True
            raise RuntimeError("boom")
    queries = [c.args[0] for c in db.sql.call_args_list]
    assert any("RELEASE_LOCK" in q for q in queries)


def test_advisory_lock_postgres_uses_try_lock(monkeypatch):
    """Postgres must use the NON-blocking pg_try_advisory_lock (the blocking
    pg_advisory_lock has no timeout and would hang starting pods forever)."""
    db = MagicMock()
    db.db_type = "postgres"
    db.sql = MagicMock(return_value=[[True]])
    monkeypatch.setattr(frappe, "db", db)
    with iso._presync_advisory_lock("lockname") as locked:
        assert locked is True
    queries = [c.args[0] for c in db.sql.call_args_list]
    assert any("pg_try_advisory_lock" in q for q in queries)
    assert not any("SELECT pg_advisory_lock(" in q for q in queries)
    assert any("pg_advisory_unlock" in q for q in queries)


def test_advisory_lock_postgres_timeout_skips(monkeypatch):
    """When the try-lock keeps returning False, the loop must give up at the
    deadline and yield False (skip) instead of blocking forever."""
    db = MagicMock()
    db.db_type = "postgres"
    db.sql = MagicMock(return_value=[[False]])
    monkeypatch.setattr(frappe, "db", db)
    monkeypatch.setattr(iso.time, "sleep", MagicMock())
    # Deterministic clock: first call sets the deadline, later calls exceed it
    # after two try-lock attempts.
    ticks = iter([0, 1, 2, 999, 999, 999])
    monkeypatch.setattr(iso.time, "monotonic", lambda: next(ticks))
    with iso._presync_advisory_lock("lockname", timeout=3) as locked:
        assert locked is False
    queries = [c.args[0] for c in db.sql.call_args_list]
    assert any("pg_try_advisory_lock" in q for q in queries)
    # not acquired → unlock must NOT be issued
    assert not any("pg_advisory_unlock" in q for q in queries)


def test_advisory_lock_no_db_yields_true(monkeypatch):
    monkeypatch.setattr(frappe, "db", None)
    with iso._presync_advisory_lock("lockname") as locked:
        assert locked is True


def test_lock_name_sanitized_and_capped():
    name = iso._presync_lock_name("dev.localhost", "my service/app")
    assert len(name) <= 64
    assert "/" not in name


# --- presync loop: error isolation + lock integration ---------------------


def _run_presync(
    tmp_path, monkeypatch, mode="migrate", import_side_effect=None, get_lock=[[1]]
):
    # Two doctype JSONs so we can test per-file isolation.
    _write_doctype(tmp_path, "Alpha", "Alpha")
    _write_doctype(tmp_path, "Beta", "Beta")

    monkeypatch.setenv("DOCTYPE_SYNC_MODE", mode)
    monkeypatch.setattr(frappe, "init", MagicMock())
    monkeypatch.setattr(frappe, "connect", MagicMock())
    monkeypatch.setattr(frappe, "destroy", MagicMock())

    db = MagicMock()
    db.db_type = "mariadb"
    db.exists = MagicMock(return_value=False)
    db.has_column = MagicMock(return_value=True)
    db.table_exists = MagicMock(return_value=True)
    db.commit = MagicMock()
    db.rollback = MagicMock()

    def _sql(query, *a, **k):
        if "GET_LOCK" in query:
            return get_lock
        return MagicMock()

    db.sql = MagicMock(side_effect=_sql)
    monkeypatch.setattr(frappe, "db", db)

    fake_import = MagicMock(side_effect=import_side_effect)
    with (
        patch.object(iso, "_get_import_file_by_path", return_value=fake_import),
        patch.object(iso, "_ensure_module_def_record"),
    ):
        ok = iso.presync_service_doctypes(
            doctypes_path=str(tmp_path),
            service_name="svc",
            site="dev.localhost",
            sites_path="/tmp/sites",
            fixtures_path="/nonexistent",
        )
    return db, fake_import, ok


def test_presync_migrate_imports_all(tmp_path, monkeypatch):
    db, fake_import, ok = _run_presync(tmp_path, monkeypatch, mode="migrate")
    assert ok is True
    assert fake_import.call_count == 2
    db.commit.assert_called()


def test_presync_per_file_error_isolation(tmp_path, monkeypatch):
    # First file raises, second still imports; rollback called for the failure.
    calls = {"n": 0}

    def _imp(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bad json import")

    db, fake_import, ok = _run_presync(tmp_path, monkeypatch, import_side_effect=_imp)
    assert ok is True
    assert fake_import.call_count == 2
    db.rollback.assert_called()


def test_presync_skips_when_lock_unavailable(tmp_path, monkeypatch):
    """Lock timeout must not claim success (entrypoint leaves workers free to retry)."""
    _db, fake_import, ok = _run_presync(tmp_path, monkeypatch, get_lock=[[0]])
    assert ok is False
    fake_import.assert_not_called()


# --- CLI (frappe-ms) -------------------------------------------------------


def test_cli_migrate_runs_presync(monkeypatch):
    from frappe_microservice import cli

    monkeypatch.setattr(frappe, "init", MagicMock())
    monkeypatch.setattr(frappe, "connect", MagicMock())
    monkeypatch.setattr(frappe, "destroy", MagicMock())
    frappe.db.sql = MagicMock(return_value=[[1]])

    with patch("frappe_microservice.presync.presync_service_doctypes") as presync:
        rc = cli.main(["migrate"])

    assert rc == 0
    presync.assert_called_once()


def test_cli_migrate_fails_on_connection_error(monkeypatch):
    from frappe_microservice import cli

    monkeypatch.setattr(frappe, "init", MagicMock())
    monkeypatch.setattr(frappe, "connect", MagicMock(side_effect=Exception("no db")))

    with patch("frappe_microservice.presync.presync_service_doctypes") as presync:
        rc = cli.main(["migrate"])

    assert rc == 1
    presync.assert_not_called()


def test_cli_requires_subcommand():
    from frappe_microservice import cli

    with pytest.raises(SystemExit):
        cli.main([])


def test_cli_worker_returns_int(monkeypatch):
    from frappe_microservice import cli

    monkeypatch.setenv("SERVICE_NAME", "svc")
    assert isinstance(cli.main(["worker"]), int)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
