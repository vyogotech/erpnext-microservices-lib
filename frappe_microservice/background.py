"""
Optional RQ-based background task processing.

Activated by setting ENABLE_RQ=1 in the environment.
The embedded SimpleWorker runs in a daemon thread on a service-scoped queue.

Gunicorn ``--preload`` fork safety
----------------------------------
The container entrypoint runs Gunicorn with ``--preload``, so ``MicroserviceApp``
is constructed in the Gunicorn *master* before workers are forked. Threads do
not survive ``os.fork()``, but any lock they hold at fork time (Python's import
``_ModuleLock``, logging locks, socket state) is copied into the child in the
"held" state with no thread left to release it — the child deadlocks on its
first request that touches that lock.

So under ``--preload`` the embedded worker thread is never started in the
master. Instead an ``os.register_at_fork(after_in_child=...)`` hook starts it in
the Gunicorn workers, and a per-pod ``flock`` keeps exactly one consumer active
per container (the previous effective concurrency), with automatic fail-over to
a sibling worker if the owning worker exits.
"""

import logging
import os
import sys
import tempfile
import threading
import traceback

import frappe

# Re-exported for tests that restore Frappe context around the RQ wrapper.
from frappe.utils.local import _contextvar  # noqa: F401
from redis import Redis
from rq import Queue, Retry, Worker
from rq.worker import SimpleWorker

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_gunicorn_preload() -> bool:
    """
    Return True when the app is being imported by Gunicorn running with --preload.

    Covers the flag on the command line (our entrypoint) and via
    GUNICORN_CMD_ARGS. ``preload_app = True`` set only in a gunicorn.conf.py is
    not detected — the entrypoint is the supported way to run services.
    """
    if "gunicorn" not in sys.modules:
        return False
    if "--preload" in sys.argv:
        return True
    return "--preload" in os.getenv("GUNICORN_CMD_ARGS", "").split()


def _embedded_worker_lock_path(queue_name: str, master_pid: int) -> str:
    """Per-container lock file used to elect a single embedded RQ consumer."""
    lock_dir = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in queue_name)
    return os.path.join(lock_dir, f"rq-embedded-{safe_name}-{master_pid}.lock")


