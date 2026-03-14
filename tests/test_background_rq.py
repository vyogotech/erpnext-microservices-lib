"""
Tests for RQ-based background task processing (env-driven, opt-in).

ENABLE_RQ=1 env var activates the embedded RQ SimpleWorker.
Without it, run_background_task (threading) continues unchanged.
"""
import os
import threading
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import frappe
from frappe_microservice.core import MicroserviceApp


def _reset_microservice_guards():
    """Reset microservice idempotency guards so patches can run again."""
    for flag in (
        "_microservice_isolation_applied",
        "_microservice_load_app_hooks_patched",
        "_microservice_hooks_resolution_patched",
        "_microservice_controller_patched",
    ):
        if hasattr(frappe, flag):
            delattr(frappe, flag)


# ============================================
# ENV-DRIVEN ACTIVATION
# ============================================


class TestEnvDrivenActivation:
    """RQ worker starts/stops based on ENABLE_RQ env var."""

    def setup_method(self):
        _reset_microservice_guards()

    @patch.dict(os.environ, {}, clear=False)
    def test_worker_not_started_without_env(self):
        """When ENABLE_RQ is not set, _rq_enabled must be False and no worker thread."""
        # Remove ENABLE_RQ if it exists
        os.environ.pop("ENABLE_RQ", None)
        app = MicroserviceApp("test-service")
        assert app._rq_enabled is False
        assert app._rq_worker_thread is None

    @patch.dict(os.environ, {"ENABLE_RQ": "1", "REDIS_URL": "redis://localhost:6379"})
    @patch("frappe_microservice.background.SimpleWorker")
    @patch("frappe_microservice.background.Queue")
    @patch("frappe_microservice.background.Redis.from_url")
    def test_worker_starts_with_env(self, mock_redis, mock_queue, mock_worker):
        """When ENABLE_RQ=1, worker daemon thread starts automatically."""
        mock_worker_instance = MagicMock()
        mock_worker.return_value = mock_worker_instance

        app = MicroserviceApp("test-service")

        assert app._rq_enabled is True
        assert app._rq_worker_thread is not None
        assert app._rq_worker_thread.daemon is True

    @patch.dict(os.environ, {"ENABLE_RQ": "0"})
    def test_worker_not_started_with_falsy_env(self):
        """ENABLE_RQ=0 should not start the worker."""
        app = MicroserviceApp("test-service")
        assert app._rq_enabled is False

    @patch.dict(os.environ, {"ENABLE_RQ": "true", "REDIS_URL": "redis://localhost:6379"})
    @patch("frappe_microservice.background.SimpleWorker")
    @patch("frappe_microservice.background.Queue")
    @patch("frappe_microservice.background.Redis.from_url")
    def test_worker_starts_with_truthy_env(self, mock_redis, mock_queue, mock_worker):
        """ENABLE_RQ=true should also start the worker."""
        mock_worker_instance = MagicMock()
        mock_worker.return_value = mock_worker_instance

        app = MicroserviceApp("test-service")
        assert app._rq_enabled is True


# ============================================
# QUEUE NAME SCOPING
# ============================================


class TestServiceScopedQueue:
    """Queue name is derived from the service name."""

    def setup_method(self):
        _reset_microservice_guards()

    @patch.dict(os.environ, {"ENABLE_RQ": "1", "REDIS_URL": "redis://localhost:6379"})
    @patch("frappe_microservice.background.SimpleWorker")
    @patch("frappe_microservice.background.Queue")
    @patch("frappe_microservice.background.Redis.from_url")
    def test_service_scoped_queue_name(self, mock_redis, mock_queue_cls, mock_worker):
        """Queue name must match the service name."""
        mock_worker.return_value = MagicMock()

        app = MicroserviceApp("signup-service")

        # Queue was created with the service name
        mock_queue_cls.assert_called_once()
        call_kwargs = mock_queue_cls.call_args
        assert call_kwargs[0][0] == "signup-service" or call_kwargs[1].get("name") == "signup-service"


# ============================================
# ENQUEUE TASK
# ============================================


