"""
Optional RQ-based background task processing.

Activated by setting ENABLE_RQ=1 in the environment.
The embedded SimpleWorker runs in a daemon thread on a service-scoped queue.
"""
import copy
import logging
import os
import threading
import traceback

import frappe
from frappe.utils.local import _contextvar
from redis import Redis
from rq import Queue, Retry
from rq.worker import SimpleWorker

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _rq_job_wrapper(func, base_ctx, service_name, *args, **kwargs):
    """
    Top-level wrapper called by RQ for each job.

    Restores Frappe context, connects to DB, runs the task,
    and commits/rollbacks + logs errors appropriately.
    Must be a module-level function so RQ can import it.
    """
    try:
        # Restore Frappe context from a copy of the base context
        _contextvar.set(copy.copy(base_ctx))
        frappe.connect()

        # Execute the actual task
        func(*args, **kwargs)

        # Commit on success
        frappe.db.commit()
    except Exception as e:
        logger.error(f"RQ job failed [{service_name}]: {e}\n{traceback.format_exc()}")
        try:
            frappe.log_error(
                title=f"Background task failed: {func.__name__ if hasattr(func, '__name__') else func}",
                message=traceback.format_exc(),
            )
        except Exception:
            pass
        try:
            frappe.db.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            frappe.destroy()
        except Exception:
            pass


class BackgroundTaskMixin:
    """
    Mixin that adds env-driven RQ support to MicroserviceApp.

    Set ENABLE_RQ=1 to auto-start the embedded SimpleWorker at startup.
    Use enqueue_task() to queue jobs. run_background_task() is unaffected.
    """

    def _maybe_start_rq_worker(self):
        """
        Called from MicroserviceApp.__init__. Reads ENABLE_RQ env var.
        If truthy, starts the embedded RQ SimpleWorker daemon thread.
        """
        self._rq_enabled = False
        self._rq_worker_thread = None
        self._rq_queue = None

        enable_rq = os.getenv("ENABLE_RQ", "").strip().lower()
        if enable_rq not in _TRUTHY:
            self.logger.info("RQ worker disabled (ENABLE_RQ not set)")
            return

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

        try:
            conn = Redis.from_url(redis_url)
            self._rq_queue = Queue(self.name, connection=conn)
            self._rq_enabled = True

            self._start_rq_worker(conn)
            self.logger.info(
                f"RQ worker started on queue '{self.name}' (redis={redis_url})"
            )
        except Exception as e:
            self.logger.error(f"Failed to start RQ worker: {e}", exc_info=True)
            self._rq_enabled = False

    def _start_rq_worker(self, conn):
        """Spawn a daemon thread running SimpleWorker.work() on the service queue."""
        worker = SimpleWorker([self._rq_queue], connection=conn)

        def _run_worker():
            try:
                worker.work(burst=False)
            except Exception as e:
                self.logger.error(f"RQ worker crashed: {e}", exc_info=True)

        thread = threading.Thread(target=_run_worker, daemon=True, name=f"rq-worker-{self.name}")
        thread.start()
        self._rq_worker_thread = thread

    def enqueue_task(self, func, *args, max_retries=0, job_timeout=None, on_failure=None, **kwargs):
        """
        Enqueue a function to the service-scoped RQ queue.

        Args:
            func: The function to execute in the background.
            *args: Positional args passed to func.
            max_retries: Number of retry attempts on failure (default: 0).
            job_timeout: Timeout in seconds for the job (default: None).
            on_failure: Optional callback on failure.
            **kwargs: Keyword args passed to func.

        Raises:
            RuntimeError: If ENABLE_RQ is not set / worker not running.
        """
        if not self._rq_enabled:
            raise RuntimeError(
                "RQ worker is not running. Set ENABLE_RQ=1 in the environment to enable it."
            )

        enqueue_kwargs = {
            "job_timeout": job_timeout or -1,
        }

        if max_retries > 0:
            enqueue_kwargs["retry"] = Retry(max=max_retries)

        if on_failure:
            enqueue_kwargs["on_failure"] = on_failure

        return self._rq_queue.enqueue(
            _rq_job_wrapper,
            func,
            self._frappe_local_base,
            self.name,
            *args,
            **kwargs,
            **enqueue_kwargs,
        )