def _rq_job_wrapper(
    func,
    service_name,
    frappe_site,
    sites_path,
    doctypes_path,
    controllers_path,
    *args,
    **kwargs,
):
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
        from frappe_microservice.presync import register_service_doctypes

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
            if (
                hasattr(frappe, "local")
                and hasattr(frappe.local, "db")
                and frappe.local.db
            ):
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
        self._rq_redis_url = None
        self._rq_master_pid = None
        self._rq_lock_fd = None

        enable_rq = os.getenv("ENABLE_RQ", "").strip().lower()
        if enable_rq not in _TRUTHY:
            return

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        self._rq_redis_url = redis_url

        try:
            conn = Redis.from_url(redis_url)
            self._rq_queue = Queue(self.name, connection=conn)
            self._rq_enabled = True

            # If WORKER_MODE is set, we don't start the thread.
            # The caller (e.g. CLI) will call run_worker() separately.
            if os.getenv("WORKER_MODE") == "1":
                self.logger.info("Worker mode enabled. Not starting background thread.")
                return

            # Under Gunicorn --preload we are in the master, about to fork.
            # Never start a thread here (see module docstring): defer it to the
            # Gunicorn workers. enqueue_task() keeps working in the master —
            # jobs simply wait in Redis until a worker's consumer picks them up.
            if _is_gunicorn_preload() and hasattr(os, "register_at_fork"):
                self._rq_master_pid = os.getpid()
                os.register_at_fork(after_in_child=self._start_rq_worker_after_fork)
                self.logger.info(
                    f"Gunicorn --preload detected: RQ worker for queue '{self.name}' "
                    f"will start in a forked worker (master pid={self._rq_master_pid})"
                )
                return

            self._start_rq_worker(conn)
            self.logger.info(
                f"RQ worker started on queue '{self.name}' (redis={redis_url})"
            )
        except Exception as e:
            self.logger.error(f"Failed to start RQ worker: {e}", exc_info=True)
            self._rq_enabled = False

    def _start_rq_worker_after_fork(self):
        """
        ``os.register_at_fork(after_in_child=...)`` hook (Gunicorn --preload).

        Runs in every forked Gunicorn worker (including workers respawned after
        a crash/timeout). Creates a fresh Redis connection for the child and
        starts the embedded SimpleWorker thread, which waits on a per-container
        lock so only one worker consumes the queue at a time.
        """
        if not self._rq_enabled or os.getenv("WORKER_MODE") == "1":
            return
        # Only direct children of the Gunicorn master are Gunicorn workers.
        # A grandchild (os.fork / multiprocessing inside a worker) must not
        # start another consumer.
        if self._rq_master_pid is not None and os.getppid() != self._rq_master_pid:
            return
        redis_url = self._rq_redis_url or os.getenv(
            "REDIS_URL", "redis://localhost:6379"
        )
        try:
            conn = Redis.from_url(redis_url)
            self._rq_queue = Queue(self.name, connection=conn)
            lock_path = _embedded_worker_lock_path(
                self.name, self._rq_master_pid or os.getppid()
            )
            self._start_rq_worker(conn, singleton_lock_path=lock_path)
        except Exception as e:
            self.logger.error(
                f"Failed to start RQ worker after fork (pid={os.getpid()}): {e}",
                exc_info=True,
            )

    def _acquire_embedded_worker_lock(self, lock_path):
        """
        Block until this process owns the per-container embedded-worker lock.

        ``flock`` is released by the kernel when the owning process exits, so if
        the owning Gunicorn worker dies, a sibling's waiting thread takes over.
        The fd is kept open for the life of the process. Returns False only if
        the lock could not be used at all and we chose to run anyway (logged).
        """
        try:
            import fcntl

            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except (ImportError, OSError) as e:
            self.logger.warning(
                f"Embedded RQ worker lock unavailable ({lock_path}: {e}); "
                f"this Gunicorn worker (pid={os.getpid()}) will consume "
                f"queue '{self.name}' without per-container exclusivity"
            )
            return False

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.logger.info(
                f"RQ worker for queue '{self.name}' on standby in pid={os.getpid()} "
                "(another Gunicorn worker owns the queue)"
            )
            fcntl.flock(fd, fcntl.LOCK_EX)  # releases the GIL while blocked

        self._rq_lock_fd = fd
        return True

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
        self.logger.info(
            f"Starting RQ worker in main process on queue '{self.name}'..."
        )
        worker.work(burst=burst)

    def _start_rq_worker(self, conn, singleton_lock_path=None):
        """
        Spawn a daemon thread running SimpleWorker.work() on the service queue.

        If ``singleton_lock_path`` is given (Gunicorn --preload workers), the
        thread first waits for the per-container lock so only one process in
        the container consumes the queue.
        """
        # The embedded worker runs off the main thread, so signals don't work
        # and the death penalty is a no-op — job_timeout CANNOT be enforced here.
        # For real timeouts run a dedicated worker (WORKER_MODE=1 +
        # run_worker(), which uses a forking Worker).
        self.logger.warning(
            "Embedded RQ SimpleWorker started (dev/best-effort): it cannot "
            "enforce job_timeout because signals do not work off the main "
            "thread. For production, run a dedicated worker with WORKER_MODE=1 "
            "and run_worker()."
        )
        worker = SimpleWorker([self._rq_queue], connection=conn)

        # Signal handlers only work in the main thread.
        # In rq 2.x, we must monkey-patch the internal method and disable death penalty signals.
        worker._install_signal_handlers = lambda: None

        class NullDeathPenalty:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args, **kwargs):
                pass

        worker.death_penalty_class = NullDeathPenalty

        def _run_worker():
            try:
                if singleton_lock_path:
                    self._acquire_embedded_worker_lock(singleton_lock_path)
                self.logger.info(
                    f"RQ worker consuming queue '{self.name}' (pid={os.getpid()})"
                )
                worker.work(burst=False)
            except Exception as e:
                self.logger.error(f"RQ worker crashed: {e}", exc_info=True)

        thread = threading.Thread(
            target=_run_worker, daemon=True, name=f"rq-worker-{self.name}"
        )
        thread.start()
        self._rq_worker_thread = thread

    def enqueue_task(
        self, func, *args, max_retries=0, job_timeout=None, on_failure=None, **kwargs
    ):
        """
        Enqueue a function to the service-scoped RQ queue.

        Args:
            func: The function to execute in the background.
            *args: Positional args passed to func.
            max_retries: Number of retry attempts on failure (default: 0).
            job_timeout: Timeout in seconds for the job. If None, falls back to
                the ``RQ_DEFAULT_JOB_TIMEOUT`` env var (seconds); if that is
                unset or 0, uses ``-1`` (no timeout) — identical to prior
                behavior. NOTE: the embedded SimpleWorker cannot enforce
                timeouts (see run_worker /); use a forking
                worker (WORKER_MODE=1) for real enforcement.
            on_failure: Optional callback on failure.
            **kwargs: Keyword args passed to func.

        Raises:
            RuntimeError: If ENABLE_RQ is not set / worker not running.
        """
        if not self._rq_enabled:
            raise RuntimeError(
                "RQ worker is not running. Set ENABLE_RQ=1 in the environment to enable it."
            )

        # Unset env → -1 → behavior identical to today. Explicit job_timeout
        # always wins over the env default.
        default_timeout = int(os.getenv("RQ_DEFAULT_JOB_TIMEOUT", "0")) or -1
        enqueue_kwargs = {
            "job_timeout": job_timeout or default_timeout,
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
