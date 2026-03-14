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
from rq import Queue, Retry, Worker
from rq.worker import SimpleWorker

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _rq_job_wrapper(func, service_name, frappe_site, sites_path, doctypes_path, controllers_path, *args, **kwargs):
    """
    Top-level wrapper called by RQ for each job.

    Restores Frappe context via frappe.init(), connects to DB, runs the task,
    and commits/rollbacks + logs errors appropriately.
    Must be a module-level function so RQ can import it.
    """
    try:
        # Restore Frappe context for the specified site
        frappe.init(site=frappe_site, sites_path=sites_path)
        
        # Re-register service doctypes so module resolution works in this thread.
        # MUST run after frappe.init because init clears frappe.local.
        from frappe_microservice.isolation import register_service_doctypes
        register_service_doctypes(doctypes_path, service_name)
        
        # Auto-discover controllers so service doctypes resolve correctly
        if controllers_path:
            from frappe_microservice.controller import get_controller_registry
            registry = get_controller_registry()
            # auto_discover_controllers already has a scanned_paths guard now
            registry.auto_discover_controllers(controllers_path)
        
        frappe.connect()

        # Execute the actual task
        func(*args, **kwargs)

        # Commit on success
        frappe.db.commit()
    except Exception as e:
        logger.error(f"RQ job failed [{service_name}]: {e}\n{traceback.format_exc()}")
        try:
            # We don't always have a DB connection here if connect() failed
            if hasattr(frappe, "local") and hasattr(frappe.local, "db") and frappe.local.db:
                frappe.log_error(
                    title=f"Background task failed: {func.__name__ if hasattr(func, '__name__') else func}",
                    message=traceback.format_exc(),
                )
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
            return

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

        try:
            conn = Redis.from_url(redis_url)
            self._rq_queue = Queue(self.name, connection=conn)
            self._rq_enabled = True

            # If WORKER_MODE is set, we don't start the thread. 
            # The caller (e.g. CLI) will call run_worker() separately.
            if os.getenv("WORKER_MODE") == "1":
                self.logger.info("Worker mode enabled. Not starting background thread.")
                return

            self._start_rq_worker(conn)
            self.logger.info(
                f"RQ worker started on queue '{self.name}' (redis={redis_url})"
            )
        except Exception as e:
            self.logger.error(f"Failed to start RQ worker: {e}", exc_info=True)
            self._rq_enabled = False

    def run_worker(self, burst=False):
        """
        Run the RQ worker in the MAIN thread. 
        Suitable for use as a container entrypoint command.
        """
        if not self._rq_enabled:
            # Try to initialize if not yet done
            self._maybe_start_rq_worker()
            if not self._rq_enabled:
                raise RuntimeError("RQ is not enabled. Check REDIS_URL and ENABLE_RQ.")

        conn = self._rq_queue.connection
        # Use forking Worker for performance and isolation in main process
        worker = Worker([self._rq_queue], connection=conn)
        self.logger.info(f"Starting RQ worker in main process on queue '{self.name}'...")
        worker.work(burst=burst)

    def _start_rq_worker(self, conn):
        """Spawn a daemon thread running SimpleWorker.work() on the service queue."""
        worker = SimpleWorker([self._rq_queue], connection=conn)
        
        # Signal handlers only work in the main thread. 
        # In rq 2.x, we must monkey-patch the internal method and disable death penalty signals.
        worker._install_signal_handlers = lambda: None
        
        class NullDeathPenalty:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args, **kwargs): pass
        
        worker.death_penalty_class = NullDeathPenalty

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
            self.name,
            self.frappe_site,
            self.sites_path,
            self.doctypes_path,
            self.controllers_path,
            *args,
            **kwargs,
            **enqueue_kwargs,
        )