class TestEnqueueTask:
    """Tests for the enqueue_task method."""

    def setup_method(self):
        _reset_microservice_guards()

    @patch.dict(os.environ, {}, clear=False)
    def test_enqueue_without_enable_rq_raises(self):
        """enqueue_task must raise RuntimeError when ENABLE_RQ is not set."""
        os.environ.pop("ENABLE_RQ", None)
        app = MicroserviceApp("test-service")

        with pytest.raises(RuntimeError, match="ENABLE_RQ"):
            app.enqueue_task(lambda: None)

    @patch.dict(os.environ, {"ENABLE_RQ": "1", "REDIS_URL": "redis://localhost:6379"})
    @patch("frappe_microservice.background.SimpleWorker")
    @patch("frappe_microservice.background.Queue")
    @patch("frappe_microservice.background.Redis.from_url")
    def test_enqueue_task_puts_job_on_queue(self, mock_redis, mock_queue_cls, mock_worker):
        """enqueue_task must call queue.enqueue with the wrapper function."""
        mock_queue_instance = MagicMock()
        mock_queue_cls.return_value = mock_queue_instance
        mock_worker.return_value = MagicMock()

        app = MicroserviceApp("test-service")

        def my_task(x):
            return x

        app.enqueue_task(my_task, 42)

        mock_queue_instance.enqueue.assert_called_once()

    @patch.dict(os.environ, {"ENABLE_RQ": "1", "REDIS_URL": "redis://localhost:6379"})
    @patch("frappe_microservice.background.SimpleWorker")
    @patch("frappe_microservice.background.Queue")
    @patch("frappe_microservice.background.Redis.from_url")
    def test_enqueue_task_with_retry(self, mock_redis, mock_queue_cls, mock_worker):
        """enqueue_task must pass retry config through to RQ."""
        mock_queue_instance = MagicMock()
        mock_queue_cls.return_value = mock_queue_instance
        mock_worker.return_value = MagicMock()

        app = MicroserviceApp("test-service")

        app.enqueue_task(lambda: None, max_retries=3, job_timeout=120)

        call_kwargs = mock_queue_instance.enqueue.call_args
        assert call_kwargs[1].get("retry") is not None or call_kwargs[1].get("job_timeout") == 120


# ============================================
# JOB WRAPPER (context restoration)
# ============================================


class TestRqJobWrapper:
    """Tests for _rq_job_wrapper — the top-level function RQ calls."""

    def setup_method(self):
        _reset_microservice_guards()

    @patch("frappe.destroy")
    @patch("frappe.db", create=True)
    @patch("frappe.connect")
    @patch("frappe_microservice.background._contextvar")
    def test_job_wrapper_restores_context(self, mock_ctxvar, mock_connect, mock_db, mock_destroy):
        """Wrapper must call _contextvar.set() with a copy of base_ctx."""
        from frappe_microservice.background import _rq_job_wrapper

        base_ctx = {"site": "test.local", "conf": {}}
        mock_db.commit = MagicMock()

        _rq_job_wrapper(lambda: None, base_ctx, "test-service")

        mock_ctxvar.set.assert_called_once()
        # The set arg should be a copy, not the original
        set_arg = mock_ctxvar.set.call_args[0][0]
        assert set_arg is not base_ctx

    @patch("frappe.destroy")
    @patch("frappe.db", create=True)
    @patch("frappe.connect")
    @patch("frappe_microservice.background._contextvar")
    def test_job_wrapper_calls_connect_and_commit(self, mock_ctxvar, mock_connect, mock_db, mock_destroy):
        """On success: frappe.connect() then frappe.db.commit()."""
        from frappe_microservice.background import _rq_job_wrapper

        mock_db.commit = MagicMock()

        _rq_job_wrapper(lambda: None, {}, "test-service")

        mock_connect.assert_called_once()
        mock_db.commit.assert_called_once()

    @patch("frappe.destroy")
    @patch("frappe.db", create=True)
    @patch("frappe.log_error", create=True)
    @patch("frappe.connect")
    @patch("frappe_microservice.background._contextvar")
    def test_job_wrapper_calls_log_error_on_failure(self, mock_ctxvar, mock_connect, mock_log_error, mock_db, mock_destroy):
        """On failure: frappe.log_error() is called."""
        from frappe_microservice.background import _rq_job_wrapper

        mock_db.rollback = MagicMock()

        def failing_task():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            _rq_job_wrapper(failing_task, {}, "test-service")

        mock_log_error.assert_called_once()

    @patch("frappe.destroy")
    @patch("frappe.db", create=True)
    @patch("frappe.log_error", create=True)
    @patch("frappe.connect")
    @patch("frappe_microservice.background._contextvar")
    def test_job_wrapper_calls_rollback_on_failure(self, mock_ctxvar, mock_connect, mock_log_error, mock_db, mock_destroy):
        """On failure: frappe.db.rollback() is called."""
        from frappe_microservice.background import _rq_job_wrapper

        mock_db.rollback = MagicMock()

        def failing_task():
            raise RuntimeError("oops")

        with pytest.raises(RuntimeError):
            _rq_job_wrapper(failing_task, {}, "test-service")

        mock_db.rollback.assert_called_once()

    @patch("frappe.destroy")
    @patch("frappe.db", create=True)
    @patch("frappe.connect")
    @patch("frappe_microservice.background._contextvar")
    def test_job_wrapper_calls_destroy_always(self, mock_ctxvar, mock_connect, mock_db, mock_destroy):
        """frappe.destroy() must always be called (success or failure)."""
        from frappe_microservice.background import _rq_job_wrapper

        mock_db.commit = MagicMock()

        _rq_job_wrapper(lambda: None, {}, "test-service")
        mock_destroy.assert_called_once()

        # Also on failure
        mock_destroy.reset_mock()
        mock_db.rollback = MagicMock()

        with pytest.raises(ValueError):
            _rq_job_wrapper(lambda: (_ for _ in ()).throw(ValueError("x")), {}, "test-service")

    @patch("frappe.destroy")
    @patch("frappe.db", create=True)
    @patch("frappe.connect")
    @patch("frappe_microservice.background._contextvar")
    def test_job_wrapper_calls_destroy_on_failure(self, mock_ctxvar, mock_connect, mock_db, mock_destroy):
        """frappe.destroy() called even when the task raises."""
        from frappe_microservice.background import _rq_job_wrapper

        mock_db.rollback = MagicMock()
        with patch("frappe.log_error", create=True):
            try:
                _rq_job_wrapper(lambda: 1/0, {}, "test-service")
            except ZeroDivisionError:
                pass

        mock_destroy.assert_called_once()


# ============================================
# WORKER THREAD PROPERTIES
# ============================================


class TestWorkerThread:
    """Worker thread must be a daemon so it doesn't block shutdown."""

    def setup_method(self):
        _reset_microservice_guards()

    @patch.dict(os.environ, {"ENABLE_RQ": "1", "REDIS_URL": "redis://localhost:6379"})
    @patch("frappe_microservice.background.SimpleWorker")
    @patch("frappe_microservice.background.Queue")
    @patch("frappe_microservice.background.Redis.from_url")
    def test_worker_thread_is_daemon(self, mock_redis, mock_queue, mock_worker):
        """Worker thread must be a daemon thread."""
        mock_worker.return_value = MagicMock()
        app = MicroserviceApp("test-service")

        assert app._rq_worker_thread.daemon is True


# ============================================
# EXISTING run_background_task UNAFFECTED
# ============================================


class TestRunBackgroundTaskUnaffected:
    """run_background_task (threading) must work independently of RQ."""

    def setup_method(self):
        _reset_microservice_guards()

    @patch.dict(os.environ, {}, clear=False)
    def test_run_background_task_works_without_rq(self):
        """run_background_task must still work when ENABLE_RQ is not set."""
        os.environ.pop("ENABLE_RQ", None)
        app = MicroserviceApp("test-service")

        result = []

        def task():
            result.append(42)

        with patch("frappe.connect"), \
             patch("frappe.db", create=True) as mock_db, \
             patch("frappe.destroy"):
            mock_db.commit = MagicMock()
            thread = app.run_background_task(task)
            thread.join(timeout=5)

        assert 42 in result
